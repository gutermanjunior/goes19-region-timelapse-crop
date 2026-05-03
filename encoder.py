# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: encoder.py
# Projeto.......: timelapse_crop
# Função........: Gerar artefatos finais via FFmpeg, com suporte a escolha de
#                 codec, cancelamento cooperativo e limpeza de saídas parciais.
#
# Papel na arquitetura
# --------------------
# Este módulo encapsula a etapa de encode do pipeline. Em termos práticos, ele:
#
# - escolhe codec GPU/CPU
# - executa o FFmpeg para MP4
# - executa o FFmpeg para GIF
# - permite cancelamento cooperativo durante o encode
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O encode continua isolado do pipeline.
#
# 2. O pipeline pode interromper encode via cancel_check.
#
# 3. Saídas parciais são removidas em caso de cancelamento.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.4.0
#
# Histórico
# ---------
# 0.4.0 - Adicionado suporte a cancelamento cooperativo durante execução do
#         FFmpeg e limpeza de saídas parciais.
# 0.1.0 - Estrutura inicial de encode MP4/GIF com escolha simples de codec.
# =============================================================================

from __future__ import annotations

import subprocess
import time
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
# [2] EXECUÇÃO DE COMANDOS DO FFMPEG
# =============================================================================
def _run_ffmpeg(
    command: list[str],
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """
    Executa um comando do FFmpeg com possibilidade de cancelamento cooperativo.

    Estratégia:
    - usa subprocess.Popen para permitir polling
    - se cancel_check sinalizar interrupção, tenta encerrar o processo de forma
      controlada e, se necessário, força kill
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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

            return_code = process.poll()
            if return_code is not None:
                break

            time.sleep(0.2)

        stdout, stderr = process.communicate()

        if return_code != 0:
            raise FFmpegError(stderr.strip() or stdout.strip() or "FFmpeg retornou erro sem saída legível.")

    finally:
        # Garante que pipes sejam drenados e recursos sejam liberados.
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


# =============================================================================
# [3] SELEÇÃO DE ENCODER
# =============================================================================
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
# [4] ENCODE DE MP4
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
    cpu_codec: str,
    cpu_preset: str,
    crf: int,
    cq: int,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """
    Gera um MP4 a partir da sequência de frames e retorna o codec utilizado.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    codec = choose_video_codec(
        ffmpeg_path=ffmpeg_path,
        prefer_gpu=prefer_gpu,
        gpu_codec=gpu_codec,
        cpu_codec=cpu_codec,
    )

    command = [
        ffmpeg_path,
        "-y",
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
                "-cq",
                str(cq),
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
        _run_ffmpeg(command, cancel_check=cancel_check)
    except Exception:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise

    return codec


# =============================================================================
# [5] ENCODE DE GIF
# =============================================================================
def encode_gif(
    *,
    ffmpeg_path: str,
    frames_pattern: str,
    output_path: Path,
    fps: int,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """
    Gera um GIF a partir da sequência de frames, usando palettegen/paletteuse.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    palette_path = output_path.with_suffix(".palette.png")

    palettegen_cmd = [
        ffmpeg_path,
        "-y",
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
        _run_ffmpeg(palettegen_cmd, cancel_check=cancel_check)
        _run_ffmpeg(paletteuse_cmd, cancel_check=cancel_check)
    except Exception:
        if palette_path.exists():
            palette_path.unlink(missing_ok=True)
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise
    finally:
        if palette_path.exists():
            palette_path.unlink(missing_ok=True)
