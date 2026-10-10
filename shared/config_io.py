"""Atomic reads/writes of config.json, shared by the UI and the daemon.

The UI writes the file and the daemon watches it, so every write goes to a
temp file in the same directory and is swapped in with os.replace(). A reader
therefore only ever sees the old or the new file, never a half-written one.

Where files live:
    BUNDLE_ROOT  the files shipped with the app (default config.json, catalog.db):
                 the project folder, or PyInstaller's unpacked bundle when frozen
    APP_HOME     the user's config/ and data/: the project folder in development,
                 %APPDATA%/AmbientMood in the installed app (Program Files is
                 read-only). AMBIENT_MOOD_HOME overrides both.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))


def _app_home() -> Path:
    if os.environ.get("AMBIENT_MOOD_HOME"):
        return Path(os.environ["AMBIENT_MOOD_HOME"]).expanduser()
    if FROZEN:
        return Path(os.environ.get("APPDATA") or Path.home()) / "AmbientMood"
    return PROJECT_ROOT


APP_HOME = _app_home()
DEFAULT_CONFIG_PATH = APP_HOME / "config" / "config.json"

# Shipped files copied into APP_HOME on first run (relative to both roots).
BUNDLED_FILES = ("config/config.json", "data/catalog.db")


class ConfigError(ValueError):
    pass


def ensure_app_home(home: Path = APP_HOME, bundle: Path = BUNDLE_ROOT) -> list[Path]:
    """Copy the shipped config and catalog into the user's folder. Returns the files copied.

    config.json is copied only if missing (it holds the user's choices);
    catalog.db also when the shipped one is newer (an app update).
    """
    copied = []
    if home.resolve() == bundle.resolve():
        return copied  # development: the project folder is both
    for rel in BUNDLED_FILES:
        src, dst = bundle / rel, home / rel
        if not src.exists():
            continue
        newer = rel.endswith(".db") and dst.exists() and src.stat().st_mtime > dst.stat().st_mtime
        if not dst.exists() or newer:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst)
    return copied


def resolve_path(raw: str, base: Path = APP_HOME) -> Path:
    """Expand %VARS%/$VARS/~ and make relative paths relative to the app's folder."""
    p = Path(os.path.expanduser(os.path.expandvars(raw)))
    return p if p.is_absolute() else base / p


def validate_config(cfg: dict[str, Any]) -> None:
    moods = cfg.get("moods")
    if not isinstance(moods, dict) or not moods:
        raise ConfigError("config.moods must be a non-empty object")
    for name, mood in moods.items():
        for axis in ("valence", "energy"):
            value = mood.get(axis)
            if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                raise ConfigError(f"moods.{name}.{axis} must be a number in [0, 1]")
    if cfg.get("target_mood") not in moods:
        raise ConfigError(f"target_mood {cfg.get('target_mood')!r} is not a defined mood")


def load_config(path: Path | str = DEFAULT_CONFIG_PATH, retries: int = 5) -> dict[str, Any]:
    """Load and validate config.json.

    Retries briefly because on Windows a reader can hit a sharing violation
    while another process is in the middle of os.replace().
    """
    path = Path(path)
    for attempt in range(retries):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            break
        except (PermissionError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(0.01 * (attempt + 1))
    validate_config(cfg)
    return cfg


def atomic_write_text(path: Path | str, text: str, retries: int = 10) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Another process has the file open without FILE_SHARE_DELETE.
                if attempt == retries - 1:
                    raise
                time.sleep(0.005 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_write_json(path: Path | str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def set_target_mood(mood: str, path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Select a target mood: writes both the name and its coordinates."""
    cfg = load_config(path)
    if mood not in cfg["moods"]:
        raise ConfigError(f"unknown mood {mood!r}")
    m = cfg["moods"][mood]
    cfg["target_mood"] = mood
    cfg["target"] = {"valence": m["valence"], "energy": m["energy"]}
    atomic_write_json(path, cfg)
    return cfg


def set_paused(paused: bool, path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    cfg = load_config(path)
    cfg["paused"] = bool(paused)
    atomic_write_json(path, cfg)
    return cfg


def target_point(cfg: dict[str, Any]) -> tuple[float, float]:
    mood = cfg["moods"][cfg["target_mood"]]
    return float(mood["valence"]), float(mood["energy"])
