# PyInstaller spec: builds dist/AmbientMood/AmbientMood.exe (run packaging/build.ps1).
# One executable for both roles: the tray app, and the daemon with --daemon (see launcher.py).
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "config" / "config.json"), "config"),  # default settings, copied to %APPDATA% on first run
        (str(ROOT / "data" / "catalog.db"), "data"),       # the song catalog
    ],
    hiddenimports=collect_submodules("backend") + collect_submodules("frontend") + collect_submodules("recommender")
    + collect_submodules("watchdog.observers"),
    excludes=["pytest", "pytestqt", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AmbientMood",
    console=False,  # a tray app: no console window
)
coll = COLLECT(exe, a.binaries, a.datas, name="AmbientMood")
