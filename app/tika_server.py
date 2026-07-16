from __future__ import annotations

import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from itertools import cycle
from pathlib import Path

from .config import auto_worker_counts, bundled_java_path, tika_server_jar_path


class TikaServerError(Exception):
    pass


class TikaServerPool:
    def __init__(self) -> None:
        _extract_workers, tika_workers, _scan_workers = auto_worker_counts()
        self.server_count = tika_workers
        self.processes: list[subprocess.Popen] = []
        self.endpoints: list[str] = []
        self._endpoint_cycle = None
        self._lock = threading.RLock()
        self._started = False

    def extract_text(self, path: Path) -> str:
        self.start()
        endpoint = self._next_endpoint()
        try:
            data = path.read_bytes()
            request = urllib.request.Request(
                f"{endpoint}/tika",
                data=data,
                method="PUT",
                headers={
                    "Accept": "text/plain",
                    "Content-Type": "application/octet-stream",
                    "X-Tika-PDFextractInlineImages": "false",
                },
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                raise TikaServerError("文档内容无法解析，可能已损坏、加密或格式不兼容，已跳过") from exc
            raise TikaServerError(f"文档解析服务处理失败，已跳过：{_clean_error(str(exc))}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            detail = _clean_error(str(exc))
            if "云盘文件尚未下载到本地" in detail:
                raise TikaServerError(detail) from exc
            raise TikaServerError(f"文档解析服务处理失败，已跳过：{detail}") from exc
        if not text:
            if path.suffix.lower() == ".pdf":
                raise TikaServerError("未检测到文本层，可能是扫描版 PDF，已跳过")
            raise TikaServerError("未提取到可搜索文本，已跳过")
        return text

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            jar = tika_server_jar_path()
            if not jar.exists():
                raise TikaServerError("缺少文档解析服务组件，已跳过")
            for index in range(self.server_count):
                port = _free_port()
                endpoint = f"http://127.0.0.1:{port}"
                # 常驻服务避免每个文档重复启动 Java，主要提速 PDF/Office 解析。
                process = subprocess.Popen(
                    [
                        str(bundled_java_path()),
                        "-Xms512m",
                        "-Xmx2g",
                        "-jar",
                        str(jar),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--id",
                        f"doc-search-{index + 1}",
                        "--noFork",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.processes.append(process)
                self.endpoints.append(endpoint)
            self._wait_until_ready()
            self._endpoint_cycle = cycle(self.endpoints)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
            for process in self.processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            _cleanup_tika_processes()
            self.processes.clear()
            self.endpoints.clear()
            self._endpoint_cycle = None
            self._started = False

    def _next_endpoint(self) -> str:
        with self._lock:
            if self._endpoint_cycle is None:
                raise TikaServerError("文档解析服务尚未就绪，已跳过")
            return next(self._endpoint_cycle)

    def _wait_until_ready(self) -> None:
        deadline = time.time() + 45
        waiting = set(self.endpoints)
        while waiting and time.time() < deadline:
            for endpoint in list(waiting):
                if _is_ready(endpoint):
                    waiting.remove(endpoint)
            if waiting:
                time.sleep(0.5)
        if waiting:
            self.stop()
            raise TikaServerError("文档解析服务启动超时，请稍后重试")


_POOL = TikaServerPool()


def shared_tika_pool() -> TikaServerPool:
    return _POOL


def stop_tika_pool() -> None:
    _POOL.stop()


def cleanup_tika_processes() -> None:
    _cleanup_tika_processes()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_ready(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/version", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _clean_error(message: str) -> str:
    text = message.replace("\r", " ").replace("\n", " ").strip()
    if "Invalid argument" in text or "Errno 22" in text:
        return "云盘文件尚未下载到本地，已跳过"
    if len(text) > 120:
        return text[:117] + "..."
    return text or "未知错误"


def _cleanup_tika_processes() -> None:
    if not _is_windows():
        return
    script = r"""
Get-CimInstance Win32_Process | Where-Object {
  $cmd = $_.CommandLine
  $_.Name -eq 'java.exe' -and
    $cmd -like '*tika-server-standard.jar*' -and
    ($cmd -like '*doc-search-*' -or $cmd -like '*doc-content-search*' -or $cmd -like '*DocContentSearch*')
} | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=10,
    )


def _is_windows() -> bool:
    return hasattr(subprocess, "CREATE_NO_WINDOW")
