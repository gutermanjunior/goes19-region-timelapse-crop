# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: config.py
# Projeto.......: timelapse_crop
# Função........: Centralizar as configurações estáticas e operacionais do
#                 pipeline de geração de recortes históricos do GOES-19.
#
# Papel na arquitetura
# --------------------
# Este módulo define a camada de configuração do projeto. Em termos práticos,
# ele concentra os parâmetros que controlam:
#
# - download das imagens do servidor GOES
# - recorte da região de interesse
# - encode final do vídeo/animação
# - uso opcional de RAM disk
# - política de persistência ou descarte de frames temporários
# - comportamento geral do pipeline
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O passo temporal do GOES neste projeto é tratado como fixo em 10 minutos.
#
# 2. O encode preferencial em GPU foi ajustado para HEVC/H.265 com NVENC.
#
# 3. O RAM disk foi configurado para a unidade W:, com 512 MB como padrão
#    atual de teste.
#
# 4. Quando keep_frames_after_encode = False, o pipeline trata os frames
#    cropados como artefatos efêmeros. Se houver RAM disk ativo, eles ficam
#    nele e são descartados ao final.
#
# 5. O main.py pode rodar sem privilégios administrativos. Quando necessário,
#    mount do ImDisk pode solicitar elevação pontual via UAC.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.4.0
#
# Histórico
# ---------
# 0.4.0 - Adicionados parâmetros de telemetria visível, manutenção do uso de
#         UAC sob demanda no ramdisk, e organização para cancelamento
#         controlado do pipeline.
# 0.3.0 - Adicionadas opções de elevação pontual do ImDisk, logs do ramdisk e
#         política de frames efêmeros.
# 0.2.0 - Reestruturação do cabeçalho, fixação explícita do step em 10 min e
#         encode preferencial via HEVC NVENC.
# 0.1.0 - Estrutura inicial com dataclasses básicas de configuração.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# =============================================================================
# [1] CONSTANTES GLOBAIS DO PROJETO
# =============================================================================
GOES_BASE_URL = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR"
DEFAULT_STEP_MINUTES = 10


# =============================================================================
# [2] CONFIGURAÇÕES DE DOWNLOAD
# =============================================================================
@dataclass(slots=True)
class DownloadConfig:
    """
    Configura a camada de download.

    Observação prática:
    A resolução alta altera diretamente o nome do arquivo remoto. Por isso, ela
    faz parte da configuração de obtenção do frame.
    """

    base_url: str = GOES_BASE_URL
    resolution: str = "21696x21696"
    timeout_s: int = 120
    max_retries: int = 2
    chunk_size: int = 1024 * 1024
    verify_head_before_get: bool = False
    user_agent: str = "Guterman-GOES-Timelapse/0.4"


# =============================================================================
# [3] CONFIGURAÇÕES DE RECORTE
# =============================================================================
@dataclass(slots=True)
class CropConfig:
    """
    Configura o comportamento da etapa de crop.
    """

    region_name: str = "sudeste_brasil"
    output_extension: str = "jpg"
    jpeg_quality: int = 92


# =============================================================================
# [4] CONFIGURAÇÕES DE ENCODE
# =============================================================================
@dataclass(slots=True)
class EncodeConfig:
    """
    Configura a geração dos artefatos finais.

    Estratégia adotada:
    - MP4 como formato principal
    - GIF opcional
    - preferência por encode via GPU
    - codec preferencial: HEVC/H.265 via NVENC
    - fallback em CPU via libx265
    """

    enabled: bool = True
    make_mp4: bool = True
    make_gif: bool = False
    ffmpeg_path: str = "ffmpeg"

    # FPS do vídeo final.
    # Exemplo:
    #   fps = 1  -> cada frame é exibido por 1 segundo
    fps: int = 4

    # Estratégia de encode preferencial.
    prefer_gpu: bool = True
    gpu_codec: str = "hevc_nvenc"
    gpu_preset: str = "p5"

    # Fallback em CPU, caso NVENC não esteja disponível.
    cpu_codec: str = "libx265"
    cpu_preset: str = "medium"

    # Controle de qualidade:
    # - cq para NVENC
    # - crf para encode em CPU
    crf: int = 22
    cq: int = 25


# =============================================================================
# [5] CONFIGURAÇÕES DE RAM DISK
# =============================================================================
@dataclass(slots=True)
class RamDiskConfig:
    """
    Configura o uso opcional de RAM disk.

    Papel do RAM disk neste projeto:
    - armazenar downloads temporários grandes (.jpg do GOES)
    - armazenar frames cropados temporários quando eles NÃO forem persistidos

    Observação importante:
    O RAM disk não substitui a RAM do processo ao abrir a imagem. Ele funciona
    como área temporária de armazenamento, não como local onde o processamento
    decodificado "vive".
    """

    enabled: bool = True
    create_on_start: bool = True
    cli_path: str = "imdisk"
    drive_letter: str = "W:"
    size_mb: int = 512
    filesystem: str = "ntfs"
    label: str = "GOESRAM"
    quick_format: bool = True
    mount_timeout_s: int = 60

    # Logs e comportamento operacional do gerenciador.
    verbose: bool = True
    passthrough_output: bool = True

    # Quando o processo atual não estiver elevado, o gerenciador pode pedir
    # elevação pontual via UAC apenas para mount do ImDisk.
    elevate_on_demand: bool = True


# =============================================================================
# [6] CONFIGURAÇÕES DO PIPELINE
# =============================================================================
@dataclass(slots=True)
class PipelineConfig:
    """
    Controla o comportamento geral da execução.

    Pontos importantes:
    - minutes_back define a janela retroativa total
    - step_minutes é fixado em 10 min por regra de negócio
    - keep_frames_after_encode define se os frames cropados serão persistidos
    - cleanup_downloaded_sources define se as imagens-fonte serão apagadas
    - stop_on_first_error define se o pipeline aborta no primeiro erro
    """

    minutes_back: int = 240
    step_minutes: int = DEFAULT_STEP_MINUTES
    keep_frames_after_encode: bool = False
    cleanup_downloaded_sources: bool = True
    stop_on_first_error: bool = False


# =============================================================================
# [7] CONFIGURAÇÃO RAIZ DA APLICAÇÃO
# =============================================================================
@dataclass(slots=True)
class AppConfig:
    """
    Agrega todas as subconfigurações do projeto.
    """

    root_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    encode: EncodeConfig = field(default_factory=EncodeConfig)
    ramdisk: RamDiskConfig = field(default_factory=RamDiskConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)


# =============================================================================
# [8] FÁBRICA DE CONFIGURAÇÃO
# =============================================================================
def load_config() -> AppConfig:
    """
    Retorna a configuração padrão da aplicação.
    """
    return AppConfig()
