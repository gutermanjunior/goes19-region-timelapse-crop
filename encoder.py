# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: encoder.py
# Projeto.......: timelapse_crop
# Função........: Gerar artefatos finais via FFmpeg, com suporte a escolha de
#                 codec, cancelamento cooperativo, telemetria estruturada e
#                 saída textual estilo FFmpeg durante a fase de encode.
#
# Papel na arquitetura
# --------------------
# Este módulo encapsula a etapa de encode do pipeline. Em termos práticos, ele:
#
# - escolhe codec GPU/CPU
# - executa o FFmpeg para MP4
# - executa o FFmpeg para GIF
# - permite cancelamento cooperativo durante o encode
# - expõe progresso detalhado do FFmpeg ao pipeline
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O encode continua isolado do pipeline.
#
# 2. O pipeline pode interromper encode via cancel_check.
#
# 3. Saídas parciais são removidas em caso de cancelamento ou falha.
#
# 4. O progresso do FFmpeg é lido via "-progress pipe:1", evitando depender
#    apenas do texto humano do stderr e permitindo telemetria estruturada.
#
# 5. O pipeline pode decidir exibir esse progresso como linhas "quase cruas"
#    no terminal, imitando a experiência de rodar o FFmpeg manualmente.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.6.0
#
# Histórico
# ---------
# 0.6.0 - Mantida a telemetria estruturada, com foco em integração limpa com
#         o pipeline para exibição de progresso textual estilo FFmpeg após o
#         término do Live do rich.
# 0.5.0 - Adicionado parsing estruturado do progresso do FFmpeg e alinhamento
#         do comando NVENC ao teste manual validado pelo usuário.
# 0.4.0 - Adicionado suporte a cancelamento cooperativo durante execução do
#         FFmpeg e limpeza de saídas parciais.
# =============================================================================

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# =============================================================================
# [1] EXCEÇÕES ESPECÍFICAS
# =============================================================================
class FFmpegError(RuntimeError):
    """
    Erro genérico da camada de encode via FFmpeg.
    """


class FFmpegCancelledError(RuntimeError):
    """
    Sinaliza cancelamento cooperativo de um encode em andamento.
    """


# =============================================================================
# [2] ESTRUTURAS DE TELEMETRIA
# =============================================================================
@dataclass(slots=True)
class FFmpegProgress:
    """
    Estrutura de telemetria incremental do FFmpeg.
    """

    frame: int = 0
    fps: float | None = None
    bitrate: str = "N/A"
    total_size_bytes: int = 0
    out_time_seconds: float = 0.0
    speed: str = "N/A"
    progress_state: str = "continue"
    codec_used: str = ""
    engine_label: str = ""


@dataclass(slots=True)
class FFmpegEncodeResult:
    """
    Resultado consolidado de um encode.
    """

    codec_used: str
    engine_label: str
    elapsed_s: float
    output_size_bytes: int
    last_frame: int
    last_speed: str
    last_fps: float | None


# =============================================================================
# [3] UTILITÁRIOS INTERNOS
# =============================================================================
def _parse_float(value: str) -> float | None:
    """
    Tenta converter string em float, retornando None se não for possível.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_out_time_to_seconds(value: str) -> float:
    """
    Converte "HH:MM:SS.micro" em segundos.
    """
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return 0.0


def _read_pipe_to_queue(pipe, target_queue: queue.Queue, stream_name: str) -> None:
    """
    Lê uma pipe de subprocesso linha a linha e empilha o conteúdo em uma queue.

    Motivo:
    Isso evita bloqueio da thread principal durante o encode.
    """
    try:
        for raw_line in iter(pipe.readline, ""):
            target_queue.put((stream_name, raw_line.rstrip("\r\n")))
    finally:
        pipe.close()


def encoder_available(ffmpeg_path: str, encoder_name: str) -> bool:
    """
    Verifica se um encoder específico está disponível no FFmpeg instalado.
    """
    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return False

    return encoder_name in result.stdout


def choose_video_codec(
    ffmpeg_path: str,
    prefer_gpu: bool,
    gpu_codec: str,
    cpu_codec: str,
) -> str:
    """
    Escolhe entre o codec GPU preferencial e o fallback em CPU.
    """
    if prefer_gpu and encoder_available(ffmpeg_path, gpu_codec):
        return gpu_codec

    return cpu_codec


# =============================================================================
# [4] EXECUÇÃO DE COMANDOS DO FFMPEG COM PROGRESSO
# =============================================================================
def _run_ffmpeg(
    command: list[str],
    *,
    codec_used: str,
    engine_label: str,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[FFmpegProgress], None] | None = None,
) -> FFmpegEncodeResult:
    """
    Executa um comando do FFmpeg com:
    - progresso em tempo real
    - cancelamento cooperativo
    - coleta de resultado final
    """
    started_at = time.perf_counter()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    output_queue: queue.Queue = queue.Queue()
    stderr_lines: list[str] = []

    stdout_thread = threading.Thread(
        target=_read_pipe_to_queue,
        args=(process.stdout, output_queue, "stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_pipe_to_queue,
        args=(process.stderr, output_queue, "stderr"),
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()

    progress_state: dict[str, str] = {}
    last_progress = FFmpegProgress(
        codec_used=codec_used,
        engine_label=engine_label,
    )

    try:
        while True:
            if cancel_check and cancel_check():
                process.terminate()

                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

                raise FFmpegCancelledError("Encode cancelado por solicitação do usuário.")

            try:
                stream_name, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and output_queue.empty():
                    break
                continue

            if stream_name == "stderr":
                if line:
                    stderr_lines.append(line)
                continue

            if not line or "=" not in line:
                continue

            key, value = line.split("=", 1)
            progress_state[key] = value

            if key == "progress":
                current_progress = FFmpegProgress(
                    frame=int(progress_state.get("frame", "0") or "0"),
                    fps=_parse_float(progress_state.get("fps", "")),
                    bitrate=progress_state.get("bitrate", "N/A"),
                    total_size_bytes=int(progress_state.get("total_size", "0") or "0"),
                    out_time_seconds=_parse_out_time_to_seconds(
                        progress_state.get("out_time", "00:00:00.0")
                    ),
                    speed=progress_state.get("speed", "N/A"),
                    progress_state=value,
                    codec_used=codec_used,
                    engine_label=engine_label,
                )

                last_progress = current_progress

                if progress_callback:
                    progress_callback(current_progress)

                if value == "end":
                    progress_state = {}

        return_code = process.wait()

        if return_code != 0:
            stderr_text = "\n".join(line for line in stderr_lines if line)
            raise FFmpegError(
                stderr_text.strip() or "FFmpeg retornou erro sem saída legível."
            )

        elapsed_s = time.perf_counter() - started_at

        return FFmpegEncodeResult(
            codec_used=codec_used,
            engine_label=engine_label,
            elapsed_s=elapsed_s,
            output_size_bytes=last_progress.total_size_bytes,
            last_frame=last_progress.frame,
            last_speed=last_progress.speed,
            last_fps=last_progress.fps,
        )

    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)


# =============================================================================
# [5] ENCODE DE MP4
# =============================================================================
def encode_mp4(
    *,
    ffmpeg_path: str,
    frames_pattern: str,
    output_path: Path,
    fps: int,
    prefer_gpu: bool,
    gpu_codec: str,
    gpu_preset: str,
    gpu_rate_control: str,
    gpu_zero_bitrate: bool,
    cpu_codec: str,
    cpu_preset: str,
    crf: int,
    cq: int,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[FFmpegProgress], None] | None = None,
) -> FFmpegEncodeResult:
    """
    Gera um MP4 a partir da sequência de frames e retorna resultado detalhado.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    codec = choose_video_codec(
        ffmpeg_path=ffmpeg_path,
        prefer_gpu=prefer_gpu,
        gpu_codec=gpu_codec,
        cpu_codec=cpu_codec,
    )

    engine_label = "GPU/NVENC" if codec == gpu_codec else "CPU"

    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-framerate",
        str(fps),
        "-i",
        frames_pattern,
    ]

    if codec == gpu_codec:
        command.extend(
            [
                "-c:v",
                codec,
                "-preset",
                gpu_preset,
                "-rc:v",
                gpu_rate_control,
                "-cq:v",
                str(cq),
            ]
        )

        if gpu_zero_bitrate:
            command.extend(["-b:v", "0"])

        command.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
    else:
        command.extend(
            [
                "-c:v",
                codec,
                "-preset",
                cpu_preset,
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )

    try:
        return _run_ffmpeg(
            command,
            codec_used=codec,
            engine_label=engine_label,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    except Exception:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise


# =============================================================================
# [6] ENCODE DE GIF
# =============================================================================
def encode_gif(
    *,
    ffmpeg_path: str,
    frames_pattern: str,
    output_path: Path,
    fps: int,
    cancel_check: Callable[[], bool] | None = None,
) -> FFmpegEncodeResult:
    """
    Gera um GIF a partir da sequência de frames, usando palettegen/paletteuse.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    palette_path = output_path.with_suffix(".palette.png")

    palettegen_cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-framerate",
        str(fps),
        "-i",
        frames_pattern,
        "-vf",
        "palettegen",
        str(palette_path),
    ]

    paletteuse_cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:1",
        "-framerate",
        str(fps),
        "-i",
        frames_pattern,
        "-i",
        str(palette_path),
        "-lavfi",
        "paletteuse",
        str(output_path),
    ]

    try:
        _run_ffmpeg(
            palettegen_cmd,
            codec_used="palettegen",
            engine_label="CPU",
            cancel_check=cancel_check,
            progress_callback=None,
        )
        return _run_ffmpeg(
            paletteuse_cmd,
            codec_used="gif",
            engine_label="CPU",
            cancel_check=cancel_check,
            progress_callback=None,
        )
    except Exception:
        if palette_path.exists():
            palette_path.unlink(missing_ok=True)
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise
    finally:
        if palette_path.exists():
            palette_path.unlink(missing_ok=True)
