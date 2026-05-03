# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: goes_client.py
# Projeto.......: timelapse_crop
# Função........: Fornecer geração de timestamps, construção de URLs do GOES e
#                 download HTTP com callback de progresso e suporte a cancelamento.
#
# Papel na arquitetura
# --------------------
# Este módulo encapsula a camada de aquisição dos arquivos remotos. Em termos
# práticos, ele:
#
# - gera nomes de arquivos GOES a partir do timestamp
# - constrói URLs do produto desejado
# - monta a lista de frames esperados
# - realiza downloads com estatísticas e callback de progresso
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O passo temporal do projeto permanece separado da lógica de rede.
#
# 2. O download aceita callback para telemetria em tempo real.
#
# 3. O download aceita cancel_check para encerramento controlado.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.4.0
#
# Histórico
# ---------
# 0.4.0 - Adicionados callback de progresso, estatísticas de download e suporte
#         a cancelamento cooperativo.
# 0.1.0 - Implementação inicial de geração de specs e download simples.
# =============================================================================

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests


# =============================================================================
# [1] CONSTANTES E UTILITÁRIOS DE TEMPO
# =============================================================================
UTC = getattr(dt, "UTC", dt.timezone.utc)


def utc_now() -> dt.datetime:
    """
    Retorna o horário atual em UTC.
    """
    return dt.datetime.now(UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """
    Garante que o datetime fornecido esteja em UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_step(value_utc: dt.datetime, step_minutes: int) -> dt.datetime:
    """
    Arredonda o timestamp para baixo, respeitando o step configurado.
    """
    value_utc = ensure_utc(value_utc)
    floored_minute = (value_utc.minute // step_minutes) * step_minutes
    return value_utc.replace(minute=floored_minute, second=0, microsecond=0)


# =============================================================================
# [2] ESTRUTURAS DE DADOS
# =============================================================================
@dataclass(frozen=True, slots=True)
class FrameSpec:
    """
    Representa um frame esperado da sequência histórica.
    """

    sequence_index: int
    timestamp_utc: dt.datetime
    filename: str
    url: str


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """
    Estado parcial do download, usado em callbacks de progresso.
    """

    bytes_downloaded: int
    total_bytes: int
    elapsed_s: float
    speed_bytes_per_s: float
    filename: str
    url: str


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """
    Estatísticas finais do download de um arquivo.
    """

    bytes_downloaded: int
    total_bytes: int
    elapsed_s: float
    average_speed_bytes_per_s: float
    filename: str
    url: str


class DownloadCancelledError(RuntimeError):
    """
    Sinaliza cancelamento cooperativo do download.
    """


# =============================================================================
# [3] GERAÇÃO DE NOMES E URLS DO GOES
# =============================================================================
def build_goes_filename(timestamp_utc: dt.datetime, resolution: str) -> str:
    """
    Constrói o nome de arquivo esperado no CDN do GOES.
    """
    timestamp_utc = ensure_utc(timestamp_utc)
    day_of_year = timestamp_utc.strftime("%j")
    formatted = timestamp_utc.strftime(f"%Y{day_of_year}%H%M")
    return f"{formatted}_GOES19-ABI-FD-GEOCOLOR-{resolution}.jpg"


def build_goes_url(base_url: str, timestamp_utc: dt.datetime, resolution: str) -> tuple[str, str]:
    """
    Constrói a URL completa do arquivo GOES e retorna também o filename.
    """
    filename = build_goes_filename(timestamp_utc, resolution)
    return f"{base_url.rstrip('/')}/{filename}", filename


def build_frame_specs(
    *,
    base_url: str,
    resolution: str,
    end_time_utc: dt.datetime,
    minutes_back: int,
    step_minutes: int,
) -> list[FrameSpec]:
    """
    Gera a lista de frames esperados para a janela temporal desejada.
    """
    if minutes_back < 0:
        raise ValueError("minutes_back deve ser >= 0")
    if step_minutes <= 0:
        raise ValueError("step_minutes deve ser > 0")

    end_slot = floor_to_step(end_time_utc, step_minutes)

    specs: list[FrameSpec] = []
    total_steps = minutes_back // step_minutes

    for idx in range(total_steps, -1, -1):
        timestamp_utc = end_slot - dt.timedelta(minutes=idx * step_minutes)
        url, filename = build_goes_url(base_url, timestamp_utc, resolution)
        specs.append(
            FrameSpec(
                sequence_index=len(specs) + 1,
                timestamp_utc=timestamp_utc,
                filename=filename,
                url=url,
            )
        )

    return specs


# =============================================================================
# [4] CLIENTE HTTP DO GOES
# =============================================================================
class GoesClient:
    """
    Cliente HTTP para aquisição de imagens do GOES.

    Responsabilidades:
    - HEAD opcional para disponibilidade
    - download com retry
    - callback de progresso
    - cancelamento cooperativo
    """

    def __init__(
        self,
        *,
        timeout_s: int = 120,
        max_retries: int = 2,
        chunk_size: int = 1024 * 1024,
        user_agent: str = "Guterman-GOES-Timelapse/0.4",
    ) -> None:
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # -------------------------------------------------------------------------
    # [4.1] CONSULTA HEAD
    # -------------------------------------------------------------------------
    def head_available(self, url: str) -> bool:
        """
        Faz uma consulta HEAD para verificar se o recurso está disponível.
        """
        try:
            response = self.session.head(url, timeout=self.timeout_s)
            return response.status_code == 200
        except requests.RequestException:
            return False

    # -------------------------------------------------------------------------
    # [4.2] DOWNLOAD COM PROGRESSO E CANCELAMENTO
    # -------------------------------------------------------------------------
    def download(
        self,
        url: str,
        destination: Path,
        *,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        """
        Faz o download do arquivo para o caminho de destino.

        Recursos adicionais:
        - emite progresso por callback
        - aceita função de cancelamento cooperativo
        - retorna estatísticas finais do download
        """
        destination.parent.mkdir(parents=True, exist_ok=True)

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 2):
            try:
                started_at = time.perf_counter()
                bytes_downloaded = 0

                with self.session.get(url, stream=True, timeout=self.timeout_s) as response:
                    response.raise_for_status()
                    total_bytes = int(response.headers.get("content-length", "0") or 0)

                    with destination.open("wb") as file_handle:
                        for chunk in response.iter_content(chunk_size=self.chunk_size):
                            if cancel_check and cancel_check():
                                raise DownloadCancelledError(
                                    "Download cancelado por solicitação do usuário."
                                )

                            if not chunk:
                                continue

                            file_handle.write(chunk)
                            bytes_downloaded += len(chunk)

                            elapsed_s = max(time.perf_counter() - started_at, 1e-9)
                            speed_bps = bytes_downloaded / elapsed_s

                            if progress_callback is not None:
                                progress_callback(
                                    DownloadProgress(
                                        bytes_downloaded=bytes_downloaded,
                                        total_bytes=total_bytes,
                                        elapsed_s=elapsed_s,
                                        speed_bytes_per_s=speed_bps,
                                        filename=destination.name,
                                        url=url,
                                    )
                                )

                elapsed_s = max(time.perf_counter() - started_at, 1e-9)
                average_speed_bps = bytes_downloaded / elapsed_s

                return DownloadResult(
                    bytes_downloaded=bytes_downloaded,
                    total_bytes=total_bytes,
                    elapsed_s=elapsed_s,
                    average_speed_bytes_per_s=average_speed_bps,
                    filename=destination.name,
                    url=url,
                )

            except DownloadCancelledError:
                # Cancelamento cooperativo não deve entrar no ciclo de retry.
                if destination.exists():
                    destination.unlink(missing_ok=True)
                raise

            except requests.RequestException as exc:
                last_error = exc

                # Limpa arquivo parcial, se existir.
                if destination.exists():
                    destination.unlink(missing_ok=True)

                if attempt >= self.max_retries + 1:
                    break

        raise RuntimeError(f"Falha no download: {url}") from last_error
