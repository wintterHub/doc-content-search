from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import app_data_dir


DEFAULT_IGNORE_RULES = [
    "AppData/",
    "Windows/",
    "Program Files/",
    "Program Files (x86)/",
    "ProgramData/",
    "System Volume Information/",
    "$Recycle.Bin/",
    ".git/",
    "node_modules/",
    "target/",
    "dist/",
    "build/",
    ".cache/",
]


def default_whitelist_rules() -> list[str]:
    rules = [_rule_path(Path.home())]
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            rules.append(_rule_path(drive))
    return rules


@dataclass
class AppSettings:
    scan_mode: str = "combined"
    ignore_rules: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_RULES))
    whitelist_rules: list[str] = field(default_factory=default_whitelist_rules)
    delay_large_mb: int = 20
    skip_large_mb: int = 50
    text_workers: int = 0
    document_workers: int = 0
    write_batch_limit: int = 800
    memory_cache_mb: int = 256
    search_page_size: int = 40
    use_sample_hash: bool = False
    codex_rule_migrated: bool = True
    onboarding_shown: bool = False


_SETTINGS: AppSettings | None = None


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def load_settings() -> AppSettings:
    global _SETTINGS
    if _SETTINGS is not None:
        return _SETTINGS
    path = settings_path()
    if not path.exists():
        _SETTINGS = AppSettings()
        save_settings(_SETTINGS)
        return _SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        is_legacy_settings = "codex_rule_migrated" not in data
        defaults = asdict(AppSettings())
        defaults.update({key: value for key, value in data.items() if key in defaults})
        _SETTINGS = AppSettings(**defaults)
        repaired = False
        if is_legacy_settings:
            # 只迁移历史默认规则；之后用户手动添加 .codex 会被正常保留。
            _SETTINGS.ignore_rules = [
                rule for rule in _SETTINGS.ignore_rules if rule.strip().replace("\\", "/").strip("/") != ".codex"
            ]
            _SETTINGS.codex_rule_migrated = True
            repaired = True
        ignore_rules = _repair_ignore_rules(_SETTINGS.ignore_rules)
        whitelist_rules = _repair_path_rules(_SETTINGS.whitelist_rules)
        if ignore_rules != _SETTINGS.ignore_rules or whitelist_rules != _SETTINGS.whitelist_rules:
            _SETTINGS.ignore_rules = ignore_rules
            _SETTINGS.whitelist_rules = whitelist_rules
            repaired = True
        if _SETTINGS.use_sample_hash:
            # 默认采用修改时间判断变化，避免启动时逐个文件读取头尾内容。
            _SETTINGS.use_sample_hash = False
            repaired = True
        if repaired:
            save_settings(_SETTINGS)
    except Exception:
        _SETTINGS = AppSettings()
    return _SETTINGS


def save_settings(settings: AppSettings) -> None:
    global _SETTINGS
    _SETTINGS = settings
    settings_path().write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")


def reset_settings() -> AppSettings:
    settings = AppSettings()
    save_settings(settings)
    return settings


def should_index_path(path: Path) -> bool:
    settings = load_settings()
    if _matches_any(path, settings.ignore_rules):
        return False
    if settings.whitelist_rules:
        return _matches_any(path, settings.whitelist_rules)
    return True


def delay_large_bytes() -> int:
    return max(1, load_settings().delay_large_mb) * 1024 * 1024


def skip_large_bytes() -> int:
    return max(1, load_settings().skip_large_mb) * 1024 * 1024


def write_batch_limit() -> int:
    return max(100, load_settings().write_batch_limit)


def sqlite_cache_kb() -> int:
    return -max(64, load_settings().memory_cache_mb) * 1024


def search_page_size() -> int:
    return min(200, max(10, load_settings().search_page_size))


def _matches_any(path: Path, rules: list[str]) -> bool:
    normalized = _normalize(path)
    parts = [part.lower() for part in normalized.split("/") if part]
    for raw_rule in rules:
        rule = raw_rule.strip()
        if not rule or rule.startswith("#"):
            continue
        normalized_rule = rule.replace("\\", "/").strip("/").lower()
        if not normalized_rule:
            continue
        if rule.endswith("/") and "/" not in normalized_rule:
            if normalized_rule in parts:
                return True
        elif "/" in normalized_rule:
            path_text = normalized.lower()
            # 带路径的目录规则按路径前缀处理，同时保留通配写法。
            if (
                path_text == normalized_rule
                or path_text.startswith(f"{normalized_rule}/")
                or fnmatch.fnmatch(path_text, normalized_rule)
                or fnmatch.fnmatch(path_text, f"**/{normalized_rule}")
                or fnmatch.fnmatch(path_text, f"**/{normalized_rule}/*")
            ):
                return True
        else:
            if any(fnmatch.fnmatch(part, normalized_rule) for part in parts):
                return True
            if fnmatch.fnmatch(path.name.lower(), normalized_rule):
                return True
    return False


def _normalize(path: Path) -> str:
    return str(path).replace("\\", "/")


def _rule_path(path: Path) -> str:
    text = str(path)
    if not text.endswith("\\"):
        text += "\\"
    return text


def _repair_ignore_rules(rules: list[str]) -> list[str]:
    repaired: list[str] = []
    for rule in rules:
        text = rule.strip()
        if _looks_like_compacted_defaults(text):
            repaired.extend(DEFAULT_IGNORE_RULES)
        elif text:
            repaired.append(text)
    return _dedupe_rules(repaired)


def _repair_path_rules(rules: list[str]) -> list[str]:
    repaired: list[str] = []
    for rule in rules:
        text = rule.strip()
        if re.search(r"\s+[A-Za-z]:\\", text):
            # 兼容历史版本把多条 Windows 路径保存到一行的配置。
            repaired.extend(part.strip() for part in re.split(r"\s+(?=[A-Za-z]:\\)", text) if part.strip())
        elif text:
            repaired.append(text)
    return _dedupe_rules(repaired)


def _looks_like_compacted_defaults(text: str) -> bool:
    return "AppData/" in text and "Windows/" in text and "Program Files/" in text


def _dedupe_rules(rules: list[str]) -> list[str]:
    seen = set()
    result = []
    for rule in rules:
        key = rule.strip().replace("\\", "/").strip("/").lower()
        if key and key not in seen:
            seen.add(key)
            result.append(rule.strip())
    return result
