from __future__ import annotations

import time
from pathlib import Path

from .config import app_data_dir

MAX_LOG_BYTES = 5 * 1024 * 1024


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_log(message: str) -> None:
    text = message.replace("\r", " ").replace("\n", " ")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {text}\n"
    path = _current_log_path()
    _ensure_utf8_bom(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def clear_old_logs(days: int = 7) -> int:
    cutoff = time.time() - days * 24 * 60 * 60
    removed = 0
    for path in logs_dir().glob("index-*.log"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _current_log_path() -> Path:
    date = time.strftime("%Y-%m-%d")
    index = 1
    while True:
        suffix = "" if index == 1 else f"-{index}"
        path = logs_dir() / f"index-{date}{suffix}.log"
        if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
            return path
        index += 1


def _ensure_utf8_bom(path: Path) -> None:
    # 给日志文件补 UTF-8 BOM，避免 Windows 上用记事本类工具打开时中文乱码。
    if not path.exists():
        path.write_bytes(b"\xef\xbb\xbf")
        return
    if path.stat().st_size == 0:
        path.write_bytes(b"\xef\xbb\xbf")
        return
    with path.open("rb") as handle:
        head = handle.read(3)
    if head == b"\xef\xbb\xbf":
        return
    data = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + data)
