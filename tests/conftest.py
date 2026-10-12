import json
import shutil
from pathlib import Path

import pytest

from backend.clock import FakeClock
from backend.features import Features
from backend.spotify.auth import AccountStore
from backend.spotify.client import MockSpotifyClient, Track, demo_catalog

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_config(tmp_path) -> Path:
    path = tmp_path / "config.json"
    shutil.copy(ROOT / "config" / "config.json", path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # Tests expect the defaults, whatever the user last picked in the sidebar.
    cfg["target_mood"], cfg["paused"], cfg["song_source"] = "focused", False, {"kind": "general"}
    cfg["target"] = {"valence": cfg["moods"]["focused"]["valence"], "energy": cfg["moods"]["focused"]["energy"]}
    cfg["paths"] = {"db": str(tmp_path / "mood.db"), "state": str(tmp_path / "state.json"),
                    "tracks_csv": str(tmp_path / "tracks.csv")}
    # Never point tests at the real VS Code / Terminal settings.
    cfg["workspace"]["vscode_settings"] = str(tmp_path / "vscode_settings.json")
    cfg["workspace"]["terminal_settings"] = str(tmp_path / "terminal_settings.json")
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def catalog():
    return demo_catalog()


@pytest.fixture
def mock_client(catalog, clock) -> MockSpotifyClient:
    return MockSpotifyClient(catalog, clock, seed=1)


def make_track(tid: str, duration_s: int = 200) -> Track:
    return Track(tid, f"Song {tid}", "Artist", duration_s * 1000)


def feats(v: float, e: float, a: float = 0.5) -> Features:
    return Features(v, e, a)


@pytest.fixture
def store(tmp_path) -> AccountStore:
    """An empty store of linked Spotify accounts."""
    return AccountStore(tmp_path / "spotify_accounts")
