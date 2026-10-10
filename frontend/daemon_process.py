"""Starting and stopping the background daemon from the UI.

The UI owns the daemon: it starts it once a Spotify account is signed in,
restarts it after a log-out or a crash, and stops it on quit. The daemon is a
separate process (a crash in one never takes down the other):

    development     python -m backend.daemon ...
    installed app   AmbientMood.exe --daemon ...   (the same executable, see launcher.py)

Stopping is cooperative: the UI creates a stop file and the daemon shuts down
cleanly (hands the next songs back to Spotify's queue, deletes the session
playlist, restores the display). The daemon also stops by itself if the UI
process disappears. Only if it doesn't exit in time is it killed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from shared.config_io import APP_HOME, FROZEN

log = logging.getLogger(__name__)


def daemon_command(config_path: Path, stop_file: Path, log_file: Path) -> list[str]:
    base = [sys.executable, "--daemon"] if FROZEN else [sys.executable, "-m", "backend.daemon"]
    return base + ["--config", str(config_path), "--parent-pid", str(os.getpid()),
                   "--stop-file", str(stop_file), "--log-file", str(log_file)]


class DaemonProcess:
    def __init__(self, config_path: Path, home: Path = APP_HOME, restart_after_s: float = 30.0,
                 stop_timeout_s: float = 20.0, extra_args: list[str] | None = None):
        self.config_path = Path(config_path)
        self.extra_args = extra_args or []  # e.g. ["--mock", "--no-display"] in tests
        self.stop_file = home / "data" / "daemon.stop"
        self.log_file = home / "data" / "daemon.log"
        self.restart_after_s = restart_after_s
        self.stop_timeout_s = stop_timeout_s
        self.proc: subprocess.Popen | None = None
        self.wanted = False  # should be running (a crash is restarted)
        self.started_at = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.wanted = True
        if self.running:
            return
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        cwd = None if FROZEN else Path(__file__).resolve().parent.parent  # so "-m backend.daemon" resolves
        self.proc = subprocess.Popen(daemon_command(self.config_path, self.stop_file, self.log_file)
                                     + self.extra_args,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags, cwd=cwd)
        self.started_at = time.monotonic()
        log.info("daemon started (pid %d); log: %s", self.proc.pid, self.log_file)

    def stop(self) -> None:
        """Ask the daemon to shut down cleanly; kill it if it doesn't within the timeout."""
        self.wanted = False
        if not self.running:
            self.proc = None
            return
        self.stop_file.touch()
        try:
            self.proc.wait(self.stop_timeout_s)
            log.info("daemon stopped")
        except subprocess.TimeoutExpired:
            log.warning("daemon did not stop in %.0fs; killing it", self.stop_timeout_s)
            self.proc.kill()
            self.proc.wait()
        self.stop_file.unlink(missing_ok=True)
        self.proc = None

    def check(self) -> None:
        """Called regularly: restart a daemon that exited on its own (not more often than restart_after_s)."""
        if self.wanted and not self.running and time.monotonic() - self.started_at >= self.restart_after_s:
            code = self.proc.returncode if self.proc else None
            log.warning("daemon exited (code %s); restarting", code)
            self.proc = None
            self.start()
