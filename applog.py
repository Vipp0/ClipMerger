"""File di log di ClipMerger: tiene traccia di quello che il programma fa e di cosa va storto, in modo da poterlo
guardare dopo (anche dopo un blocco o una chiusura improvvisa).

Dove sta: %LOCALAPPDATA%\\ClipMerger\\logs\\
  clipmerger.log  - avvio, impostazioni, comandi ffmpeg, errori completi, memoria durante i lavori lunghi
                    (si ruota da solo: restano circa gli ultimi 3 MB)
  crash.log       - rapporto dei blocchi "duri" del programma (errori fatali in Python/Qt)

Nel log non finisce mai altro che nomi di file, impostazioni ed errori: niente password né dati personali."""
from __future__ import annotations

import atexit
import ctypes
import faulthandler
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("clipmerger")

SAMPLE_SECONDS = 5  # how often the memory of the running ffmpeg processes is looked at
REPORT_EVERY = 6  # ... and one line is written every REPORT_EVERY samples (i.e. every 30 s)
MAX_COMMAND_CHARS = 8000

_ready = False
_crash_file = None
_tracked: dict[int, dict] = {}  # pid -> {label, started, peak_private, peak_ws}
_lock = threading.Lock()
_lock_file: Path | None = None


# --------------------------------------------------------------------------- where
def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ClipMerger" / "logs"


# --------------------------------------------------------------------------- memory (Windows API via ctypes)
class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemory(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_GB = 1024 ** 3
_PROCESS_QUERY_LIMITED_INFORMATION, _PROCESS_VM_READ = 0x1000, 0x0010
_k32 = None
if sys.platform == "win32":
    try:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.OpenProcess.restype = ctypes.c_void_p
        _k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        _k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        _k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemory), ctypes.c_ulong]
        _k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    except (OSError, AttributeError):
        _k32 = None


def system_memory() -> dict | None:
    """RAM and "committed" memory (RAM + page file) of the whole computer, in bytes."""
    if _k32 is None:
        return None
    st = _MemoryStatus()
    st.dwLength = ctypes.sizeof(st)
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return {"ram_total": st.ullTotalPhys, "ram_free": st.ullAvailPhys,
            "commit_limit": st.ullTotalPageFile, "commit_free": st.ullAvailPageFile}


def process_memory(pid: int | None = None) -> dict | None:
    """private (committed) and working-set (really in RAM) bytes of a process; None if unreadable."""
    if _k32 is None:
        return None
    handle, opened = None, False
    try:
        if pid is None:
            handle = _k32.GetCurrentProcess()
        else:
            handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, 0, pid)
            opened = True
            if not handle:
                return None
        info = _ProcessMemory()
        info.cb = ctypes.sizeof(info)
        if not _k32.K32GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
            return None
        return {"private": info.PrivateUsage, "ws": info.WorkingSetSize, "peak_ws": info.PeakWorkingSetSize}
    finally:
        if opened and handle:
            _k32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if _k32 is None:
        return False
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return False
    _k32.CloseHandle(handle)
    return True


def memory_line() -> str:
    """One line: whole-computer memory plus this program and the tracked ffmpeg processes."""
    parts = []
    sm = system_memory()
    if sm:
        parts.append(f"RAM libera {sm['ram_free'] / _GB:.1f}/{sm['ram_total'] / _GB:.1f} GB")
        used = sm["commit_limit"] - sm["commit_free"]
        parts.append(f"memoria impegnata {used / _GB:.1f}/{sm['commit_limit'] / _GB:.1f} GB")
    own = process_memory()
    if own:
        parts.append(f"ClipMerger {own['private'] / _GB:.2f} GB")
    with _lock:
        tracked = [(pid, dict(t)) for pid, t in _tracked.items()]
    for pid, t in tracked:
        pm = process_memory(pid)
        if pm:
            parts.append(f"{t['label']}#{pid} {pm['private'] / _GB:.2f} GB (in RAM {pm['ws'] / _GB:.2f})")
    return " | ".join(parts) if parts else "memoria non leggibile"


# --------------------------------------------------------------------------- tracking of the ffmpeg processes
def track(pid: int, label: str = "ffmpeg") -> None:
    with _lock:
        _tracked[pid] = {"label": label, "started": time.monotonic(), "peak_private": 0, "peak_ws": 0}


def untrack(pid: int) -> str:
    """Stops following a process; returns a short text with its memory peak ("picco 2.31 GB")."""
    with _lock:
        t = _tracked.pop(pid, None)
    if not t:
        return ""
    peak = max(t["peak_private"], t["peak_ws"])
    return f"picco di memoria {peak / _GB:.2f} GB" if peak else ""


def _sampler():
    n = 0
    while True:
        time.sleep(SAMPLE_SECONDS)
        with _lock:
            pids = list(_tracked)
        if not pids:
            n = 0
            continue
        for pid in pids:
            pm = process_memory(pid)
            if pm:
                with _lock:
                    t = _tracked.get(pid)
                    if t:
                        t["peak_private"] = max(t["peak_private"], pm["private"])
                        t["peak_ws"] = max(t["peak_ws"], pm["peak_ws"])
        n += 1
        if n % REPORT_EVERY == 0:
            log.info("memoria | %s", memory_line())


# --------------------------------------------------------------------------- helpers for the callers
def format_command(cmd: list[str]) -> str:
    text = subprocess.list2cmdline([str(c) for c in cmd])
    return text if len(text) <= MAX_COMMAND_CHARS else text[:MAX_COMMAND_CHARS] + f"... [+{len(text) - MAX_COMMAND_CHARS} caratteri]"


def signed_code(code: int | None) -> int | None:
    """Windows reports exit codes as unsigned (4294967294); -2 is what ffmpeg means."""
    return code - 2 ** 32 if isinstance(code, int) and code >= 2 ** 31 else code


def describe_exception(exc: BaseException) -> str:
    """str(exc) can be empty (MemoryError, for one): then at least the name of the error."""
    return str(exc) or type(exc).__name__


def log_exception(where: str, exc: BaseException) -> None:
    log.error("Errore imprevisto in %s: %s", where, describe_exception(exc), exc_info=(type(exc), exc, exc.__traceback__))


def log_result(where: str, name: str, result) -> None:
    """One line per finished file; errors are always written, with the full text."""
    if getattr(result, "skipped", False):
        log.info("%s | %s | saltato (già presente)", where, name)
    elif getattr(result, "success", False):
        note = getattr(result, "note", None)
        log.info("%s | %s | completato%s", where, name, f" ({note})" if note else "")
    elif getattr(result, "error", None) == "Annullato":
        log.info("%s | %s | annullato", where, name)
    else:
        log.error("%s | %s | ERRORE: %s", where, name, getattr(result, "error", "?"))


def log_batch_start(where: str, description: str, files: int, parallel: int) -> None:
    log.info("%s | avvio: %d file, %d in parallelo | %s", where, files, parallel, description)
    log.info("memoria | %s", memory_line())


# --------------------------------------------------------------------------- setup
def _windows_version() -> str:
    try:
        return platform.platform()
    except Exception:  # noqa: BLE001
        return "?"


def setup(version: str) -> None:
    """Starts the log. Never raises: a program that can't write its log must still run."""
    global _ready, _crash_file, _lock_file
    if _ready:
        return
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / "clipmerger.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        log.propagate = False
        _ready = True
    except Exception:  # noqa: BLE001
        return

    try:
        _install_hooks(folder)
    except Exception:  # noqa: BLE001
        log.exception("impossibile installare i controlli sugli errori")

    frozen = bool(getattr(sys, "frozen", False))
    log.info("=" * 72)
    log.info("ClipMerger %s avviato (%s) | %s | Python %s | %d CPU", version, "eseguibile" if frozen else "da sorgente",
             _windows_version(), platform.python_version(), os.cpu_count() or 0)
    log.info("memoria | %s", memory_line())

    # A previous session that never reached the normal shutdown: crash, forced kill, PC switched off...
    try:
        _lock_file = folder / "sessione_in_corso.txt"
        if _lock_file.exists():
            previous = _lock_file.read_text(encoding="utf-8", errors="replace").strip()
            pid_text = previous.split("|")[0].strip()
            if not (pid_text.isdigit() and _pid_alive(int(pid_text)) and int(pid_text) != os.getpid()):
                log.warning("La sessione precedente NON si è chiusa normalmente (blocco, chiusura forzata o PC spento): %s", previous)
        _lock_file.write_text(f"{os.getpid()} | avviata {time.strftime('%Y-%m-%d %H:%M:%S')} | versione {version}", encoding="utf-8")
        atexit.register(_clean_shutdown)
    except OSError:
        pass

    threading.Thread(target=_sampler, name="log-memoria", daemon=True).start()


def _clean_shutdown() -> None:
    log.info("ClipMerger chiuso normalmente")
    try:
        if _lock_file:
            _lock_file.unlink(missing_ok=True)
    except OSError:
        pass


def _install_hooks(folder: Path) -> None:
    global _crash_file

    def excepthook(exc_type, exc, tb):
        log.critical("Errore non gestito (il programma potrebbe chiudersi)", exc_info=(exc_type, exc, tb))
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:  # noqa: BLE001  (no console in the packaged program)
            pass

    def thread_hook(args):
        log.critical("Errore non gestito nel thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook

    crash = folder / "crash.log"
    try:
        if crash.exists() and crash.stat().st_size > 200_000:
            crash.unlink()
    except OSError:
        pass
    _crash_file = open(crash, "a", encoding="utf-8", buffering=1)
    _crash_file.write(f"\n--- avvio {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    faulthandler.enable(file=_crash_file, all_threads=True)

    # Warnings and errors that Qt itself reports (they usually only go to a console nobody sees).
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def qt_handler(mode, context, message):
            if mode == QtMsgType.QtDebugMsg or mode == QtMsgType.QtInfoMsg:
                return
            level = logging.WARNING if mode == QtMsgType.QtWarningMsg else logging.ERROR
            log.log(level, "Qt: %s", message)

        qInstallMessageHandler(qt_handler)
    except Exception:  # noqa: BLE001
        pass
