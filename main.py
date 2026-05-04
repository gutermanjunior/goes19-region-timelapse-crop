# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: main.py
# Projeto.......: timelapse_crop
# Função........: Servir como ponto de entrada da aplicação, interpretando
#                 argumentos de linha de comando, aplicando overrides sobre a
#                 configuração padrão e disparando a execução do pipeline.
#
# Papel na arquitetura
# --------------------
# Este módulo é a camada de entrada do projeto. Ele não implementa a lógica
# de negócio pesada do pipeline; sua responsabilidade é organizar a interface
# de uso da ferramenta.
#
# Em termos práticos, este arquivo cuida de:
# - definir a CLI
# - converter argumentos em overrides de configuração
# - instalar o encerramento controlado para CTRL+C
# - chamar o pipeline principal
# - imprimir um resumo final da execução
# - exibir mensagens de erro de forma amigável
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O parâmetro step_minutes foi removido da CLI.
#    Motivo: neste projeto, o espaçamento temporal do GOES é tratado como fixo
#    em 10 minutos, então expor isso na interface só adiciona ruído.
#
# 2. O parâmetro fps permanece na CLI.
#    Motivo: fps controla a velocidade visual da animação final.
#
# 3. O main deve ser fino.
#    Motivo: a lógica pesada permanece no pipeline e módulos auxiliares.
#
# 4. CTRL+C foi tratado como pedido de encerramento controlado.
#    Motivo: isso permite limpeza de temporários e desmontagem do RAM disk.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.5.0
#
# Histórico
# ---------
# 0.5.0 - Atualizado o manual da CLI com os valores reais do novo config,
#         incluindo o RAM disk padrão de 2048 MB e o comportamento efetivo da
#         execução sem argumentos.
# 0.4.0 - Adicionados encerramento controlado via CTRL+C, manual da CLI com
#         valores reais do config, e resumo final expandido com estatísticas.
# =============================================================================

from __future__ import annotations

import argparse
import signal
import sys
from typing import Any

from rich.console import Console

from config import AppConfig, load_config
from pipeline import (
    PipelineResult,
    RunCancellationToken,
    UserAbortError,
    run_pipeline,
)


# =============================================================================
# [1] CONSOLE GLOBAL DE SAÍDA
# =============================================================================
console = Console()


# =============================================================================
# [2] GERAÇÃO DO MANUAL DE USO DA CLI
# =============================================================================
def _format_bool_label(value: bool) -> str:
    """
    Converte booleanos em rótulos mais legíveis para o manual da CLI.
    """
    return "Sim" if value else "Não"


def _build_cli_manual(defaults: AppConfig) -> str:
    """
    Monta o manual textual da CLI com base nos valores REAIS do config.py.
    """
    ramdisk_mode = "monta automaticamente um RAM disk temporário" if (
        defaults.ramdisk.enabled and defaults.ramdisk.create_on_start
    ) else (
        "usa um RAM disk já existente" if defaults.ramdisk.enabled else "não usa RAM disk"
    )

    return f"""
===========================================================================
MANUAL DE USO DA CLI
===========================================================================

Este programa gera um timelapse a partir de imagens históricas do GOES-19.

Uso básico:
    python main.py [opções]

---------------------------------------------------------------------------
CONFIGURAÇÕES PADRÃO (SEM ARGUMENTOS)
---------------------------------------------------------------------------

Ao executar:
    python main.py

O programa utiliza integralmente os valores definidos em config.py.

Na prática, hoje os padrões são:

- Janela temporal retroativa:
    {defaults.pipeline.minutes_back} minutos

- Região padrão:
    {defaults.crop.region_name}

- FPS padrão da animação:
    {defaults.encode.fps}

- Geração de MP4:
    {_format_bool_label(defaults.encode.make_mp4)}

- Geração de GIF:
    {_format_bool_label(defaults.encode.make_gif)}

- Encode preferencial via GPU:
    {_format_bool_label(defaults.encode.prefer_gpu)}

- Uso de RAM disk:
    {_format_bool_label(defaults.ramdisk.enabled)}

- Modo do RAM disk:
    {ramdisk_mode}

- Unidade do RAM disk:
    {defaults.ramdisk.drive_letter}

- Tamanho padrão do RAM disk:
    {defaults.ramdisk.size_mb} MB

- Manter frames após o encode:
    {_format_bool_label(defaults.pipeline.keep_frames_after_encode)}

- Limpar imagens-fonte baixadas:
    {_format_bool_label(defaults.pipeline.cleanup_downloaded_sources)}

---------------------------------------------------------------------------
RESUMO
---------------------------------------------------------------------------

Rodar sem argumentos significa:
    "executar com a configuração padrão real do projeto"

Para alterar qualquer comportamento, utilize os parâmetros da CLI.

---------------------------------------------------------------------------
[1] JANELA TEMPORAL E REGIÃO
---------------------------------------------------------------------------

--minutes-back <int>
    Define quantos minutos no passado serão utilizados para montar o
    timelapse, a partir do momento atual.

--region <str>
    Nome da região definida no arquivo regions.py.

---------------------------------------------------------------------------
[2] ANIMAÇÃO / ENCODE
---------------------------------------------------------------------------

--fps <int>
    Define o FPS do vídeo final.
    Controla a velocidade visual da animação.

    Exemplos:
        --fps 1   -> cada frame dura 1 segundo
        --fps 4   -> animação mais rápida

--gif
    Se presente, gera também um GIF além do MP4.

--cpu-only
    Força o encode via CPU, desabilitando uso de GPU.

---------------------------------------------------------------------------
[3] RAM DISK (OPCIONAL)
---------------------------------------------------------------------------

--use-ramdisk
    Usa um RAM disk já existente no sistema.

--mount-ramdisk
    Cria um RAM disk temporário no início da execução e remove ao final.

--ramdisk-size-mb <int>
    Define o tamanho do RAM disk (em MB) quando usado com --mount-ramdisk.

---------------------------------------------------------------------------
EXEMPLOS COMPLETOS
---------------------------------------------------------------------------

Exemplo 1 (básico):
    python main.py --minutes-back 60 --region sudeste_brasil

Exemplo 2 (com controle de velocidade):
    python main.py --minutes-back 180 --region sudeste_brasil --fps 4

Exemplo 3 (gerando GIF também):
    python main.py --minutes-back 120 --region sudeste_brasil --gif

Exemplo 4 (com RAM disk temporário):
    python main.py --minutes-back 240 --region sudeste_brasil --mount-ramdisk --ramdisk-size-mb 2048

---------------------------------------------------------------------------
OBSERVAÇÕES IMPORTANTES
---------------------------------------------------------------------------

- O intervalo temporal das imagens do GOES é fixo em 10 minutos.
  (não configurável via CLI)

- Parâmetros não informados utilizam os valores padrão definidos em config.py.

===========================================================================
""".rstrip()


# =============================================================================
# [3] CONSTRUÇÃO DA INTERFACE DE LINHA DE COMANDO
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    """
    Cria e retorna o parser de argumentos da aplicação.

    Observação importante:
    O argumento --step-minutes foi removido intencionalmente. Neste projeto,
    a grade temporal do GOES é tratada como fixa em 10 minutos.
    """
    defaults = load_config()
    cli_manual = _build_cli_manual(defaults)

    parser = argparse.ArgumentParser(
        description="Gera timelapse de recortes históricos do GOES-19.",
        epilog=cli_manual,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # -------------------------------------------------------------------------
    # [3.1] ARGUMENTOS DE JANELA TEMPORAL E REGIÃO
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--minutes-back",
        type=int,
        help="Quantos minutos para trás buscar a partir do momento atual.",
    )
    parser.add_argument(
        "--region",
        type=str,
        help="Nome da região cadastrada em regions.py.",
    )

    # -------------------------------------------------------------------------
    # [3.2] ARGUMENTOS DE ANIMAÇÃO / ENCODE
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--fps",
        type=int,
        help="FPS do vídeo/animação final. Ex.: 1 = cada frame dura 1 segundo.",
    )
    parser.add_argument(
        "--gif",
        action="store_true",
        help="Gera GIF além do MP4.",
    )
    parser.add_argument(
        "--cpu-only",
        action="store_true",
        help="Desabilita encode via GPU e força fallback em CPU.",
    )

    # -------------------------------------------------------------------------
    # [3.3] ARGUMENTOS DE RAM DISK
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--use-ramdisk",
        action="store_true",
        help="Usa um RAM disk já existente/configurado.",
    )
    parser.add_argument(
        "--mount-ramdisk",
        action="store_true",
        help="Monta e desmonta o RAM disk durante a execução.",
    )
    parser.add_argument(
        "--ramdisk-size-mb",
        type=int,
        help="Tamanho do RAM disk em MB para montagem temporária.",
    )

    return parser


# =============================================================================
# [4] APLICAÇÃO DE OVERRIDES DA CLI SOBRE A CONFIGURAÇÃO BASE
# =============================================================================
def apply_cli_overrides(args: argparse.Namespace) -> AppConfig:
    """
    Carrega a configuração padrão e aplica sobre ela os overrides recebidos via
    linha de comando.
    """
    config = load_config()

    # -------------------------------------------------------------------------
    # [4.1] OVERRIDES DE JANELA TEMPORAL E REGIÃO
    # -------------------------------------------------------------------------
    if args.minutes_back is not None:
        config.pipeline.minutes_back = args.minutes_back

    if args.region is not None:
        config.crop.region_name = args.region

    # -------------------------------------------------------------------------
    # [4.2] OVERRIDES DE ANIMAÇÃO / ENCODE
    # -------------------------------------------------------------------------
    if args.fps is not None:
        config.encode.fps = args.fps

    if args.gif:
        config.encode.make_gif = True

    if args.cpu_only:
        config.encode.prefer_gpu = False

    # -------------------------------------------------------------------------
    # [4.3] OVERRIDES DE RAM DISK
    # -------------------------------------------------------------------------
    if args.use_ramdisk:
        config.ramdisk.enabled = True
        config.ramdisk.create_on_start = False

    if args.mount_ramdisk:
        config.ramdisk.enabled = True
        config.ramdisk.create_on_start = True

    if args.ramdisk_size_mb is not None:
        config.ramdisk.size_mb = args.ramdisk_size_mb

    return config


# =============================================================================
# [5] INSTALAÇÃO DO ENCERRAMENTO CONTROLADO
# =============================================================================
def install_ctrl_c_handler(cancel_token: RunCancellationToken) -> Any:
    """
    Instala um handler de CTRL+C que solicita encerramento controlado na primeira
    interrupção e força KeyboardInterrupt na segunda.
    """
    previous_handler = signal.getsignal(signal.SIGINT)
    interrupt_count = {"count": 0}

    def _handle_sigint(signum, frame) -> None:
        interrupt_count["count"] += 1

        if interrupt_count["count"] == 1:
            cancel_token.request_shutdown(
                "CTRL+C recebido. Encerrando de forma controlada..."
            )
            console.print(
                "\n[yellow]CTRL+C recebido. Encerrando de forma controlada... "
                "pressione CTRL+C novamente para forçar.[/]"
            )
            return

        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)
    return previous_handler


# =============================================================================
# [6] RESUMO FINAL DE EXECUÇÃO
# =============================================================================
def _format_megabytes(value_in_bytes: int) -> str:
    """
    Converte bytes em MB com formatação simples para o resumo final.
    """
    return f"{value_in_bytes / (1024 * 1024):.1f} MB"


def print_execution_summary(result: PipelineResult) -> None:
    """
    Exibe um resumo compacto e estatístico da execução ao final do pipeline.
    """
    console.print("\n[bold green]Execução concluída.[/]")
    console.print(f"[cyan]Frames previstos:[/] {result.expected_frames}")
    console.print(f"[cyan]Frames gerados:[/] {result.frames_generated}")
    console.print(f"[cyan]Frames com falha:[/] {result.failed_frames}")
    console.print(f"[cyan]Dados baixados:[/] {_format_megabytes(result.total_downloaded_bytes)}")
    console.print(f"[cyan]Tempo total:[/] {result.total_elapsed_s:.2f} s")
    console.print(f"[cyan]Tempo médio de crop:[/] {result.average_crop_time_s:.2f} s/frame")
    console.print(
        f"[cyan]Velocidade média de download:[/] "
        f"{result.average_download_speed_mb_s:.2f} MB/s"
    )
    console.print(f"[cyan]Pasta da execução:[/] {result.run_dir}")

    if result.mp4_path:
        console.print(f"[cyan]MP4:[/] {result.mp4_path}")
        if result.mp4_codec_used:
            console.print(f"[cyan]Codec MP4:[/] {result.mp4_codec_used}")
        if result.mp4_engine_label:
            console.print(f"[cyan]Engine MP4:[/] {result.mp4_engine_label}")
        if result.mp4_encode_elapsed_s > 0:
            console.print(f"[cyan]Tempo de encode MP4:[/] {result.mp4_encode_elapsed_s:.2f} s")
        if result.mp4_output_size_bytes > 0:
            console.print(f"[cyan]Tamanho do MP4:[/] {_format_megabytes(result.mp4_output_size_bytes)}")

    if result.gif_path:
        console.print(f"[cyan]GIF:[/] {result.gif_path}")


# =============================================================================
# [7] PONTO DE ENTRADA PRINCIPAL
# =============================================================================
def main() -> int:
    """
    Função principal da aplicação.
    """
    cancel_token = RunCancellationToken()
    previous_sigint_handler = None

    try:
        parser = build_parser()
        args = parser.parse_args()
        config = apply_cli_overrides(args)

        previous_sigint_handler = install_ctrl_c_handler(cancel_token)

        result = run_pipeline(config, cancel_token=cancel_token)

        print_execution_summary(result)
        return 0

    except UserAbortError as exc:
        console.print(f"\n[yellow]{exc}[/]")
        return 130

    except KeyboardInterrupt:
        console.print("\n[yellow]Execução interrompida pelo usuário.[/]")
        return 130

    except Exception as exc:
        console.print(f"\n[bold red]Erro fatal:[/] {exc}")
        return 1

    finally:
        if previous_sigint_handler is not None:
            signal.signal(signal.SIGINT, previous_sigint_handler)


# =============================================================================
# [8] EXECUÇÃO DIRETA DO SCRIPT
# =============================================================================
if __name__ == "__main__":
    sys.exit(main())
