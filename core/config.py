from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT_DIR / "storage"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"

load_dotenv(ROOT_DIR / ".env")


def get_setting(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def setting_path(name: str, default: str) -> Path:
    path = Path(get_setting(name, default)).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path


RUNTIME_PREFS_FILE = STORAGE_DIR / "runtime_prefs.json"


def get_runtime_pref(name: str, default: str = "") -> str:
    """Read a preference changed at runtime (e.g. from Telegram); falls back to env/default."""
    try:
        data = json.loads(RUNTIME_PREFS_FILE.read_text(encoding="utf-8"))
        value = str(data.get(name, "")).strip()
        if value:
            return value
    except (OSError, ValueError):
        pass
    return os.getenv(name, default) or default


def set_runtime_pref(name: str, value: str) -> None:
    """Persist a runtime preference shared by every process."""
    try:
        data = json.loads(RUNTIME_PREFS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[name] = value
    RUNTIME_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PREFS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
