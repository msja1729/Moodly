#!/usr/bin/env bash
# Builds the macOS release: dist/AmbientMood-<version>-mac-<arch>.dmg
#
#   packaging/build_mac.sh 0.2.0
#
# Must run on a Mac (PyInstaller can't build a Mac app on Windows). The GitHub
# workflow .github/workflows/release-mac.yml runs it on GitHub's Mac machines.
# 1. creates .venv-mac and installs the dependencies
# 2. imports the song catalog if data/catalog.db is missing
# 3. runs the tests (Qt offscreen)
# 4. PyInstaller -> dist/AmbientMood.app
# 5. smoke test: the packaged daemon runs a mock session and stops cleanly
# 6. hdiutil -> a .dmg with the app and a link to /Applications
set -euo pipefail
VERSION="${1:?usage: build_mac.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ARCH="$(uname -m)"   # arm64 (Apple silicon) or x86_64 (Intel)

python3 -m venv .venv-mac
PY="$ROOT/.venv-mac/bin/python"
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements-build.txt

if [ ! -f data/catalog.db ]; then
  echo "data/catalog.db is missing; importing the song catalog..."
  "$PY" -m backend.catalog import
fi

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  QT_QPA_PLATFORM=offscreen "$PY" -m pytest -q
fi

rm -rf build dist/AmbientMood dist/AmbientMood.app
APP_VERSION="$VERSION" "$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/AmbientMood.spec

# Smoke test: the packaged daemon (mock Spotify, no display) runs a session and stops cleanly.
HOME_DIR="$(mktemp -d)"
"$PY" - "$HOME_DIR" <<'PY'
import json, pathlib, sys
home = pathlib.Path(sys.argv[1]) / "config"
home.mkdir(parents=True)
cfg = json.loads(pathlib.Path("config/config.json").read_text(encoding="utf-8"))
cfg["paused"] = False  # a session should run in the smoke test
(home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
PY
AMBIENT_MOOD_HOME="$HOME_DIR" dist/AmbientMood.app/Contents/MacOS/AmbientMood --daemon --mock --no-display \
  --seed 1 --duration 60 --log-file "$HOME_DIR/daemon.log"
grep -q "session started" "$HOME_DIR/daemon.log"
grep -q "stopped; display restored" "$HOME_DIR/daemon.log"
echo "smoke test passed"

DMG="dist/AmbientMood-${VERSION}-mac-${ARCH}.dmg"
STAGE="$(mktemp -d)"
cp -R dist/AmbientMood.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Ambient Mood" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "Built $DMG"
