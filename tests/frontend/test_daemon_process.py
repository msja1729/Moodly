import asyncio
import os
import time

import pytest

from backend.daemon import parent_alive, watch_for_stop
from frontend.daemon_process import DaemonProcess
from shared.config_io import ensure_app_home


def test_ensure_app_home_copies_shipped_files_once(tmp_path):
    bundle, home = tmp_path / "bundle", tmp_path / "home"
    (bundle / "config").mkdir(parents=True)
    (bundle / "data").mkdir()
    (bundle / "config" / "config.json").write_text("{}", encoding="utf-8")
    (bundle / "data" / "catalog.db").write_bytes(b"v1")

    assert len(ensure_app_home(home, bundle)) == 2
    # The user's config is never overwritten; a newer shipped catalog is.
    (home / "config" / "config.json").write_text('{"mine": 1}', encoding="utf-8")
    (bundle / "data" / "catalog.db").write_bytes(b"v2")
    later = time.time() + 10
    os.utime(bundle / "data" / "catalog.db", (later, later))
    assert ensure_app_home(home, bundle) == [home / "data" / "catalog.db"]
    assert (home / "config" / "config.json").read_text(encoding="utf-8") == '{"mine": 1}'
    assert (home / "data" / "catalog.db").read_bytes() == b"v2"


def test_ensure_app_home_does_nothing_in_development(tmp_path):
    assert ensure_app_home(tmp_path, tmp_path) == []


def test_parent_alive():
    assert parent_alive(os.getpid())
    assert not parent_alive(2 ** 22 + 3)  # no such process


def test_stop_file_cancels_the_task(tmp_path):
    stop = tmp_path / "daemon.stop"

    async def main():
        task = asyncio.create_task(asyncio.sleep(30))
        watch_for_stop(asyncio.get_running_loop(), task, stop, None, interval_s=0.05)
        await asyncio.sleep(0.1)
        stop.touch()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    asyncio.run(main())


def test_daemon_process_starts_and_stops_cleanly(tmp_config, tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIENT_MOOD_HOME", str(tmp_path))  # the child keeps its files here
    d = DaemonProcess(tmp_config, home=tmp_path, extra_args=["--mock", "--no-display", "--seed", "1"])
    d.start()
    try:
        deadline = time.monotonic() + 30
        state = tmp_path / "state.json"
        while not state.exists() and time.monotonic() < deadline and d.running:
            time.sleep(0.2)
        assert d.running, (tmp_path / "data" / "daemon.log").read_text(encoding="utf-8", errors="replace")
        assert state.exists()
    finally:
        d.stop()
    assert not d.running
    log = (tmp_path / "data" / "daemon.log").read_text(encoding="utf-8")
    assert "stop requested" in log and "stopped; display restored" in log


def test_only_one_live_daemon(tmp_path):
    from backend.daemon import acquire_single_instance

    first = acquire_single_instance(tmp_path / "daemon.lock")
    assert first is not None
    assert acquire_single_instance(tmp_path / "daemon.lock") is None
    first.close()
    again = acquire_single_instance(tmp_path / "daemon.lock")
    assert again is not None
    again.close()


def test_config_with_a_byte_order_mark_loads(tmp_config):
    from shared.config_io import load_config

    tmp_config.write_bytes(b"\xef\xbb\xbf" + tmp_config.read_bytes())
    assert load_config(tmp_config)["target_mood"] == "focused"
