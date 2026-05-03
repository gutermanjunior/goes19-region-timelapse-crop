# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: paths.py
# Projeto.......: timelapse_crop
# Função........: Organizar e padronizar os caminhos utilizados pelo pipeline,
#                 distinguindo claramente artefatos temporários de saídas finais.
#
# Papel na arquitetura
# --------------------
# Este módulo centraliza a política de caminhos do projeto. Em termos práticos,
# ele define:
#
# - raiz do projeto
# - raiz de output persistente
# - raiz local de temporários
# - caminhos por execução (run)
# - separação entre temporários efêmeros e saídas finais
#
# Decisões de projeto relevantes
# ------------------------------
# 1. Downloads do GOES são sempre tratados como temporários.
#
# 2. Frames cropados podem seguir dois caminhos:
#    - persistentes, quando keep_frames_after_encode = True
#    - efêmeros, quando keep_frames_after_encode = False
#
# 3. Quando existe RAM disk ativo, os temporários vão para ele.
#
# 4. MP4, GIF e manifest JSON permanecem sempre no output persistente do
#    projeto.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.3.0
#
# Histórico
# ---------
# 0.3.0 - Introduzida separação explícita entre temp_session_root e output final,
#         com frames efêmeros podendo ficar no temp_root.
# 0.1.0 - Estrutura inicial simples de caminhos do projeto.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# =============================================================================
# [1] ESTRUTURAS DE CAMINHOS DO PROJETO
# =============================================================================
@dataclass(slots=True, frozen=True)
class ProjectPaths:
    """
    Caminhos estruturais permanentes do projeto.
    """

    root_dir: Path
    output_root: Path
    local_temp_root: Path


@dataclass(slots=True, frozen=True)
class RunPaths:
    """
    Caminhos resolvidos para uma execução específica.

    Convenção:
    - run_dir ............... saída final persistente da execução
    - temp_session_root ..... raiz temporária efêmera da execução
    - downloads_dir ......... JPGs grandes do GOES baixados temporariamente
    - frames_dir ............ frames cropados (temporários ou persistentes)
    - manifests_dir ......... manifest JSON persistente
    """

    run_dir: Path
    temp_session_root: Path
    downloads_dir: Path
    frames_dir: Path
    manifests_dir: Path


# =============================================================================
# [2] CONSTRUÇÃO DOS CAMINHOS BASE DO PROJETO
# =============================================================================
def build_project_paths(root_dir: Path | None = None) -> ProjectPaths:
    """
    Resolve os caminhos base do projeto.
    """
    root = (root_dir or Path(__file__).resolve().parent).resolve()

    return ProjectPaths(
        root_dir=root,
        output_root=root / "output",
        local_temp_root=root / "_temp_local",
    )


# =============================================================================
# [3] CONSTRUÇÃO DOS CAMINHOS DE UMA EXECUÇÃO
# =============================================================================
def build_run_paths(
    project_paths: ProjectPaths,
    run_id: str,
    *,
    temp_root: Path | None = None,
    keep_frames_after_encode: bool,
) -> RunPaths:
    """
    Resolve os caminhos de uma execução específica.

    Regra central:
    - downloads sempre vão para a área temporária
    - frames só ficam em output se o usuário quiser persisti-los
    """
    run_dir = project_paths.output_root / run_id
    effective_temp_root = temp_root if temp_root is not None else project_paths.local_temp_root

    # Diretório temporário exclusivo desta execução.
    temp_session_root = effective_temp_root / run_id
    downloads_dir = temp_session_root / "downloads"

    if keep_frames_after_encode:
        frames_dir = run_dir / "frames"
    else:
        frames_dir = temp_session_root / "frames"

    manifests_dir = run_dir / "manifests"

    return RunPaths(
        run_dir=run_dir,
        temp_session_root=temp_session_root,
        downloads_dir=downloads_dir,
        frames_dir=frames_dir,
        manifests_dir=manifests_dir,
    )


# =============================================================================
# [4] GARANTIA DE CRIAÇÃO DE DIRETÓRIOS
# =============================================================================
def ensure_dirs(*directories: Path) -> None:
    """
    Cria todos os diretórios necessários, de forma idempotente.
    """
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
