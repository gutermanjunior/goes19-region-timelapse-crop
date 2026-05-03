# =============================================================================
# GOES-19 TIME-LAPSE CROP TOOL
# =============================================================================
# Arquivo.......: ramdisk.py
# Projeto.......: timelapse_crop
# Função........: Gerenciar montagem, verificação e desmontagem opcional de um
#                 RAM disk via ImDisk, com possibilidade de elevação pontual
#                 via UAC apenas para os comandos que exigem privilégio.
#
# Papel na arquitetura
# --------------------
# Este módulo encapsula a interação com a ferramenta externa ImDisk.
#
# Em termos práticos, ele é responsável por:
# - montar o RAM disk, se solicitado
# - verificar se o volume realmente ficou disponível para uso do pipeline
# - desmontar o volume ao final da execução, se for temporário
# - elevar somente o mount via UAC quando o processo principal não estiver admin
#
# Decisões de projeto relevantes
# ------------------------------
# 1. O processo principal do Python pode rodar sem privilégios administrativos.
#
# 2. Apenas o mount do ImDisk é elevado sob demanda.
#
# 3. O unmount é executado normalmente, pois no ambiente do usuário ele não
#    exige nova elevação.
#
# 4. A verificação de prontidão do volume é operacional:
#    o teste é criar pasta e arquivo dentro do volume.
#
# Autor...........: Guterman / OpenAI
# Status..........: Em desenvolvimento
# Versão..........: 0.4.0
#
# Histórico
# ---------
# 0.4.0 - Ajustado para elevar apenas o mount, mantendo o unmount em execução
#         normal, e preservando logs e limpeza defensiva.
# 0.3.0 - Reescrito para suportar elevação pontual via UAC usando
#         ShellExecuteExW e correções estruturais.
# 0.2.0 - Versão com logs, polling e desmontagem defensiva.
# =============================================================================

from __future__ import annotations

import ctypes
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes


# =============================================================================
# [1] EXCEÇÃO ESPECÍFICA DO MÓDULO
# =============================================================================
class RamDiskError(RuntimeError):
    """Erro específico da camada de gerenciamento do RAM disk."""


# =============================================================================
# [2] HELPERS DE PLATAFORMA WINDOWS
# =============================================================================
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
INFINITE = 0xFFFFFFFF
ERROR_CANCELLED = 1223


class SHELLEXECUTEINFOW(ctypes.Structure):
    """
    Estrutura utilizada pela API ShellExecuteExW.

    O objetivo aqui é obter um handle de processo para poder:
    - disparar o UAC via verb 'runas'
    - aguardar a conclusão do comando elevado
    - ler o código de saída do processo
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _is_process_elevated() -> bool:
    """
    Indica se o processo Python atual já está elevado.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _close_handle(handle: wintypes.HANDLE | None) -> None:
    """
    Fecha handle de processo do Windows, se existir.
    """
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)


# =============================================================================
# [3] GERENCIADOR DE RAM DISK
# =============================================================================
@dataclass(slots=True)
class RamDiskManager:
    """
    Gerencia o ciclo de vida opcional do RAM disk.
    """

    cli_path: str = "imdisk"
    drive_letter: str = "W:"
    size_mb: int = 2048
    filesystem: str = "ntfs"
    label: str = "GOESRAM"
    quick_format: bool = True
    create_on_start: bool = False
    mount_timeout_s: int = 60
    verbose: bool = True
    passthrough_output: bool = True
    elevate_on_demand: bool = True

    # Estado interno:
    # indica se esta instância foi a responsável por criar o volume atual.
    _mounted_by_this_instance: bool = False

    # -------------------------------------------------------------------------
    # [3.1] PROPRIEDADES BÁSICAS
    # -------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(f"{self.drive_letter}\\")

    # -------------------------------------------------------------------------
    # [3.2] LOG INTERNO
    # -------------------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[ramdisk] {message}")

    # -------------------------------------------------------------------------
    # [3.3] RESOLUÇÃO DO EXECUTÁVEL DO IMDISK
    # -------------------------------------------------------------------------
    def _resolve_cli_path(self) -> str:
        """
        Resolve o caminho do executável do ImDisk.
        """
        resolved = shutil.which(self.cli_path)
        if resolved:
            return resolved

        candidate = Path(self.cli_path)
        if candidate.exists():
            return str(candidate)

        raise RamDiskError(
            f"Não foi possível localizar o executável do ImDisk: {self.cli_path}"
        )

    # -------------------------------------------------------------------------
    # [3.4] EXECUÇÃO NORMAL DE COMANDOS EXTERNOS
    # -------------------------------------------------------------------------
    def _run_command_normal(self, command: list[str]) -> subprocess.CompletedProcess:
        """
        Executa comando sem elevação.
        """
        self._log(f"Executando comando: {' '.join(command)}")

        if self.passthrough_output:
            return subprocess.run(
                command,
                check=False,
                text=True,
            )

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.stdout.strip():
            self._log(f"stdout: {result.stdout.strip()}")

        if result.stderr.strip():
            self._log(f"stderr: {result.stderr.strip()}")

        return result

    # -------------------------------------------------------------------------
    # [3.5] EXECUÇÃO ELEVADA VIA UAC
    # -------------------------------------------------------------------------
    def _run_command_elevated(self, command: list[str]) -> subprocess.CompletedProcess:
        """
        Executa um comando com elevação pontual via UAC.

        Observação importante:
        Neste modo, o stdout/stderr do processo elevado não retorna de forma
        natural ao terminal atual.
        """
        executable = self._resolve_cli_path()
        parameters = subprocess.list2cmdline(command[1:])

        self._log(f"Solicitando elevação UAC para: {executable} {parameters}")

        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = None
        sei.lpVerb = "runas"
        sei.lpFile = executable
        sei.lpParameters = parameters
        sei.lpDirectory = str(Path(executable).parent)
        sei.nShow = SW_SHOWNORMAL
        sei.hInstApp = None

        success = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not success:
            error_code = ctypes.GetLastError()
            if error_code == ERROR_CANCELLED:
                raise RamDiskError("Elevação UAC cancelada pelo usuário.")
            raise ctypes.WinError(error_code)

        try:
            ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, INFINITE)

            exit_code = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                sei.hProcess,
                ctypes.byref(exit_code),
            )
            if not ok:
                raise ctypes.WinError()

            completed = subprocess.CompletedProcess(
                args=command,
                returncode=int(exit_code.value),
                stdout="",
                stderr="",
            )

            self._log(f"Comando elevado finalizado com exit code {completed.returncode}")
            return completed

        finally:
            _close_handle(sei.hProcess)

    # -------------------------------------------------------------------------
    # [3.6] ORQUESTRADOR DE EXECUÇÃO DE COMANDOS
    # -------------------------------------------------------------------------
    def _run_command(
        self,
        command: list[str],
        *,
        elevate: bool,
    ) -> subprocess.CompletedProcess:
        """
        Decide entre execução normal e execução elevada.

        Regra:
        - se elevate=True e o processo atual não estiver admin, solicita UAC
        - caso contrário, executa normalmente
        """
        if elevate and not _is_process_elevated() and self.elevate_on_demand:
            return self._run_command_elevated(command)

        return self._run_command_normal(command)

    # -------------------------------------------------------------------------
    # [3.7] CHECAGEM DE DISPONIBILIDADE DO VOLUME
    # -------------------------------------------------------------------------
    def is_ready(self) -> bool:
        """
        Verifica se o RAM disk está operacional para o pipeline.
        """
        try:
            work_dir = self.root / "goes_timelapse"
            probe_dir = work_dir / "_probe"

            probe_dir.mkdir(parents=True, exist_ok=True)

            probe_file = probe_dir / "probe.txt"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()

            probe_dir.rmdir()
            return True

        except Exception as exc:
            self._log(f"is_ready() ainda falhou: {exc}")
            return False

    # -------------------------------------------------------------------------
    # [3.8] CONSTRUÇÃO DOS COMANDOS
    # -------------------------------------------------------------------------
    def build_mount_command(self) -> list[str]:
        """
        Monta o comando do ImDisk para criação do RAM disk.
        """
        format_parts = [f"/fs:{self.filesystem}", f"/v:{self.label}", "/y"]

        if self.quick_format:
            format_parts.insert(1, "/q")

        return [
            self._resolve_cli_path(),
            "-a",
            "-t",
            "vm",
            "-s",
            f"{self.size_mb}M",
            "-m",
            self.drive_letter,
            "-p",
            " ".join(format_parts),
        ]

    def build_unmount_command(self) -> list[str]:
        """
        Retorna o comando de desmontagem do volume.
        """
        return [
            self._resolve_cli_path(),
            "-D",
            "-m",
            self.drive_letter,
        ]

    # -------------------------------------------------------------------------
    # [3.9] LIMPEZA PREVENTIVA PRÉ-MONTAGEM
    # -------------------------------------------------------------------------
    def _pre_mount_cleanup(self) -> None:
        """
        Tenta limpar preventivamente a letra da unidade antes de nova montagem.
        """
        try:
            self._log(f"Limpeza preventiva da letra {self.drive_letter} antes da montagem...")
            result = self._run_command(self.build_unmount_command(), elevate=False)
            self._log(f"Return code da limpeza preventiva: {result.returncode}")
        except Exception as exc:
            self._log(f"Limpeza preventiva ignorada: {exc}")

    # -------------------------------------------------------------------------
    # [3.10] DESMONTAGEM DEFENSIVA APÓS FALHA DE MOUNT
    # -------------------------------------------------------------------------
    def _safe_unmount_after_failed_mount(self) -> None:
        """
        Tenta desmontar o volume sem lançar nova exceção.
        """
        try:
            self._log("Falha após montagem detectada. Tentando desmontagem defensiva...")
            result = self._run_command(self.build_unmount_command(), elevate=False)
            self._log(f"Return code da desmontagem defensiva: {result.returncode}")
        except Exception as exc:
            self._log(f"Desmontagem defensiva também falhou: {exc}")

    # -------------------------------------------------------------------------
    # [3.11] MONTAGEM DO RAM DISK
    # -------------------------------------------------------------------------
    def mount(self) -> Path:
        """
        Monta o RAM disk e aguarda até que ele esteja operacional.
        """
        if self.is_ready():
            self._log(f"RAM disk já disponível em {self.root}")
            self._mounted_by_this_instance = False
            return self.root

        self._pre_mount_cleanup()

        command = self.build_mount_command()
        self._log(f"Comando de montagem: {' '.join(command)}")

        result = self._run_command(command, elevate=True)
        self._log(f"Return code: {result.returncode}")

        if result.returncode != 0:
            raise RamDiskError("Falha ao montar ramdisk.")

        self._mounted_by_this_instance = True

        try:
            deadline = time.time() + self.mount_timeout_s
            attempt = 0

            while time.time() < deadline:
                attempt += 1
                ready = self.is_ready()
                self._log(f"Polling #{attempt}: ready={ready} | root={self.root}")

                if ready:
                    self._log(f"RAM disk pronto em {self.root}")
                    return self.root

                time.sleep(0.5)

            raise RamDiskError(
                f"Ramdisk {self.drive_letter} não ficou disponível a tempo "
                f"({self.mount_timeout_s}s)."
            )

        except Exception:
            if self._mounted_by_this_instance:
                self._safe_unmount_after_failed_mount()
                self._mounted_by_this_instance = False
            raise

    # -------------------------------------------------------------------------
    # [3.12] DESMONTAGEM NORMAL
    # -------------------------------------------------------------------------
    def unmount(self) -> None:
        """
        Desmonta o volume somente se esta instância o criou.

        Observação:
        No ambiente do usuário, o unmount é feito em execução normal, sem pedir
        uma nova elevação via UAC.
        """
        if not self._mounted_by_this_instance:
            self._log("Nenhum volume temporário criado por esta instância para desmontar.")
            return

        result = self._run_command(self.build_unmount_command(), elevate=False)
        self._log(f"Return code: {result.returncode}")

        if result.returncode != 0:
            raise RamDiskError("Falha ao desmontar ramdisk.")

        self._mounted_by_this_instance = False

    # -------------------------------------------------------------------------
    # [3.13] CONTEXT MANAGER
    # -------------------------------------------------------------------------
    def __enter__(self) -> Path | None:
        """
        Entrada do context manager.
        """
        if self.create_on_start:
            return self.mount()

        if self.is_ready():
            self._mounted_by_this_instance = False
            return self.root

        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        """
        Saída do context manager.
        """
        if not self.create_on_start:
            return

        try:
            self.unmount()
        except Exception as unmount_exc:
            self._log(f"Erro ao desmontar no __exit__: {unmount_exc}")
            if exc_type is None:
                raise
