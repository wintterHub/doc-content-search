from __future__ import annotations

from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import discover_roots, is_excluded
from .indexer import Indexer


class ChangeHandler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer) -> None:
        super().__init__()
        self.indexer = indexer

    def on_created(self, event) -> None:
        self._upsert(event.src_path)

    def on_modified(self, event) -> None:
        self._upsert(event.src_path)

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self.indexer.enqueue_delete(Path(event.src_path))

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self.indexer.enqueue_delete(Path(event.src_path))
            self._upsert(event.dest_path)

    def _upsert(self, path_text: str) -> None:
        path = Path(path_text)
        if not is_excluded(path) and path.is_file():
            self.indexer.enqueue_upsert(path)


class FileWatcher:
    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer
        self.observer = Observer()

    def start(self) -> None:
        handler = ChangeHandler(self.indexer)
        for root in discover_roots():
            try:
                self.observer.schedule(handler, str(root), recursive=True)
            except Exception as exc:
                self.indexer.status.last_error = f"监听目录失败：{root}，{exc}"
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=3)
