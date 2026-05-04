# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: pipeline.py
# Projeto.......: timelapse_crop
# Função........: Orquestrar o fluxo principal de geração de recortes históricos
#                 do GOES-19, do download da imagem-fonte até o encode final.
#
# Papel na arquitetura
# --------------------
# Este módulo é a camada de orquestração do projeto. Ele coordena:
#
# - config.py      -> parâmetros operacionais
# - goes_client.py -> geração dos slots temporais + download HTTP
# - cropper.py     -> recorte da imagem da região de interesse
# - encoder.py     -> geração do vídeo final (MP4/GIF)
# - paths.py       -> organização dos diretórios de execução
# - ramdisk.py     -> uso opcional de armazenamento temporário em RAM
# - regions.py     -> resolução geométrica da região
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O processamento é sequencial: uma imagem por vez.
#
# 2. O manifest JSON foi mantido como saída persistente para auditoria e debug.
#
# 3. Downloads são sempre efêmeros.
#
# 4. Frames cropados podem ser:
#    - persistentes, se keep_frames_after_encode = True
#    - efêmeros, se keep_frames_after_encode = False
#
# 5. O loop principal foi enriquecido com telemetria visível de progresso.
#
# 6. O pipeline coopera com o encerramento controlado via CTRL+C.
#
# 7. A fase de encode MP4 sai do Live do rich e passa a exibir linhas textuais
#    estilo FFmpeg no terminal, deixando o progresso mais transparente.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.6.0
#
# Histórico
# ---------
# 0.6.0 - Alterado o fluxo para encerrar o Live do rich antes do encode e
#         exibir progresso textual estilo FFmpeg no terminal, seguido pelo
#         resumo final normal da execução.
# 0.5.0 - Adicionada telemetria real da fase de encode MP4 com callback do
#         FFmpeg, retorno expandido do pipeline e exibição explícita de codec e
#         engine utilizados.
# 0.4.0 - Adicionados cancelamento cooperativo, telemetria visível de download,
#         tempos de crop e resumo estatístico da execução.
# =============================================================================

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from config import AppConfig
from cropper import crop_image
from encoder import (
    FFmpegCancelledError,
    FFmpegEncodeResult,
    FFmpegProgress,
    encode_gif,
    encode_mp4,
)
from goes_client import (
    DownloadCancelledError,
    DownloadProgress,
    GoesClient,
    build_frame_specs,
    utc_now,
)
from paths import build_project_paths, build_run_paths, ensure_dirs
from ramdisk import RamDiskManager
from regions import get_region


# =============================================================================
# [1] CONSOLE DE EXECUÇÃO
# =============================================================================
console = Console()


# =============================================================================
# [2] EXCEÇÕES E CONTROLE DE CANCELAMENTO
# =============================================================================
class UserAbortError(RuntimeError):
    """
    Sinaliza encerramento controlado solicitado pelo usuário.
    """


@dataclass(slots=True)
class RunCancellationToken:
    """
    Token cooperativo de cancelamento da execução.
    """

    _event: threading.Event = field(default_factory=threading.Event)
    reason: str = "Execução interrompida pelo usuário."

    def request_shutdown(self, reason: str | None = None) -> None:
        if reason:
            self.reason = reason
        self._event.set()

    def is_shutdown_requested(self) -> bool:
        return self._event.is_set()

    def raise_if_requested(self) -> None:
        if self.is_shutdown_requested():
            raise UserAbortError(self.reason)


# =============================================================================
# [3] ESTRUTURAS DE RESULTADO E ESTATÍSTICA
# =============================================================================
@dataclass(slots=True)
class RuntimeStats:
    """
    Acumula estatísticas úteis da execução.
    """

    expected_frames: int = 0
    processed_frames: int = 0
    successful_frames: int = 0
    failed_frames: int = 0
    total_downloaded_bytes: int = 0
    total_download_elapsed_s: float = 0.0
    total_crop_elapsed_s: float = 0.0
    started_at: float = field(default_factory=time.perf_counter)

    @property
    def total_elapsed_s(self) -> float:
        return max(time.perf_counter() - self.started_at, 0.0)

    @property
    def average_crop_time_s(self) -> float:
        if self.successful_frames == 0:
            return 0.0
        return self.total_crop_elapsed_s / self.successful_frames

    @property
    def average_download_speed_mb_s(self) -> float:
        if self.total_download_elapsed_s <= 0:
            return 0.0
        return (self.total_downloaded_bytes / (1024 * 1024)) / self.total_download_elapsed_s


@dataclass(slots=True)
class PipelineResult:
    """
    Estrutura de retorno do pipeline.
    """

    run_dir: Path
    expected_frames: int
    frames_generated: int
    failed_frames: int
    mp4_path: Path | None
    gif_path: Path | None
    total_downloaded_bytes: int
    total_elapsed_s: float
    average_crop_time_s: float
    average_download_speed_mb_s: float
    mp4_codec_used: str | None = None
    mp4_engine_label: str | None = None
    mp4_encode_elapsed_s: float = 0.0
    mp4_output_size_bytes: int = 0


# =============================================================================
# [4] UTILITÁRIOS INTERNOS
# =============================================================================
def _clean_previous_jpgs(directory: Path) -> None:
    if not directory.exists():
        return

    for file_path in directory.glob("*.jpg"):
        file_path.unlink()


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _cleanup_temp_session_root(temp_session_root: Path) -> None:
    shutil.rmtree(temp_session_root, ignore_errors=True)


def _format_mb_s(value_in_bytes_per_s: float) -> str:
    return f"{value_in_bytes_per_s / (1024 * 1024):.2f} MB/s"


def _format_mb(value_in_bytes: int) -> str:
    return f"{value_in_bytes / (1024 * 1024):.1f} MB"


def _format_ffmpeg_time(seconds: float) -> str:
    """
    Converte segundos em HH:MM:SS.xx para exibição estilo FFmpeg.
    """
    if seconds < 0:
        seconds = 0.0

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60

    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"


def _build_overall_details(
    stats: RuntimeStats,
    *,
    phase: str,
    last_crop_s: float | None = None,
    current_frame_label: str = "-",
) -> str:
    parts = [
        f"Esperados {stats.expected_frames}",
        f"Processados {stats.processed_frames}",
        f"OK {stats.successful_frames}",
        f"Falhas {stats.failed_frames}",
        f"Atual {current_frame_label}",
        f"Fase {phase}",
        f"Média crop {stats.average_crop_time_s:.2f}s",
    ]

    if last_crop_s is not None:
        parts.append(f"Último crop {last_crop_s:.2f}s")

    return " | ".join(parts)


def _build_progress_layout() -> tuple[Progress, Progress]:
    """
    Constrói dois progress bars:
    - progresso geral da execução
    - progresso da fase atual de download
    """
    overall_progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        TextColumn("• {task.fields[details]}"),
        expand=True,
    )

    phase_progress = Progress(
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TransferSpeedColumn(),
        TextColumn("• {task.fields[details]}"),
        expand=True,
    )

    return overall_progress, phase_progress


# =============================================================================
# [5] CALLBACKS E FORMATADORES DA FASE DE ENCODE
# =============================================================================
def _make_ffmpeg_style_printer(total_frames: int):
    """
    Retorna um callback que imprime progresso textual estilo FFmpeg.

    Estratégia:
    - sobrescreve a mesma linha do terminal com carriage return
    - imprime newline automático ao final do encode
    """
    last_printed = {"line": ""}

    def _printer(progress_info: FFmpegProgress) -> None:
        fps_text = f"{progress_info.fps:.1f}" if progress_info.fps is not None else "N/A"
        line = (
            f"frame={progress_info.frame:>5} "
            f"fps={fps_text:>6} "
            f"time={_format_ffmpeg_time(progress_info.out_time_seconds)} "
            f"size={_format_mb(progress_info.total_size_bytes):>8} "
            f"speed={progress_info.speed:>6} "
            f"codec={progress_info.codec_used} "
            f"engine={progress_info.engine_label}"
        )

        print("\r" + line, end="", flush=True)
        last_printed["line"] = line

        if progress_info.progress_state == "end":
            print(flush=True)

    return _printer


# =============================================================================
# [6] NÚCLEO DO PIPELINE
# =============================================================================
def _run_inner(
    config: AppConfig,
    ramdisk_root: Path | None,
    cancel_token: RunCancellationToken,
) -> PipelineResult:
    project_paths = build_project_paths(config.root_dir)
    run_id = utc_now().strftime("run_%Y%m%dT%H%M%SZ")

    temp_root = (
        ramdisk_root / "goes_timelapse"
        if ramdisk_root is not None
        else project_paths.local_temp_root
    )

    run_paths = build_run_paths(
        project_paths,
        run_id=run_id,
        temp_root=temp_root,
        keep_frames_after_encode=config.pipeline.keep_frames_after_encode,
    )

    ensure_dirs(
        project_paths.output_root,
        project_paths.local_temp_root,
        run_paths.run_dir,
        run_paths.temp_session_root,
        run_paths.downloads_dir,
        run_paths.frames_dir,
        run_paths.manifests_dir,
    )

    _clean_previous_jpgs(run_paths.frames_dir)

    region = get_region(config.crop.region_name)

    frame_specs = build_frame_specs(
        base_url=config.download.base_url,
        resolution=config.download.resolution,
        end_time_utc=utc_now(),
        minutes_back=config.pipeline.minutes_back,
        step_minutes=config.pipeline.step_minutes,
    )

    stats = RuntimeStats(expected_frames=len(frame_specs))

    client = GoesClient(
        timeout_s=config.download.timeout_s,
        max_retries=config.download.max_retries,
        chunk_size=config.download.chunk_size,
        user_agent=config.download.user_agent,
    )

    manifest_rows: list[dict] = []

    console.print(f"[cyan]Run:[/] {run_id}")
    console.print(f"[cyan]Região:[/] {region.name} | {region.description}")
    console.print(f"[cyan]Frames previstos:[/] {len(frame_specs)}")
    console.print(f"[cyan]Área temporária:[/] {run_paths.temp_session_root}")

    mp4_path: Path | None = None
    gif_path: Path | None = None
    mp4_result: FFmpegEncodeResult | None = None

    overall_progress, phase_progress = _build_progress_layout()
    layout_group = Group(overall_progress, phase_progress)

    try:
        with Live(layout_group, console=console, refresh_per_second=8):
            overall_task = overall_progress.add_task(
                "Baixando e recortando...",
                total=max(len(frame_specs), 1),
                details=_build_overall_details(
                    stats,
                    phase="Inicializando",
                    current_frame_label="-",
                ),
            )

            phase_task = phase_progress.add_task(
                "Fase atual",
                total=1,
                completed=0,
                visible=False,
                details="Aguardando...",
            )

            # -----------------------------------------------------------------
            # [6.1] LOOP PRINCIPAL: DOWNLOAD + CROP + REGISTRO
            # -----------------------------------------------------------------
            for index, frame_spec in enumerate(frame_specs, start=1):
                cancel_token.raise_if_requested()

                current_frame_label = f"{index}/{len(frame_specs)}"
                frame_timestamp_str = frame_spec.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")

                overall_progress.update(
                    overall_task,
                    details=_build_overall_details(
                        stats,
                        phase=f"Preparando {frame_timestamp_str}",
                        current_frame_label=current_frame_label,
                    ),
                )

                try:
                    if (
                        config.download.verify_head_before_get
                        and not client.head_available(frame_spec.url)
                    ):
                        stats.failed_frames += 1
                        stats.processed_frames += 1

                        overall_progress.update(
                            overall_task,
                            advance=1,
                            details=_build_overall_details(
                                stats,
                                phase="HEAD indisponível",
                                current_frame_label=current_frame_label,
                            ),
                        )
                        continue

                    source_path = run_paths.downloads_dir / frame_spec.filename

                    def _download_callback(progress_info: DownloadProgress) -> None:
                        details = (
                            f"Frame {current_frame_label} | "
                            f"{frame_timestamp_str} | "
                            f"{_format_mb_s(progress_info.speed_bytes_per_s)}"
                        )

                        phase_progress.update(
                            phase_task,
                            description=f"Download: {progress_info.filename}",
                            total=progress_info.total_bytes if progress_info.total_bytes > 0 else 1,
                            completed=progress_info.bytes_downloaded,
                            visible=True,
                            details=details,
                        )

                    download_result = client.download(
                        frame_spec.url,
                        source_path,
                        progress_callback=_download_callback,
                        cancel_check=cancel_token.is_shutdown_requested,
                    )

                    stats.total_downloaded_bytes += download_result.bytes_downloaded
                    stats.total_download_elapsed_s += download_result.elapsed_s

                    cancel_token.raise_if_requested()

                    crop_started_at = time.perf_counter()
                    frame_output_index = stats.successful_frames + 1
                    frame_path = run_paths.frames_dir / f"frame_{frame_output_index:06d}.{config.crop.output_extension}"

                    crop_result = crop_image(
                        source_path=source_path,
                        output_path=frame_path,
                        region=region,
                        jpeg_quality=config.crop.jpeg_quality,
                    )

                    crop_elapsed_s = time.perf_counter() - crop_started_at
                    stats.total_crop_elapsed_s += crop_elapsed_s
                    stats.successful_frames += 1
                    stats.processed_frames += 1

                    manifest_rows.append(
                        {
                            "frame_index": frame_output_index,
                            "timestamp_utc": frame_spec.timestamp_utc.isoformat(),
                            "source_url": frame_spec.url,
                            "source_filename": frame_spec.filename,
                            "cropped_filename": frame_path.name,
                            "crop_width": crop_result.width,
                            "crop_height": crop_result.height,
                            "download_bytes": download_result.bytes_downloaded,
                            "download_elapsed_s": download_result.elapsed_s,
                            "download_avg_speed_bytes_s": download_result.average_speed_bytes_per_s,
                            "crop_elapsed_s": crop_elapsed_s,
                            "region": {
                                "name": region.name,
                                "left": region.left,
                                "top": region.top,
                                "width": region.width,
                                "height": region.height,
                            },
                        }
                    )

                    if config.pipeline.cleanup_downloaded_sources and source_path.exists():
                        source_path.unlink()

                    phase_progress.update(
                        phase_task,
                        visible=False,
                        details="Download concluído.",
                    )

                    overall_progress.update(
                        overall_task,
                        advance=1,
                        details=_build_overall_details(
                            stats,
                            phase=(
                                f"Download {download_result.elapsed_s:.2f}s @ "
                                f"{_format_mb_s(download_result.average_speed_bytes_per_s)}"
                            ),
                            last_crop_s=crop_elapsed_s,
                            current_frame_label=current_frame_label,
                        ),
                    )

                except DownloadCancelledError as exc:
                    raise UserAbortError(str(exc)) from exc

                except Exception as exc:
                    stats.failed_frames += 1
                    stats.processed_frames += 1

                    if 'source_path' in locals() and isinstance(source_path, Path) and source_path.exists():
                        source_path.unlink(missing_ok=True)

                    phase_progress.update(
                        phase_task,
                        visible=False,
                        details="Download interrompido/falhou.",
                    )

                    overall_progress.update(
                        overall_task,
                        advance=1,
                        details=_build_overall_details(
                            stats,
                            phase=f"Falha: {type(exc).__name__}",
                            current_frame_label=current_frame_label,
                        ),
                    )

                    console.print(f"[red]Erro no frame {frame_spec.filename}:[/] {exc}")

                    if config.pipeline.stop_on_first_error:
                        raise

            # -----------------------------------------------------------------
            # [6.2] VALIDAÇÃO PÓS-LOOP
            # -----------------------------------------------------------------
            if stats.successful_frames == 0:
                raise RuntimeError("Nenhum frame foi gerado. Verifique download, crop e região.")

            manifest = {
                "run_id": run_id,
                "frames_expected": stats.expected_frames,
                "frames_generated": stats.successful_frames,
                "failed_frames": stats.failed_frames,
                "download_resolution": config.download.resolution,
                "minutes_back": config.pipeline.minutes_back,
                "step_minutes": config.pipeline.step_minutes,
                "region_name": region.name,
                "rows": manifest_rows,
            }

            _write_manifest(
                run_paths.manifests_dir / "frames_manifest.json",
                manifest,
            )

        # ---------------------------------------------------------------------
        # [6.3] A PARTIR DAQUI, O LIVE DO RICH JÁ TERMINOU
        # ---------------------------------------------------------------------
        # A decisão aqui é deliberada: durante o encode, trocamos a UI do rich
        # por linhas textuais estilo FFmpeg no próprio terminal. Isso deixa a
        # fase final mais transparente e evita a sensação de "congelamento".
        cancel_token.raise_if_requested()

        if config.encode.enabled:
            frames_pattern = str(run_paths.frames_dir / "frame_%06d.jpg")
            base_output_name = f"goes19_{region.name}_{run_id}"

            if config.encode.make_mp4:
                console.print("\n[bold cyan]Iniciando encode MP4...[/]")
                console.print(
                    f"[cyan]Codec preferencial:[/] {config.encode.gpu_codec} | "
                    f"[cyan]Preset GPU:[/] {config.encode.gpu_preset} | "
                    f"[cyan]FPS:[/] {config.encode.fps}"
                )

                mp4_path = run_paths.run_dir / f"{base_output_name}.mp4"
                ffmpeg_style_printer = _make_ffmpeg_style_printer(stats.successful_frames)

                mp4_result = encode_mp4(
                    ffmpeg_path=config.encode.ffmpeg_path,
                    frames_pattern=frames_pattern,
                    output_path=mp4_path,
                    fps=config.encode.fps,
                    prefer_gpu=config.encode.prefer_gpu,
                    gpu_codec=config.encode.gpu_codec,
                    gpu_preset=config.encode.gpu_preset,
                    gpu_rate_control=config.encode.gpu_rate_control,
                    gpu_zero_bitrate=config.encode.gpu_zero_bitrate,
                    cpu_codec=config.encode.cpu_codec,
                    cpu_preset=config.encode.cpu_preset,
                    crf=config.encode.crf,
                    cq=config.encode.cq,
                    cancel_check=cancel_token.is_shutdown_requested,
                    progress_callback=ffmpeg_style_printer,
                )

                console.print(f"[green]MP4 gerado:[/] {mp4_path}")
                console.print(f"[green]Codec usado:[/] {mp4_result.codec_used}")
                console.print(f"[green]Engine usada:[/] {mp4_result.engine_label}")
                console.print(f"[green]Tempo de encode:[/] {mp4_result.elapsed_s:.2f}s")
                console.print(f"[green]Tamanho final:[/] {_format_mb(mp4_result.output_size_bytes)}")

            if config.encode.make_gif:
                console.print("\n[bold cyan]Iniciando encode GIF...[/]")

                gif_path = run_paths.run_dir / f"{base_output_name}.gif"

                encode_gif(
                    ffmpeg_path=config.encode.ffmpeg_path,
                    frames_pattern=frames_pattern,
                    output_path=gif_path,
                    fps=config.encode.fps,
                    cancel_check=cancel_token.is_shutdown_requested,
                )

                console.print(f"[green]GIF gerado:[/] {gif_path}")

        return PipelineResult(
            run_dir=run_paths.run_dir,
            expected_frames=stats.expected_frames,
            frames_generated=stats.successful_frames,
            failed_frames=stats.failed_frames,
            mp4_path=mp4_path,
            gif_path=gif_path,
            total_downloaded_bytes=stats.total_downloaded_bytes,
            total_elapsed_s=stats.total_elapsed_s,
            average_crop_time_s=stats.average_crop_time_s,
            average_download_speed_mb_s=stats.average_download_speed_mb_s,
            mp4_codec_used=mp4_result.codec_used if mp4_result else None,
            mp4_engine_label=mp4_result.engine_label if mp4_result else None,
            mp4_encode_elapsed_s=mp4_result.elapsed_s if mp4_result else 0.0,
            mp4_output_size_bytes=mp4_result.output_size_bytes if mp4_result else 0,
        )

    except FFmpegCancelledError as exc:
        raise UserAbortError(str(exc)) from exc

    finally:
        _cleanup_temp_session_root(run_paths.temp_session_root)


# =============================================================================
# [7] PONTO PÚBLICO DE ENTRADA DO PIPELINE
# =============================================================================
def run_pipeline(
    config: AppConfig,
    *,
    cancel_token: RunCancellationToken | None = None,
) -> PipelineResult:
    """
    Executa o pipeline com ou sem RAM disk.
    """
    effective_cancel_token = cancel_token or RunCancellationToken()

    ramdisk = RamDiskManager(
        cli_path=config.ramdisk.cli_path,
        drive_letter=config.ramdisk.drive_letter,
        size_mb=config.ramdisk.size_mb,
        filesystem=config.ramdisk.filesystem,
        label=config.ramdisk.label,
        quick_format=config.ramdisk.quick_format,
        create_on_start=config.ramdisk.create_on_start,
        mount_timeout_s=config.ramdisk.mount_timeout_s,
        verbose=config.ramdisk.verbose,
        passthrough_output=config.ramdisk.passthrough_output,
        elevate_on_demand=config.ramdisk.elevate_on_demand,
    )

    if config.ramdisk.enabled:
        with ramdisk as ramdisk_root:
            return _run_inner(config, ramdisk_root, effective_cancel_token)

    return _run_inner(config, None, effective_cancel_token)
