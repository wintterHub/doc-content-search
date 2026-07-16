from __future__ import annotations

import sqlite3
import threading
import time
import hashlib
import re
import zlib
from pathlib import Path

from .config import database_path
from .settings import load_settings, sqlite_cache_kb

SCHEMA_VERSION = "3"
PREVIEW_LIMIT = 1600
SNIPPET_RADIUS = 90


class IndexStore:
    def __init__(self) -> None:
        self.path = database_path()
        _remove_legacy_database(self.path)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.lock = threading.RLock()
        self.metadata: dict[str, tuple[int, float, str]] = {}
        self.conn.execute("pragma busy_timeout=30000")
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.execute("pragma temp_store=memory")
        self.conn.execute(f"pragma cache_size={sqlite_cache_kb()}")
        self.conn.execute("pragma mmap_size=536870912")
        self._init_schema()
        self._cleanup_orphan_fts()
        self._normalize_non_retryable_tasks()
        self._load_metadata()

    def apply_runtime_settings(self) -> None:
        # 缓存大小属于连接级参数，保存高级配置后需要重新应用到当前连接。
        with self.lock:
            self.conn.execute(f"pragma cache_size={sqlite_cache_kb()}")

    def checkpoint(self) -> None:
        # 退出前收缩 WAL，避免大量写入后 index.db-wal 长时间占用磁盘。
        with self.lock:
            self.conn.execute("pragma wal_checkpoint(truncate)")

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            create table if not exists app_meta (
                key text primary key,
                value text not null
            )
            """
        )
        self.conn.execute(
            """
            create table if not exists files (
                id integer primary key,
                path text not null unique,
                extension text not null,
                size integer not null,
                modified_at real not null,
                preview text not null default '',
                content_cache blob not null default x''
            )
            """
        )
        self._ensure_column("files", "fingerprint", "text not null default ''")
        self.conn.execute(
            """
            create table if not exists index_tasks (
                path text primary key,
                size integer not null,
                modified_at real not null,
                fingerprint text not null,
                status text not null,
                updated_at real not null,
                error text not null default ''
            )
            """
        )
        self.conn.execute(
            """
            create virtual table if not exists file_fts using fts5(
                content,
                content='',
                contentless_delete=1,
                tokenize='unicode61'
            )
            """
        )
        self.conn.execute(
            "insert or replace into app_meta(key, value) values('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def _load_metadata(self) -> None:
        # 将已索引文件元数据放入内存，重建索引时避免逐个候选文件查询 SQLite。
        with self.lock:
            rows = self.conn.execute("select path, size, modified_at, fingerprint from files").fetchall()
            self.metadata = {row[0]: (row[1], row[2], row[3]) for row in rows}

    def indexed_count(self) -> int:
        return len(self.metadata)

    def indexed_paths(self) -> list[Path]:
        return [Path(path) for path in self.metadata.keys()]

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"alter table {table} add column {column} {definition}")

    def upsert(self, path: Path, content: str) -> None:
        stat = path.stat()
        self.upsert_snapshot(path, content, stat.st_size, stat.st_mtime, fingerprint_for(path, stat.st_size, stat.st_mtime))

    def upsert_snapshot(self, path: Path, content: str, size: int, modified_at: float, fingerprint: str) -> None:
        text_path = str(path)
        with self.lock, self.conn:
            self._delete_fts_by_path(text_path)
            self.conn.execute("delete from files where path = ?", (text_path,))
            self.conn.execute(
                """
                insert into files(path, extension, size, modified_at, preview, content_cache, fingerprint)
                values(?,?,?,?,?,?,?)
                """,
                (
                    text_path,
                    path.suffix.lower().lstrip("."),
                    size,
                    modified_at,
                    _preview_text(content),
                    _compressed_content(content),
                    fingerprint,
                ),
            )
            rowid = self.conn.execute("select id from files where path = ?", (text_path,)).fetchone()[0]
            self.conn.execute("insert into file_fts(rowid, content) values(?,?)", (rowid, content))
            self.metadata[text_path] = (size, modified_at, fingerprint)
            self.conn.execute("delete from index_tasks where path = ?", (text_path,))

    def apply_batch(self, items: list[tuple[str, Path, str | None, int, float, str]]) -> tuple[int, int]:
        indexed = 0
        deleted = 0
        with self.lock, self.conn:
            for action, path, content, size, modified_at, fingerprint in items:
                text_path = str(path)
                self._delete_fts_by_path(text_path)
                self.conn.execute("delete from files where path = ?", (text_path,))
                if action == "delete":
                    self.metadata.pop(text_path, None)
                    self.conn.execute("delete from index_tasks where path = ?", (text_path,))
                    deleted += 1
                    continue
                if content is None:
                    self.metadata.pop(text_path, None)
                    self._set_task(text_path, size, modified_at, fingerprint, "skipped", "")
                    continue
                self.conn.execute(
                    """
                    insert into files(path, extension, size, modified_at, preview, content_cache, fingerprint)
                    values(?,?,?,?,?,?,?)
                    """,
                    (
                        text_path,
                        path.suffix.lower().lstrip("."),
                        size,
                        modified_at,
                        _preview_text(content),
                        _compressed_content(content),
                        fingerprint,
                    ),
                )
                rowid = self.conn.execute("select id from files where path = ?", (text_path,)).fetchone()[0]
                self.conn.execute("insert into file_fts(rowid, content) values(?,?)", (rowid, content))
                self.metadata[text_path] = (size, modified_at, fingerprint)
                self.conn.execute("delete from index_tasks where path = ?", (text_path,))
                indexed += 1
        return indexed, deleted

    def delete(self, path: Path) -> None:
        text_path = str(path)
        with self.lock, self.conn:
            self._delete_fts_by_path(text_path)
            self.conn.execute("delete from files where path = ?", (text_path,))
            self.conn.execute("delete from index_tasks where path = ?", (text_path,))
            self.metadata.pop(text_path, None)

    def needs_index(self, path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        return self.needs_index_snapshot(path, stat.st_size, stat.st_mtime)

    def needs_index_snapshot(self, path: Path, size: int, modified_at: float) -> bool:
        row = self.metadata.get(str(path))
        if row is None:
            return True
        # 复用判断只使用文件系统元数据，不读取文件内容，保证启动补扫尽可能快。
        return row[0] != size or abs(row[1] - modified_at) > 0.001

    def pending_tasks(self) -> list[Path]:
        with self.lock:
            rows = self.conn.execute(
                "select path from index_tasks where status in ('pending','processing','failed_retryable') order by updated_at"
            ).fetchall()
        return [Path(row[0]) for row in rows]

    def mark_task_pending(self, path: Path, size: int, modified_at: float, fingerprint: str) -> None:
        with self.lock, self.conn:
            self._set_task(str(path), size, modified_at, fingerprint, "pending", "")

    def mark_task_error(self, path: Path, error: str, retryable: bool = False) -> None:
        status = "failed_retryable" if retryable else "failed"
        try:
            stat = path.stat()
            size = stat.st_size
            modified_at = stat.st_mtime
            fingerprint = fingerprint_for(path, size, modified_at)
        except OSError:
            size = 0
            modified_at = 0.0
            fingerprint = ""
        with self.lock, self.conn:
            self._set_task(str(path), size, modified_at, fingerprint, status, error)

    def mark_task_skipped(self, path: Path, reason: str) -> None:
        try:
            stat = path.stat()
            size = stat.st_size
            modified_at = stat.st_mtime
            fingerprint = fingerprint_for(path, size, modified_at)
        except OSError:
            size = 0
            modified_at = 0.0
            fingerprint = ""
        with self.lock, self.conn:
            self._set_task(str(path), size, modified_at, fingerprint, "skipped", reason)

    def clear_all(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("delete from files")
            self.conn.execute("delete from file_fts")
            self.conn.execute("delete from index_tasks")
            self.metadata.clear()

    def _set_task(self, path: str, size: int, modified_at: float, fingerprint: str, status: str, error: str) -> None:
        self.conn.execute(
            """
            insert into index_tasks(path, size, modified_at, fingerprint, status, updated_at, error)
            values(?,?,?,?,?,?,?)
            on conflict(path) do update set
                size=excluded.size,
                modified_at=excluded.modified_at,
                fingerprint=excluded.fingerprint,
                status=excluded.status,
                updated_at=excluded.updated_at,
                error=excluded.error
            """,
            (path, size, modified_at, fingerprint, status, time.time(), error),
        )

    def search(self, query: str, limit: int = 40, offset: int = 0, path_filter: str = "") -> list[dict]:
        if not query.strip():
            return []
        where = "where file_fts match ?"
        params: list[object] = [query]
        if path_filter.strip():
            where += " and files.path like ? escape '\\' collate nocase"
            params.append(f"%{_escape_like(path_filter.strip())}%")
        sql = """
            select files.path, files.extension, files.modified_at,
                   files.content_cache, files.preview,
                   bm25(file_fts) as score
            from file_fts
            join files on files.id = file_fts.rowid
            {where}
            order by score
            limit ? offset ?
        """.format(where=where)
        params.extend([limit, offset])
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "path": row[0],
                "extension": row[1],
                "modified_at": row[2],
                "snippet": _snippet_from_cache(row[3], row[4], query),
                "score": row[5],
            }
            for row in rows
        ]

    def _delete_fts_by_path(self, path: str) -> None:
        row = self.conn.execute("select id from files where path = ?", (path,)).fetchone()
        if row is not None:
            self.conn.execute("delete from file_fts where rowid = ?", (row[0],))

    def _cleanup_orphan_fts(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("delete from file_fts where rowid not in (select id from files)")

    def _normalize_non_retryable_tasks(self) -> None:
        # 历史版本会把“已跳过”的解析失败保存为可重试，启动后会反复处理同一批文件。
        with self.lock, self.conn:
            self.conn.execute(
                """
                update index_tasks
                set status = 'skipped'
                where status = 'failed_retryable'
                  and (
                      error like '%已跳过%'
                      or error like '%文档内容无法解析%'
                      or error like '%可能已损坏%'
                      or error like '%格式不兼容%'
                  )
                """
            )


def fingerprint_for(path: Path, size: int | None = None, modified_at: float | None = None) -> str:
    try:
        stat = path.stat()
        size = stat.st_size if size is None else size
        modified_ns = getattr(stat, "st_mtime_ns", int((modified_at or stat.st_mtime) * 1_000_000_000))
    except OSError:
        size = size or 0
        modified_ns = int((modified_at or 0) * 1_000_000_000)
    base = f"{size}:{modified_ns}"
    if not load_settings().use_sample_hash:
        return base
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
            if size and size > 4096:
                handle.seek(max(size - 4096, 0))
                tail = handle.read(4096)
            else:
                tail = b""
        digest = hashlib.sha1(head + tail).hexdigest()
        return f"{base}:{digest}"
    except OSError:
        return base


def _remove_legacy_database(path: Path) -> None:
    if not path.exists():
        return
    conn = None
    should_remove = False
    try:
        conn = sqlite3.connect(path)
        has_meta = conn.execute(
            "select 1 from sqlite_master where type='table' and name='app_meta'"
        ).fetchone()
        if not has_meta:
            should_remove = True
        else:
            row = conn.execute("select value from app_meta where key = 'schema_version'").fetchone()
            if row and row[0] == SCHEMA_VERSION:
                return
            should_remove = True
    except sqlite3.Error as exc:
        raise RuntimeError(f"索引文件检查失败，请先从托盘退出旧程序后再启动：{exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    if not should_remove:
        return
    # 旧版索引保存了完整正文；直接删除旧库，下一次启动会用轻量结构重建。
    for target in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    if path.exists():
        raise RuntimeError("旧索引文件正在被占用，请先从托盘退出旧程序后再启动。")


def _preview_text(content: str) -> str:
    text = re.sub(r"\s+", " ", content or "").strip()
    if len(text) <= PREVIEW_LIMIT:
        return text
    return text[:PREVIEW_LIMIT].rstrip() + "..."


def _highlight_preview(preview: str, query: str) -> str:
    text = preview or "已命中文档内容"
    for term in _query_terms(query):
        text = re.sub(
            re.escape(term),
            lambda match: f"<b>{match.group(0)}</b>",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compressed_content(content: str) -> bytes:
    if not content:
        return b""
    return zlib.compress(content.encode("utf-8", errors="ignore"), level=6)


def _decompressed_content(data: bytes) -> str:
    if not data:
        return ""
    try:
        return zlib.decompress(data).decode("utf-8", errors="ignore")
    except zlib.error:
        return ""


def _snippet_from_cache(cache: bytes, preview: str, query: str) -> str:
    terms = _query_terms(query)
    content = _decompressed_content(cache)
    if content and terms:
        lowered = content.lower()
        match_index = -1
        for term in terms:
            match_index = lowered.find(term.lower())
            if match_index >= 0:
                break
        if match_index >= 0:
            start = max(0, match_index - SNIPPET_RADIUS)
            end = min(len(content), match_index + SNIPPET_RADIUS)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet += "..."
            return _highlight_preview(snippet, query)
    return _highlight_preview(preview, query)


def _query_terms(query: str) -> list[str]:
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", query, flags=re.IGNORECASE)
    matches = re.findall(r'"([^"]+)"|([^\s*()]+)', cleaned)
    terms = []
    for quoted, plain in matches:
        term = (quoted or plain).strip()
        if term:
            terms.append(term)
    return sorted(set(terms), key=len, reverse=True)
