from __future__ import annotations

import os
import html
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import app_data_dir, resource_dir
from .index_store import IndexStore
from .indexer import Indexer
from .log_manager import clear_old_logs, logs_dir
from .settings import AppSettings, load_settings, reset_settings, save_settings, search_page_size
from .watcher import FileWatcher

INSTANCE_SERVER_NAME = "cn.zhaocj.DocContentSearch"


class SearchSignals(QObject):
    completed = Signal(int, str, list)
    failed = Signal(int, str, str)


class LoadingRow(QWidget):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.step = 0
        self.label = QLabel(message)
        self.label.setObjectName("loadingRow")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumWidth(180)
        self.label.setMinimumHeight(28)
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 16, 8, 16)
        layout.addWidget(self.label)
        self.setLayout(layout)
        self.setMinimumHeight(64)

    def tick(self) -> None:
        self.step = (self.step + 1) % 4
        self.label.setText(self.message + "." * self.step)


def _snippet_to_rich_text(snippet: str) -> str:
    marker_start = "\u0001HIGHLIGHT_START\u0001"
    marker_end = "\u0001HIGHLIGHT_END\u0001"
    text = (snippet or "").replace("<b>", marker_start).replace("</b>", marker_end)
    escaped = html.escape(text)
    return (
        escaped.replace(marker_start, "<span style='background-color:#fff2a8;color:#111827;font-weight:600;'>")
        .replace(marker_end, "</span>")
        .replace("\n", "<br>")
    )


class ResultCard(QWidget):
    def __init__(self, row: dict, on_open, on_location) -> None:
        super().__init__()
        self.setObjectName("resultCard")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.path = Path(row["path"])
        self.on_open = on_open
        self.on_location = on_location

        title = QLabel(html.escape(self.path.name))
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setObjectName("resultTitle")
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        path_label = QLabel(html.escape(str(self.path)))
        path_label.setTextFormat(Qt.TextFormat.RichText)
        path_label.setToolTip(str(self.path))
        path_label.setObjectName("resultPath")
        path_label.setMinimumWidth(0)
        path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["modified_at"]))
        meta = QLabel(f"类型：{html.escape(row['extension']) or '未知'}  修改：{modified}")
        meta.setObjectName("resultMeta")
        meta.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        snippet = QLabel(_snippet_to_rich_text(row["snippet"]))
        snippet.setTextFormat(Qt.TextFormat.RichText)
        snippet.setWordWrap(True)
        snippet.setObjectName("resultSnippet")
        snippet.setMaximumHeight(64)
        snippet.setMinimumWidth(0)
        snippet.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        open_btn = QPushButton("打开文件")
        open_btn.setObjectName("resultAction")
        open_btn.setFixedWidth(84)
        open_btn.clicked.connect(lambda: self.on_open(self.path))
        location_btn = QPushButton("打开位置")
        location_btn.setObjectName("resultAction")
        location_btn.setFixedWidth(84)
        location_btn.clicked.connect(lambda: self.on_location(self.path))

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(open_btn)
        actions.addWidget(location_btn)
        actions_box = QWidget()
        actions_box.setFixedWidth(176)
        actions_box.setLayout(actions)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.addStretch(1)
        action_row.addWidget(actions_box)

        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(meta)

        layout = QVBoxLayout()
        layout.setContentsMargins(7, 4, 7, 4)
        layout.setSpacing(1)
        layout.addLayout(header)
        layout.addWidget(path_label)
        layout.addWidget(snippet)
        layout.addLayout(action_row)
        self.setLayout(layout)


class AdvancedConfigDialog(QDialog):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.settings = load_settings()
        self.setWindowTitle("高级配置")
        self.resize(720, 680)

        operations = QGroupBox("索引操作")
        op_layout = QHBoxLayout()
        self.pause_btn = QPushButton("恢复索引" if window.indexer.pause_event.is_set() else "暂停索引")
        self.pause_btn.clicked.connect(self.toggle_pause)
        actions = [
            self.pause_btn,
            _button("重新扫描", window.indexer.start_full_index),
            _button("清空索引并重建", self.clear_and_rebuild),
            _button("打开索引目录", lambda: os.startfile(app_data_dir())),
            _button("打开日志目录", lambda: os.startfile(logs_dir())),
            _button("清理旧日志", self.clear_logs),
        ]
        for action in actions:
            op_layout.addWidget(action)
        operations.setLayout(op_layout)

        presets = QGroupBox("推荐模式")
        preset_layout = QHBoxLayout()
        preset_actions = [
            _button("速度优先", lambda: self.apply_preset("speed")),
            _button("平衡模式", lambda: self.apply_preset("balanced")),
            _button("尽量完整", lambda: self.apply_preset("complete")),
        ]
        for action in preset_actions:
            preset_layout.addWidget(action)
        presets.setLayout(preset_layout)

        form = QFormLayout()
        self.ignore_rules = QTextEdit("\n".join(self.settings.ignore_rules))
        self.ignore_rules.setFixedHeight(110)
        self.whitelist_rules = QTextEdit("\n".join(self.settings.whitelist_rules))
        self.whitelist_rules.setFixedHeight(90)
        self.delay_large = _spin(self.settings.delay_large_mb, 1, 102400)
        self.skip_large = _spin(self.settings.skip_large_mb, 1, 102400)
        self.text_workers = _spin(self.settings.text_workers, 0, 256)
        self.document_workers = _spin(self.settings.document_workers, 0, 32)
        self.write_batch = _spin(self.settings.write_batch_limit, 100, 10000)
        self.memory_cache = _spin(self.settings.memory_cache_mb, 64, 4096)
        self.search_page_size = _spin(self.settings.search_page_size, 10, 200)
        self.sample_hash = QCheckBox("使用更严格的文件变化判断（较慢）")
        self.sample_hash.setChecked(self.settings.use_sample_hash)
        form.addRow("只包含这些位置（可选）", self.whitelist_rules)
        form.addRow("排除这些位置", self.ignore_rules)
        form.addRow("大文件延后处理阈值（MB）", self.delay_large)
        form.addRow("超大文件跳过阈值（MB）", self.skip_large)
        form.addRow("文本处理任务数（0 自动）", self.text_workers)
        form.addRow("文档解析任务数（0 自动）", self.document_workers)
        form.addRow("写入批量上限", self.write_batch)
        form.addRow("内存缓存大小（MB）", self.memory_cache)
        form.addRow("每次显示结果数量", self.search_page_size)
        form.addRow("文件特征", self.sample_hash)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        defaults_btn = QPushButton("恢复默认")
        buttons.addButton(defaults_btn, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        defaults_btn.clicked.connect(self.restore_defaults)

        layout = QVBoxLayout()
        layout.addWidget(operations)
        layout.addWidget(presets)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def toggle_pause(self) -> None:
        self.window.toggle_pause()
        self.pause_btn.setText("恢复索引" if self.window.indexer.pause_event.is_set() else "暂停索引")

    def clear_and_rebuild(self) -> None:
        self.window.indexer.clear_and_rebuild()
        QMessageBox.information(self, "提示", "已清空索引并开始重建。")

    def clear_logs(self) -> None:
        removed = clear_old_logs()
        QMessageBox.information(self, "提示", f"已清理 {removed} 个日志文件。")

    def apply_preset(self, mode: str) -> None:
        presets = {
            "speed": (20, 50, 0, 0, 800, 256, False),
            "balanced": (30, 100, 0, 0, 1000, 512, False),
            "complete": (50, 200, 0, 0, 800, 512, False),
        }
        delay, skip, text_workers, document_workers, write_batch, memory_cache, sample_hash = presets[mode]
        self.delay_large.setValue(delay)
        self.skip_large.setValue(skip)
        self.text_workers.setValue(text_workers)
        self.document_workers.setValue(document_workers)
        self.write_batch.setValue(write_batch)
        self.memory_cache.setValue(memory_cache)
        self.sample_hash.setChecked(sample_hash)

    def restore_defaults(self) -> None:
        onboarding_shown = load_settings().onboarding_shown
        settings = reset_settings()
        settings.onboarding_shown = onboarding_shown
        save_settings(settings)
        self.settings = settings
        self.ignore_rules.setPlainText("\n".join(settings.ignore_rules))
        self.whitelist_rules.setPlainText("\n".join(settings.whitelist_rules))
        self.delay_large.setValue(settings.delay_large_mb)
        self.skip_large.setValue(settings.skip_large_mb)
        self.text_workers.setValue(settings.text_workers)
        self.document_workers.setValue(settings.document_workers)
        self.write_batch.setValue(settings.write_batch_limit)
        self.memory_cache.setValue(settings.memory_cache_mb)
        self.search_page_size.setValue(settings.search_page_size)
        self.sample_hash.setChecked(settings.use_sample_hash)

    def save(self) -> None:
        old_settings = load_settings()
        new_settings = AppSettings(
            scan_mode="combined",
            ignore_rules=_lines(self.ignore_rules),
            whitelist_rules=_lines(self.whitelist_rules),
            delay_large_mb=self.delay_large.value(),
            skip_large_mb=self.skip_large.value(),
            text_workers=self.text_workers.value(),
            document_workers=self.document_workers.value(),
            write_batch_limit=self.write_batch.value(),
            memory_cache_mb=self.memory_cache.value(),
            search_page_size=self.search_page_size.value(),
            use_sample_hash=self.sample_hash.isChecked(),
            onboarding_shown=old_settings.onboarding_shown,
        )
        scope_changed = _rule_key(old_settings.ignore_rules) != _rule_key(new_settings.ignore_rules) or _rule_key(
            old_settings.whitelist_rules
        ) != _rule_key(new_settings.whitelist_rules)
        save_settings(new_settings)
        self.window.indexer.apply_runtime_settings()
        if scope_changed:
            self.window.indexer.apply_scope_change()
            QMessageBox.information(self, "提示", "配置已保存并立即应用，正在按新的范围增量调整索引。")
        else:
            QMessageBox.information(self, "提示", "配置已保存并立即应用。")
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("全文文档搜索")
        self.resize(1050, 720)
        self.is_exiting = False
        self.cleaned_up = False
        self.tray_message_shown = False

        self.store = IndexStore()
        self.indexer = Indexer(self.store)
        self.watcher = FileWatcher(self.indexer)
        self.indexer.start_full_index()
        self.watcher.start()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入要搜索的文档内容")
        self.search_input.textChanged.connect(self.schedule_search)
        self.path_filter_input = QLineEdit()
        self.path_filter_input.setPlaceholderText("按路径或文件名过滤（可选）")
        self.path_filter_input.textChanged.connect(self.schedule_search)

        self.status_label = QLabel("正在建立索引，结果会逐步变完整。")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.itemDoubleClicked.connect(self.open_item)
        self.search_generation = 0
        self.search_query = ""
        self.path_filter = ""
        self.search_offset = 0
        self.search_loading = False
        self.search_has_more = False
        self.loading_item = None
        self.loading_widget = None
        self.search_signals = SearchSignals()
        self.search_signals.completed.connect(self.apply_search_results)
        self.search_signals.failed.connect(self.apply_search_error)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.search)
        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self.tick_loading)
        self.results.verticalScrollBar().valueChanged.connect(self.maybe_load_more_results)
        self.results.verticalScrollBar().rangeChanged.connect(lambda _min, _max: QTimer.singleShot(0, self.refit_result_cards))

        advanced_btn = QPushButton("高级配置")
        advanced_btn.clicked.connect(self.open_advanced_config)

        top = QHBoxLayout()
        top.addWidget(self.search_input, 1)
        top.addWidget(self.path_filter_input, 1)
        top.addWidget(advanced_btn)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.status_label)
        self.show_empty_state("输入关键词后，将只搜索文件内容，不搜索文件名。")

        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(1000)
        self._setup_tray()
        self.instance_server = None
        QTimer.singleShot(600, self.show_onboarding_if_needed)

    def _setup_tray(self) -> None:
        icon_path = resource_dir() / "assets" / "app.ico"
        icon = QIcon(str(icon_path)) if icon_path.exists() else self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("全文文档搜索")

        menu = QMenu(self)
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.restore_from_tray)
        exit_action = QAction("退出程序", self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def toggle_pause(self) -> None:
        if self.indexer.pause_event.is_set():
            self.indexer.resume()
        else:
            self.indexer.pause()

    def schedule_search(self) -> None:
        self.search_timer.start(250)

    def open_advanced_config(self) -> None:
        AdvancedConfigDialog(self).exec()

    def refresh_status(self) -> None:
        s = self.indexer.snapshot()
        if s.state == "实时更新中" and s.pending == 0:
            status_text = "搜索内容已准备好，后续文件变化会自动更新。"
            if s.failed > 0:
                status_text += " 部分文件暂时无法读取，详情可在日志中查看。"
            self.status_label.setText(status_text)
        elif s.state == "等待索引" and s.indexed > 0:
            self.status_label.setText(f"已载入 {s.indexed} 个文件内容，正在检查是否有变化...")
        elif s.state == "正在统计文件" and s.scan_total == 0 and s.indexed > 0:
            self.status_label.setText(f"已载入 {s.indexed} 个文件内容，正在检查是否有变化...")
        elif s.state == "正在统计文件" and s.scan_total > 0:
            checked = min(s.scan_completed, s.scan_total)
            percent = int(checked * 100 / max(s.scan_total, 1))
            self.status_label.setText(f"正在检查文件变化，检查进度：{percent}%（已检查{checked}/总文件数{s.scan_total}）")
        elif s.total > 0 and s.pending > 0:
            updated = min(s.completed, s.total)
            percent = int(updated * 100 / max(s.total, 1))
            if s.state == "检测到文件变化":
                status_text = "检测到文件变化，正在更新搜索内容"
            elif updated >= s.total:
                status_text = "搜索内容基本完成，正在处理剩余文件"
            elif s.state == "已暂停":
                status_text = "索引已暂停，可在高级配置中恢复"
            else:
                status_text = "正在更新搜索内容"
            self.status_label.setText(
                f"{status_text}，更新进度：{percent}%"
                f"（已更新{updated}/总文件数{s.total}）"
            )
        elif s.scan_total > 0:
            checked = min(s.scan_completed, s.scan_total)
            percent = int(checked * 100 / max(s.scan_total, 1))
            self.status_label.setText(f"正在检查文件变化，检查进度：{percent}%（已检查{checked}/总文件数{s.scan_total}）")
        else:
            self.status_label.setText("正在准备文件内容，搜索结果会逐步变完整。")

    def search(self) -> None:
        query = self.search_input.text().strip()
        path_filter = self.path_filter_input.text().strip()
        self.search_generation += 1
        generation = self.search_generation
        self.search_query = query
        self.path_filter = path_filter
        self.search_offset = 0
        self.search_has_more = False
        if not query:
            self.clear_loading()
            self.show_empty_state("输入关键词后，将只搜索文件内容，不搜索文件名。")
            return
        self.load_search_page(generation, query, path_filter, 0, replace=True)

    def load_search_page(self, generation: int, query: str, path_filter: str, offset: int, replace: bool = False) -> None:
        if self.search_loading:
            return
        self.search_loading = True
        self.show_loading("正在搜索" if replace else "正在加载更多", replace)
        threading.Thread(target=self._search_worker, args=(generation, query, path_filter, offset, replace), daemon=True).start()

    def _search_worker(self, generation: int, query: str, path_filter: str, offset: int, replace: bool) -> None:
        try:
            rows = self.store.search(query, limit=search_page_size(), offset=offset, path_filter=path_filter)
        except Exception as exc:
            self.search_signals.failed.emit(generation, query, str(exc))
            return
        self.search_signals.completed.emit(generation, query, [offset, replace, rows, path_filter])

    def apply_search_error(self, generation: int, _query: str, message: str) -> None:
        self.search_loading = False
        self.clear_loading()
        if generation != self.search_generation:
            return
        self.status_label.setText(f"搜索失败：{message}")

    def apply_search_results(self, generation: int, query: str, payload: list) -> None:
        self.search_loading = False
        current_filter = self.path_filter_input.text().strip()
        if generation != self.search_generation or query != self.search_input.text().strip():
            self.clear_loading()
            return
        offset, replace, rows, path_filter = payload
        if path_filter != current_filter:
            self.clear_loading()
            return
        self.results.setUpdatesEnabled(False)
        self.clear_loading()
        if replace:
            self.results.clear()
        if not query:
            self.results.setUpdatesEnabled(True)
            return
        if not rows and offset == 0:
            message = "没有找到同时匹配内容和路径条件的文件。" if path_filter else "没有找到包含该内容的文件。可以稍后再试，搜索内容可能仍在更新。"
            self.show_empty_state(message)
            self.results.setUpdatesEnabled(True)
            return
        for row in rows:
            card = ResultCard(row, self.open_path, self.open_file_location)
            item = QListWidgetItem()
            item.setData(256, row["path"])
            self.fit_result_card(item, card)
            self.results.addItem(item)
            self.results.setItemWidget(item, card)
        self.search_offset = offset + len(rows)
        self.search_has_more = len(rows) >= search_page_size()
        self.results.setUpdatesEnabled(True)
        QTimer.singleShot(0, self.refit_result_cards)

    def maybe_load_more_results(self) -> None:
        if self.search_loading or not self.search_has_more or not self.search_query:
            return
        bar = self.results.verticalScrollBar()
        if bar.value() >= bar.maximum() - 3:
            self.load_search_page(self.search_generation, self.search_query, self.path_filter, self.search_offset)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self.refit_result_cards)

    def fit_result_card(self, item: QListWidgetItem, card: QWidget) -> None:
        # itemWidget 不会自动按可视区域收缩，长路径时需要显式限制卡片宽度。
        width = max(260, self.results.viewport().width() - 24)
        card.setFixedWidth(width)
        item.setSizeHint(QSize(width, card.sizeHint().height()))

    def refit_result_cards(self) -> None:
        for index in range(self.results.count()):
            item = self.results.item(index)
            widget = self.results.itemWidget(item)
            if isinstance(widget, ResultCard):
                self.fit_result_card(item, widget)

    def show_loading(self, message: str, replace: bool) -> None:
        self.clear_loading()
        if replace:
            self.results.clear()
        self.loading_widget = LoadingRow(message)
        self.loading_item = QListWidgetItem()
        self.loading_item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.loading_item.setSizeHint(QSize(240, 72))
        self.results.addItem(self.loading_item)
        self.results.setItemWidget(self.loading_item, self.loading_widget)
        self.loading_timer.start(280)

    def clear_loading(self) -> None:
        self.loading_timer.stop()
        if self.loading_item is None:
            self.loading_widget = None
            return
        row = self.results.row(self.loading_item)
        if row >= 0:
            self.results.takeItem(row)
        self.loading_item = None
        self.loading_widget = None

    def tick_loading(self) -> None:
        if self.loading_widget is not None:
            self.loading_widget.tick()

    def show_empty_state(self, message: str) -> None:
        self.clear_loading()
        self.results.clear()
        item = QListWidgetItem(message)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results.addItem(item)

    def show_onboarding_if_needed(self) -> None:
        settings = load_settings()
        if settings.onboarding_shown:
            return
        roots = "、".join(settings.whitelist_rules) if settings.whitelist_rules else "可访问的常用位置"
        QMessageBox.information(self, "提示", f"将为这些位置建立文件内容索引：{roots}\n可在高级配置中调整。")
        settings.onboarding_shown = True
        save_settings(settings)

    def open_item(self, item: QListWidgetItem) -> None:
        data = item.data(256)
        if not data:
            return
        self.open_path(Path(data))

    def open_path(self, path: Path) -> None:
        if path.exists():
            os.startfile(path)
        else:
            self.status_label.setText("文件不存在，可能已被移动或删除。")

    def open_file_location(self, path: Path) -> None:
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            self.status_label.setText("文件不存在，可能已被移动或删除。")

    def closeEvent(self, event) -> None:
        if self.is_exiting:
            self._cleanup()
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()
        if not self.tray_message_shown and self.tray.isVisible():
            self.tray.showMessage("全文文档搜索", "程序已最小化到托盘，索引会继续运行。", QSystemTrayIcon.MessageIcon.Information, 3000)
            self.tray_message_shown = True

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def attach_instance_server(self, server: QLocalServer) -> None:
        self.instance_server = server
        server.newConnection.connect(self._handle_instance_message)

    def _handle_instance_message(self) -> None:
        while self.instance_server and self.instance_server.hasPendingConnections():
            socket = self.instance_server.nextPendingConnection()
            socket.readyRead.connect(lambda sock=socket: self._read_instance_message(sock))
            # 第二个进程的唤醒消息很短，可能在绑定 readyRead 前已经到达。
            if socket.bytesAvailable():
                self._read_instance_message(socket)
            else:
                QTimer.singleShot(50, lambda sock=socket: self._read_instance_message(sock) if sock.bytesAvailable() else None)

    def _read_instance_message(self, socket: QLocalSocket) -> None:
        message = bytes(socket.readAll()).decode("utf-8", errors="ignore")
        if message.strip() == "show":
            self.restore_from_tray()
        socket.disconnectFromServer()

    def exit_app(self) -> None:
        self.is_exiting = True
        self._cleanup()
        QApplication.quit()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.restore_from_tray()

    def _cleanup(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.watcher.stop()
        self.indexer.shutdown()
        if hasattr(self, "tray"):
            self.tray.hide()


def _button(text: str, callback) -> QPushButton:
    button = QPushButton(text)
    button.clicked.connect(callback)
    return button


def _spin(value: int, minimum: int, maximum: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    return box


def _lines(text_edit: QTextEdit) -> list[str]:
    return [line.strip() for line in text_edit.toPlainText().splitlines() if line.strip()]


def _rule_key(rules: list[str]) -> list[str]:
    return [rule.strip().replace("\\", "/").strip("/").lower() for rule in rules if rule.strip()]


def _notify_existing_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_SERVER_NAME)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def _create_instance_server() -> QLocalServer:
    QLocalServer.removeServer(INSTANCE_SERVER_NAME)
    server = QLocalServer()
    if not server.listen(INSTANCE_SERVER_NAME):
        raise RuntimeError("无法创建单实例监听，请稍后重试。")
    return server


def main() -> None:
    app = QApplication(sys.argv)
    if _notify_existing_instance():
        sys.exit(0)
    server = _create_instance_server()
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(
        """
        QWidget { font-family: Microsoft YaHei; font-size: 14px; color: #172033; }
        QLineEdit { padding: 10px; border: 1px solid #cfd7e6; border-radius: 6px; background: #ffffff; }
        QPushButton { padding: 8px 14px; border: 1px solid #cfd7e6; border-radius: 6px; background: #ffffff; }
        QPushButton:hover { background: #f5f8fc; border-color: #b8c6da; }
        QPushButton#resultAction { padding: 4px 10px; font-size: 12px; color: #155e75; border-color: #a7d8e8; background: #eef9fc; }
        QListWidget { border: 1px solid #d8e0ee; background: #f7f9fc; outline: 0; }
        QListWidget::item { border: 0; margin: 5px 4px; }
        QListWidget::item:hover { background: #eef6fb; }
        QWidget#resultCard { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; }
        QWidget#resultCard:hover { background: #f9fcff; border-color: #9fc8d8; }
        #resultTitle { font-size: 15px; font-weight: 600; color: #101828; }
        #resultPath { color: #667085; font-size: 12px; }
        #resultMeta { color: #475467; font-size: 12px; }
        #resultSnippet { color: #1f2937; line-height: 1.25; }
        #loadingRow { color: #667085; font-size: 14px; }
        """
    )
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", f"程序启动失败：{exc}")
        sys.exit(1)
    window.attach_instance_server(server)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
