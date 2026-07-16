from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DocContentSearch"
DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".conf",
    ".properties",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
}
MAX_TEXT_BYTES = 80 * 1024 * 1024
UNKNOWN_TEXT_SNIFF_LIMIT = 2 * 1024 * 1024
LARGE_FILE_DELAY_BYTES = 20 * 1024 * 1024
MAX_INDEX_FILE_BYTES = 200 * 1024 * 1024
SCAN_EXTENSIONS = DOC_EXTENSIONS | TEXT_EXTENSIONS


def auto_worker_counts() -> tuple[int, int, int]:
    from .settings import load_settings

    cpu_count = os.cpu_count() or 4
    memory_gb = _memory_gb()
    settings = load_settings()
    extract_workers = settings.text_workers or min(24, max(6, cpu_count))
    tika_workers = settings.document_workers or min(4, max(1, min(cpu_count // 4, memory_gb // 8)))
    scan_workers = min(16, max(4, cpu_count))
    return extract_workers, tika_workers, scan_workers

EXCLUDED_PARTS = {
    "appdata",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system volume information",
    "$recycle.bin",
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    ".cache",
}

EXCLUDED_EXACT_PATHS = set()


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home()))
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return app_data_dir() / "index.db"


def bundled_java_path() -> Path:
    exe = resource_dir() / "vendor" / "jre" / "bin" / "java.exe"
    if exe.exists():
        return exe
    return Path("java")


def tika_server_jar_path() -> Path:
    return resource_dir() / "vendor" / "tika-server-standard.jar"


def is_excluded(path: Path) -> bool:
    from .settings import should_index_path

    return not should_index_path(path)


def is_default_excluded(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    if any(part in EXCLUDED_PARTS for part in parts):
        return True
    return any(_contains_sequence(parts, sequence) for sequence in EXCLUDED_EXACT_PATHS)


def discover_roots() -> list[Path]:
    roots: list[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        if root.exists():
            roots.append(root)
    return roots


def discover_priority_roots() -> list[Path]:
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads",
        Path.home() / "OneDrive",
    ]
    seen = set()
    roots = []
    for path in candidates:
        if path.exists():
            resolved = str(path.resolve()).lower()
            if resolved not in seen:
                seen.add(resolved)
                roots.append(path)
    return roots


def _contains_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    if len(sequence) > len(parts):
        return False
    return any(parts[index : index + len(sequence)] == sequence for index in range(len(parts) - len(sequence) + 1))


def _memory_gb() -> int:
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return max(1, int(status.ullTotalPhys / (1024**3)))
    except Exception:
        return 8
