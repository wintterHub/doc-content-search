from __future__ import annotations

import zipfile
from xml.etree import ElementTree
from pathlib import Path

from .config import DOC_EXTENSIONS, MAX_TEXT_BYTES, TEXT_EXTENSIONS, UNKNOWN_TEXT_SNIFF_LIMIT
from .settings import skip_large_bytes
from .tika_server import TikaServerError, shared_tika_pool


class ExtractError(Exception):
    pass


class SkipFileError(ExtractError):
    pass


class DocumentExtractor:
    def extract(self, path: Path) -> str:
        suffix = path.suffix.lower()
        self._reject_huge_file(path)
        if suffix in DOC_EXTENSIONS:
            return self._extract_with_tika(path)
        return self._extract_plain_text(path)

    def is_supported_candidate(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in DOC_EXTENSIONS or suffix in TEXT_EXTENSIONS:
            return True
        try:
            if path.stat().st_size > UNKNOWN_TEXT_SNIFF_LIMIT:
                return False
        except OSError:
            return False
        return self._looks_like_text(path)

    def _extract_with_tika(self, path: Path) -> str:
        if path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            text = self._extract_open_xml(path)
            if text:
                return text
        try:
            return shared_tika_pool().extract_text(path)
        except TikaServerError as exc:
            message = str(exc)
            if "云盘文件尚未下载到本地" in message or "未检测到文本层" in message or "未提取到可搜索文本" in message:
                raise SkipFileError(message) from exc
            raise ExtractError(message) from exc

    def _reject_huge_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > skip_large_bytes():
            raise SkipFileError(f"文件过大，已跳过（{_format_size(size)}）")

    def _extract_plain_text(self, path: Path) -> str:
        try:
            data = path.read_bytes()
        except OSError as exc:
            if exc.errno == 22:
                raise SkipFileError("云盘文件尚未下载到本地，已跳过") from exc
            raise
        if len(data) > MAX_TEXT_BYTES:
            raise SkipFileError("文件过大，已跳过")
        if data.startswith(b"\xff\xfe"):
            return data[2:].decode("utf-16-le", errors="replace")
        if data.startswith(b"\xfe\xff"):
            return data[2:].decode("utf-16-be", errors="replace")
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8", errors="replace")
        return data.decode("utf-8", errors="replace")

    def _extract_open_xml(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                if path.suffix.lower() == ".docx":
                    return _join_text(_xml_texts(archive, ("word/",), ".xml"))
                if path.suffix.lower() == ".xlsx":
                    return _join_text(_xml_texts(archive, ("xl/sharedStrings.xml", "xl/worksheets/"), ".xml"))
                if path.suffix.lower() == ".pptx":
                    return _join_text(_xml_texts(archive, ("ppt/slides/",), ".xml"))
        except OSError as exc:
            if exc.errno == 22:
                raise SkipFileError("云盘文件尚未下载到本地，已跳过") from exc
        except (zipfile.BadZipFile, ElementTree.ParseError):
            return ""
        return ""

    def _looks_like_text(self, path: Path) -> bool:
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                return False
            sample = path.read_bytes()[:4096]
        except OSError:
            return False
        if not sample:
            return False
        controls = sum(1 for b in sample if b < 9 or (13 < b < 32))
        return controls / max(len(sample), 1) < 0.08


def _xml_texts(archive: zipfile.ZipFile, prefixes: tuple[str, ...], suffix: str) -> list[str]:
    values: list[str] = []
    for name in archive.namelist():
        if not name.endswith(suffix):
            continue
        if not any(name == prefix or name.startswith(prefix) for prefix in prefixes):
            continue
        root = ElementTree.fromstring(archive.read(name))
        for node in root.iter():
            if node.text and node.text.strip():
                values.append(node.text.strip())
    return values


def _join_text(values: list[str]) -> str:
    return " ".join(values).strip()


def _format_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MB"
