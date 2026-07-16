from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .everything_provider import EverythingError, EverythingProvider
from .config import (
    DOC_EXTENSIONS,
    SCAN_EXTENSIONS,
    TEXT_EXTENSIONS,
    auto_worker_counts,
    is_excluded,
)
from .extractor import DocumentExtractor, ExtractError, SkipFileError
from .index_store import IndexStore
from .log_manager import write_log
from .settings import delay_large_bytes, skip_large_bytes, write_batch_limit
from .tika_server import cleanup_tika_processes, stop_tika_pool


@dataclass
class IndexStatus:
    state: str = "等待索引"
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    total: int = 0
    completed: int = 0
    scan_completed: int = 0
    scan_total: int = 0
    discovered: int = 0
    reused: int = 0
    current_path: str = ""
    last_error: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=300))


class Indexer:
    def __init__(self, store: IndexStore) -> None:
        self.store = store
        self.extract_workers, self.tika_workers, self.scan_workers = auto_worker_counts()
        self.everything = EverythingProvider()
        self.extract_gate = threading.Semaphore(self.extract_workers)
        self.tika_gate = threading.Semaphore(self.tika_workers)
        self.status = IndexStatus()
        self.status.indexed = self.store.indexed_count()
        self.status_lock = threading.Lock()
        self.queue: queue.Queue[tuple[str, Path]] = queue.Queue()
        self.slow_queue: queue.Queue[tuple[str, Path]] = queue.Queue()
        self.write_queue: queue.Queue[tuple[str, Path, str | None, int, float, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.full_index_active = threading.Event()
        self.full_index_lock = threading.Lock()
        self.active_extracts = 0
        self.active_writes = 0
        self.started_at = time.time()
        self.last_summary_at = self.started_at
        self.pending_write_log_count = 0
        self.last_write_log_at = self.started_at
        self.workers = [
            threading.Thread(target=self._consume_queue, daemon=True)
            for _ in range(self.extract_workers)
        ]
        for worker in self.workers:
            worker.start()
        self.slow_worker = threading.Thread(target=self._consume_slow_queue, daemon=True)
        self.slow_worker.start()
        self.writer = threading.Thread(target=self._consume_writes, daemon=True)
        self.writer.start()
        self._log(f"已载入 {self.status.indexed} 个已有索引。")

    def start_full_index(self) -> None:
        threading.Thread(target=self._full_index, daemon=True).start()

    def clear_and_rebuild(self) -> None:
        self.store.clear_all()
        self.start_full_index()

    def apply_runtime_settings(self) -> None:
        target_extract_workers, target_tika_workers, self.scan_workers = auto_worker_counts()
        if target_extract_workers > len(self.workers):
            for _ in range(target_extract_workers - len(self.workers)):
                worker = threading.Thread(target=self._consume_queue, daemon=True)
                self.workers.append(worker)
                worker.start()
        self.extract_workers = target_extract_workers
        self.tika_workers = target_tika_workers
        self.extract_gate = threading.Semaphore(max(1, target_extract_workers))
        self.tika_gate = threading.Semaphore(max(1, target_tika_workers))
        self.store.apply_runtime_settings()
        self._log("配置已立即应用。")

    def apply_scope_change(self) -> None:
        threading.Thread(target=self._apply_scope_change, daemon=True).start()

    def _apply_scope_change(self) -> None:
        self._set_state("正在检查文件变化")
        # 配置变化只增量扫描新范围，旧索引默认保留，避免白名单/黑名单误配置导致大量索引丢失。
        self._log("配置已应用，已保留原有索引，正在检查新增或变化文件。")
        self.start_full_index()

    def enqueue_upsert(self, path: Path, realtime: bool = True) -> None:
        if realtime and self.full_index_active.is_set():
            return
        try:
            stat = path.stat()
            self.store.mark_task_pending(path, stat.st_size, stat.st_mtime, self.store_fingerprint(path, stat.st_size, stat.st_mtime))
        except OSError:
            pass
        self._enqueue_action("upsert", path)
        if realtime:
            with self.status_lock:
                self.status.state = "检测到文件变化"
                self.status.total += 1
                self.status.pending = self._pending_count_locked()

    def enqueue_delete(self, path: Path, realtime: bool = True) -> None:
        if realtime and self.full_index_active.is_set():
            return
        if realtime:
            self._enqueue_action("delete", path)
            with self.status_lock:
                self.status.state = "检测到文件变化"
                self.status.total += 1
                self.status.pending = self._pending_count_locked()
        else:
            self._enqueue_action("delete", path)

    def pause(self) -> None:
        self.pause_event.set()
        self._set_state("已暂停")

    def resume(self) -> None:
        self.pause_event.clear()
        self._set_state("实时更新中")

    def _full_index(self) -> None:
        if not self.full_index_lock.acquire(blocking=False):
            self._log("已有索引任务正在运行，本次重建请求已忽略。")
            return
        try:
            self.full_index_active.set()
            self._reset_progress()
            self._log(f"已载入 {self.status.indexed} 个已有索引。")
            self._set_state("正在统计文件")
            pending = [path for path in self.store.pending_tasks() if path.exists()]
            seen = {str(path).lower() for path in pending}
            with self.status_lock:
                self.status.total = len(pending)
                self.status.pending = len(pending)
                self.status.completed = 0
                self.status.scan_completed = 0
                self.status.scan_total = 0
            for path in pending:
                if self.stop_event.is_set():
                    return
                self.enqueue_upsert(path, realtime=False)
            queued = self._collect_candidates(enqueue=True, seen_paths=seen)
            with self.status_lock:
                reused = self.status.reused
            self._log(f"已复用 {reused} 个已有索引，发现 {len(pending) + queued} 个需要更新的文件。")
            self._log(f"统计完成：需要处理 {len(pending) + queued} 个文件。")
            if len(pending) + queued > 0:
                self._set_state("正在建立索引")
            self.queue.join()
            self.slow_queue.join()
            self.write_queue.join()
            self._set_state("实时更新中")
            with self.status_lock:
                self.status.current_path = ""
                self.status.pending = 0
                self.status.total = self.status.completed
        finally:
            self.full_index_active.clear()
            self.full_index_lock.release()

    def _collect_candidates(self, enqueue: bool = False, seen_paths: set[str] | None = None) -> int | list[Path]:
        try:
            candidates = self.everything.list_candidates()
        except EverythingError as exc:
            self._fail(str(exc), counts_completed=False)
            return 0 if enqueue else []
        with self.status_lock:
            self.status.discovered = len(candidates)
            self.status.scan_total = len(candidates)
            self.status.scan_completed = 0
        eligible: list[Path] = []
        seen = seen_paths or set()
        for path in candidates:
            key = str(path).lower()
            if key in seen or is_excluded(path) or path.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            seen.add(key)
            eligible.append(path)
        if enqueue:
            with self.status_lock:
                # 分母先固定下来，避免主界面进度一边跑一边涨。
                self.status.total += len(eligible)
        deduped: list[Path] = []
        last_progress_at = time.time()
        for index, path in enumerate(eligible, start=1):
            size, modified_at = _safe_stat(path)
            if size > skip_large_bytes():
                with self.status_lock:
                    self.status.skipped += 1
                    self._mark_completed_locked()
                self._log(f"跳过：{path}，文件过大，已跳过（{_format_size(size)}）")
                self._update_scan_progress(index, len(eligible), last_progress_at, enqueue)
                last_progress_at = time.time() if time.time() - last_progress_at >= 0.5 else last_progress_at
                continue
            if self.store.needs_index_snapshot(path, size, modified_at):
                self._mark_pending(path, size)
                deduped.append(path)
                if enqueue:
                    with self.status_lock:
                        # 需要更新的文件进入队列，完成数等写入/跳过/失败后再增加。
                        self.status.pending += 1
                    self.enqueue_upsert(path, realtime=False)
            elif enqueue:
                with self.status_lock:
                    # 已索引且未变化的文件立即计入完成，形成断点续上的观感。
                    self.status.reused += 1
                    self._mark_completed_locked()
            self._update_scan_progress(index, len(eligible), last_progress_at, enqueue)
            last_progress_at = time.time() if time.time() - last_progress_at >= 0.5 else last_progress_at
        with self.status_lock:
            if not enqueue:
                self.status.total = len(deduped)
                self.status.completed = 0
        self._log(f"已找到 {len(deduped)} 个待处理文件。")
        if enqueue:
            return len(deduped)
        deduped.sort(key=_speed_first_key)
        return deduped

    def _consume_queue(self) -> None:
        self._consume_from_queue(self.queue)

    def _consume_slow_queue(self) -> None:
        self._consume_from_queue(self.slow_queue)

    def _consume_from_queue(self, source_queue: queue.Queue[tuple[str, Path]]) -> None:
        extractor = DocumentExtractor()
        while not self.stop_event.is_set():
            try:
                action, path = source_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            while self.pause_event.is_set():
                time.sleep(0.2)
            with self.status_lock:
                self.active_extracts += 1
                self.status.pending = self._pending_count_locked()
                self.status.current_path = str(path)
            try:
                with self.extract_gate:
                    if action == "delete":
                        self.write_queue.put(("delete", path, None, 0, 0.0, ""))
                    else:
                        if path.exists() and extractor.is_supported_candidate(path):
                            stat = path.stat()
                            if stat.st_size > skip_large_bytes():
                                self._skip(path, f"文件过大，已跳过（{_format_size(stat.st_size)}）")
                                continue
                            content = self._extract_with_limit(extractor, path)
                            fingerprint = self.store_fingerprint(path, stat.st_size, stat.st_mtime)
                            self.write_queue.put(("upsert", path, content, stat.st_size, stat.st_mtime, fingerprint))
                        else:
                            self._skip(path, "不是支持的文本或文档格式")
            except SkipFileError as exc:
                self._skip(path, str(exc))
            except ExtractError as exc:
                self._skip(path, str(exc))
            except Exception as exc:
                self._fail(f"{path}：{exc}", counts_completed=True, path=path)
            finally:
                with self.status_lock:
                    self.active_extracts = max(0, self.active_extracts - 1)
                    self.status.pending = self._pending_count_locked()
                source_queue.task_done()

    def _consume_writes(self) -> None:
        while not self.stop_event.is_set():
            batch = []
            try:
                batch.append(self.write_queue.get(timeout=0.5))
            except queue.Empty:
                continue
            target_batch_size = (
                1 if batch[0][3] > delay_large_bytes() else min(write_batch_limit(), max(200, self.write_queue.qsize()))
            )
            wait_until = time.time() + 0.25
            while len(batch) < target_batch_size:
                try:
                    if time.time() < wait_until:
                        item = self.write_queue.get(timeout=0.05)
                    else:
                        item = self.write_queue.get_nowait()
                    if item[3] > delay_large_bytes() and batch:
                        self.write_queue.task_done()
                        self.write_queue.put(item)
                        break
                    batch.append(item)
                except queue.Empty:
                    break

            # SQLite 统一批量写入，避免数据库争用，同时减少 FTS 提交次数。
            try:
                with self.status_lock:
                    self.active_writes += len(batch)
                    self.status.pending = self._pending_count_locked()
                indexed, _deleted = self.store.apply_batch(batch)
                with self.status_lock:
                    self.status.indexed += indexed
                    self._mark_completed_locked(len(batch))
                    self.status.pending = self._pending_count_locked()
                if indexed:
                    self._log_write_progress(indexed)
                self._log_summary()
            except Exception as exc:
                with self.status_lock:
                    self.status.failed += len(batch)
                    self._mark_completed_locked(len(batch))
                    self.status.last_error = str(exc)
                self._log(f"写入索引失败：{exc}")
            finally:
                with self.status_lock:
                    self.active_writes = max(0, self.active_writes - len(batch))
                    self.status.pending = self._pending_count_locked()
                for _ in batch:
                    self.write_queue.task_done()

    def _extract_with_limit(self, extractor: DocumentExtractor, path: Path) -> str:
        if path.suffix.lower() in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}:
            with self.tika_gate:
                return extractor.extract(path)
        return extractor.extract(path)

    def _quick_candidate(self, path: Path) -> bool:
        if is_excluded(path) or not path.is_file():
            return False
        suffix = path.suffix.lower()
        if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}:
            return True
        if suffix:
            from .config import TEXT_EXTENSIONS

            return suffix in TEXT_EXTENSIONS
        return True

    def snapshot(self) -> IndexStatus:
        with self.status_lock:
            return IndexStatus(
                state=self.status.state,
                indexed=self.status.indexed,
                skipped=self.status.skipped,
                failed=self.status.failed,
                pending=self.status.pending,
                total=self.status.total,
                completed=self.status.completed,
                scan_total=self.status.scan_total,
                scan_completed=self.status.scan_completed,
                discovered=self.status.discovered,
                reused=self.status.reused,
                current_path=self.status.current_path,
                last_error=self.status.last_error,
                logs=deque(self.status.logs, maxlen=300),
            )

    def _reset_progress(self) -> None:
        with self.status_lock:
            self.status.total = 0
            self.status.completed = 0
            self.status.discovered = 0
            self.status.reused = 0
            self.status.pending = 0
            self.status.current_path = ""
            self.status.last_error = ""
            self.status.logs.clear()
            self.status.indexed = self.store.indexed_count()
            self.status.scan_completed = 0
            self.status.scan_total = 0
            self.started_at = time.time()
            self.last_summary_at = self.started_at
            self.pending_write_log_count = 0
            self.last_write_log_at = self.started_at

    def _set_state(self, state: str) -> None:
        with self.status_lock:
            self.status.state = state
        self._log(state)

    def _skip(self, path: Path, reason: str) -> None:
        with self.status_lock:
            self.status.skipped += 1
            self._mark_completed_locked()
            self.status.pending = self._pending_count_locked()
        self.store.mark_task_skipped(path, reason)
        self._log(f"跳过：{path}，{reason}")

    def _pending_count_locked(self) -> int:
        # 包含队列外正在执行的任务，避免大文件处理中被误判为空闲。
        return (
            self.queue.qsize()
            + self.slow_queue.qsize()
            + self.write_queue.qsize()
            + self.active_extracts
            + self.active_writes
        )

    def _mark_completed_locked(self, count: int = 1) -> None:
        if self.status.total > 0:
            self.status.completed = min(self.status.completed + count, self.status.total)
        else:
            self.status.completed += count

    def _log_summary(self) -> None:
        now = time.time()
        if now - self.last_summary_at < 5:
            return
        with self.status_lock:
            if self._pending_count_locked() == 0 and not self.full_index_active.is_set():
                return
            elapsed_minutes = max((now - self.started_at) / 60, 0.01)
            speed = int(self.status.completed / elapsed_minutes)
            message = (
                f"解析摘要：成功 {self.status.indexed}，跳过 {self.status.skipped}，"
                f"失败 {self.status.failed}，速度 {speed} 个/分钟"
            )
            self.last_summary_at = now
        self._log(message)

    def _log_write_progress(self, indexed: int) -> None:
        self.pending_write_log_count += indexed
        now = time.time()
        if self.pending_write_log_count < 50 and now - self.last_write_log_at < 5:
            return
        count = self.pending_write_log_count
        self.pending_write_log_count = 0
        self.last_write_log_at = now
        # 少量写入不显示为“批量”，避免日志给人造成单文件批处理的误解。
        prefix = "已批量写入" if count > 1 else "已写入"
        self._log(f"{prefix} {count} 个文件。")

    def _update_collect_progress(self, completed: int, total: int, last_progress_at: float) -> None:
        now = time.time()
        if completed < total and now - last_progress_at < 0.5:
            return
        # 准备阶段也刷新进度，避免大量文件过滤时界面看起来停住。
        with self.status_lock:
            self.status.completed = completed
            self.status.pending = max(total - completed, 0)

    def _update_scan_progress(self, checked: int, total: int, last_progress_at: float, enqueue: bool) -> None:
        now = time.time()
        if checked < total and now - last_progress_at < 0.5:
            return
        if enqueue:
            with self.status_lock:
                self.status.scan_total = total
                self.status.scan_completed = checked
                self.status.pending = self._pending_count_locked()
            return
        self._update_collect_progress(checked, total, last_progress_at)

    def _fail(self, message: str, counts_completed: bool = True, path: Path | None = None) -> None:
        with self.status_lock:
            self.status.failed += 1
            if counts_completed:
                self._mark_completed_locked()
            self.status.last_error = message
            self.status.pending = self._pending_count_locked()
        if path is not None:
            self.store.mark_task_error(path, message, retryable=True)
        self._log(f"失败：{message}")

    def _log(self, message: str) -> None:
        text = message.replace("\r", " ").replace("\n", " ")
        write_log(text)
        if len(text) > 260:
            text = text[:240] + "..."
        with self.status_lock:
            self.status.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def shutdown(self) -> None:
        self.stop_event.set()
        stop_tika_pool()
        cleanup_tika_processes()
        self.store.checkpoint()

    def store_fingerprint(self, path: Path, size: int, modified_at: float) -> str:
        from .index_store import fingerprint_for

        return fingerprint_for(path, size, modified_at)

    def _mark_pending(self, path: Path, size: int) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        self.store.mark_task_pending(path, size, stat.st_mtime, self.store_fingerprint(path, size, stat.st_mtime))

    def _enqueue_action(self, action: str, path: Path) -> None:
        if action == "upsert" and _safe_size(path) > delay_large_bytes():
            # 大文件单独后台处理，避免占满普通文件队列导致界面长时间无反馈。
            self.slow_queue.put((action, path))
            return
        self.queue.put((action, path))


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_stat(path: Path) -> tuple[int, float]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime
    except OSError:
        return 0, 0.0


def _speed_first_key(path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    size = _safe_size(path)
    if suffix in TEXT_EXTENSIONS:
        priority = 0
    elif suffix in DOC_EXTENSIONS:
        priority = 1
    else:
        priority = 2
    if size > delay_large_bytes():
        priority += 10
    return priority, size


def _format_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MB"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result
