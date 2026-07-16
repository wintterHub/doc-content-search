from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from .config import SCAN_EXTENSIONS, app_data_dir, resource_dir


class EverythingError(Exception):
    pass


class EverythingProvider:
    def __init__(self) -> None:
        self.vendor_dir = resource_dir() / "vendor" / "everything"
        self.everything_exe = self.vendor_dir / "Everything.exe"
        self.es_exe = self.vendor_dir / "es.exe"
        self.data_dir = app_data_dir() / "FileEnumerator"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def ensure_ready(self) -> None:
        if not self.everything_exe.exists() or not self.es_exe.exists():
            raise EverythingError("缺少快速文件统计组件，无法开始索引。")
        if self._can_query():
            return
        self._start_everything()
        for _ in range(30):
            if self._can_query():
                return
            time.sleep(0.5)
        raise EverythingError("快速文件统计组件未能启动或数据库尚未加载，请稍后重试或以管理员身份运行本程序。")

    def list_candidates(self) -> list[Path]:
        self.ensure_ready()
        with tempfile.NamedTemporaryFile(prefix="doc-search-everything-", suffix=".txt", delete=False) as handle:
            output_path = Path(handle.name)
        try:
            query = "ext:" + ";".join(sorted(ext.lstrip(".") for ext in SCAN_EXTENSIONS))
            result = subprocess.run(
                [
                    str(self.es_exe),
                    "-timeout",
                    "60000",
                    "-export-txt",
                    str(output_path),
                    "/a-d",
                    query,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip() or "快速文件统计失败。"
                raise EverythingError(f"快速文件统计获取文件列表失败：{detail}")
            return self._read_paths(output_path)
        finally:
            output_path.unlink(missing_ok=True)

    def _start_everything(self) -> None:
        # 使用内置文件统计组件读取已建立好的文件名数据库。
        subprocess.Popen(
            [str(self.everything_exe), "-startup"],
            cwd=str(self.vendor_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _can_query(self) -> bool:
        try:
            result = subprocess.run(
                [str(self.es_exe), "-timeout", "3000", "-get-everything-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def _read_paths(self, output_path: Path) -> list[Path]:
        data = output_path.read_bytes()
        for encoding in ("utf-8-sig", "mbcs"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
        return [Path(line.strip()) for line in text.splitlines() if line.strip()]
