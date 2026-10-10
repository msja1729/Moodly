# Ambient Mood Regulator

A background daemon plus a tray sidebar. The daemon maps every song you listen to on Spotify to a point (valence, arousal). Your current mood is the **average point of the songs in your queue**. It then slowly nudges your environment toward the mood you picked:

- **Audio (session playlist):** at the start of a session your current song and queue move into a private "Ambient Mood session" playlist, which keeps playing seamlessly. On a regular cycle, the daemon:
  - searches a ~90k-track catalog for songs one Markov-chain step toward your target that also fit musically (tempo, key, loudness)
  - inserts them at random positions in the middle of the upcoming songs, so you hear them soon
  - removes songs far from both your mood and the target (and, when you change the mood, up to 5 songs that don't fit the new one)

  The playlist is deleted when the session ends.
- **Workspace:** it hot-swaps the VS Code theme and the Windows Terminal color scheme, and keeps any comments in those settings files.
- **Display:** it ramps the screen color temperature by 100 K per minute.

Every song change is logged to SQLite with whether it was skipped. Skip rates feed back into the Markov chain.

See `SPEC.md.md` for requirements, `PLAN.md` for the design plan, `MODIF.md` for the change log and `docs/MANUAL.md` for the full user manual.

## Project layout

```
README.md            this file
SPEC.md.md           requirements
PLAN.md              design plan (Phase 1 built, Phase 2 planned)
MODIF.md             change log
LOG.md               open problems to fix
requirements.txt     Python dependencies
requirements-build.txt  + PyInstaller, for building the release
launcher.py          entry point of the packaged AmbientMood.exe (tray app, or daemon with --daemon)
packaging/
  AmbientMood.spec   PyInstaller build
  installer.iss      Inno Setup installer
  build.ps1          builds the release: tests -> exe -> installer
pytest.ini           test settings
.env.example         optional development overrides (copy to .env)
config/
  config.json        moods, tunables, paths and the public Spotify Client ID
docs/
  MANUAL.md          user manual
backend/             the daemon (headless)
  daemon.py          main loop and orchestration
  inference.py       Spotify polling and mood estimation
  mood_space.py      Markov chain over valence x energy
  features.py        audio-feature providers (Spotify -> ReccoBeats -> CSV)
  catalog.py         the ~90k-track database: import and fast search
  selection.py       which songs to add/remove and where (pure functions)
  feedback.py        SQLite telemetry
  clock.py           real / accelerated / fake clocks
  spotify/
    client.py        live Spotify client and the scripted mock
    auth.py          PKCE sign-in, Premium check, linked accounts, log out
  actuators/
    session.py       the session playlist: hand-over, add/remove cycles, hand back + delete
    audio.py         Phase 1 mode: bridging tracks appended to the queue
    workspace.py     VS Code / Windows Terminal themes
    display.py       screen color temperature
frontend/            the tray app (PyQt6)
  app.py             tray icon, glass sidebar window, startup
  components.py      sidebar widgets (mood tiles, mood map, account + Log out)
  onboarding.py      Spotify account window (choose / connect / Premium check)
  daemon_process.py  starts, restarts and cleanly stops the daemon process
shared/
  config_io.py       atomic config reads/writes used by both processes; where files live
data/                runtime files (git-ignored): mood.db, state.json, spotify_accounts/, daemon.log, ...
tests/
  conftest.py        shared fixtures
  backend/           daemon, actuators, Spotify auth tests
  frontend/          tray UI and account window tests
```

## Setup (Windows)

```powershell
py -3.13 -m venv .venv          # use python.org Python, NOT Anaconda (its DLLs break PyQt6)
.venv\Scripts\python -m pip install -r requirements.txt
```

### Spotify (Premium required for playback and queue)
Users don't need any keys. Sign-in uses Spotify's PKCE flow, so the app ships **no client secret**: each user signs in to their own account and gets their own token, stored only on their computer.

The app only needs the public **Client ID** of a registered Spotify app, set once by the developer in `config/config.json`:

```json
"spotify": { "client_id": "<Client ID>", "redirect_uri": "http://127.0.0.1:8888/callback" }
```

(Create the app at https://developer.spotify.com/dashboard with that redirect URI. `SPOTIPY_CLIENT_ID` / `SPOTIPY_REDIRECT_URI` in `.env` override these for development.)

1. Start the tray app (`python -m frontend.app`). Every launch opens a **Spotify account** window:
   - If accounts are already linked on this computer, it lists them. **Select one** to use it, or click **Connect another account**.
   - If none are linked yet, it explains Premium is required. Click **Connect Spotify**, choose your account in the browser (use *Not you?* to switch accounts), and approve access.

   The app then checks the account's plan:
   - **Premium:** the account is linked (saved in `data/spotify_accounts/`) and becomes active; bridging tracks go into that account's queue.
   - **Not Premium:** the window says *You don't have Spotify Premium*, nothing is saved, and you can try a different account or quit.
2. The sidebar's top-right corner shows the account in use, with a **Log out** link (also in the tray icon's right-click menu). Log out forgets that account on this computer and reopens the account window so you can pick another one.
3. The live daemon only starts once a Premium account is active, and it follows the active account if you switch.

### Track catalog (recommended)

```powershell
.venv\Scripts\python -m backend.catalog import    # downloads ~20 MB, stores 89,741 tracks in data/catalog.db
```

New songs are picked from this catalog plus your own top and saved tracks. Without it, only your library is used, and the mood settles less precisely.

### Audio features
Spotify's `/audio-features` endpoint returns 403 for apps created after November 2024, so features come from a fallback chain: **Spotify → ReccoBeats → local CSV**. Every result is cached in `data/mood.db`.

For a much bigger pool of bridging tracks, download a dataset with `track_id, valence, energy, acousticness` columns (e.g. Kaggle's *Spotify Tracks Dataset*) and save it as `data/tracks.csv`. Without it, bridging tracks come only from your top tracks and saved tracks.

## Run

```powershell
.venv\Scripts\python -m frontend.app              # the tray UI; it starts and stops the daemon itself
```

The daemon runs as a separate background process and logs to `data/daemon.log`. To watch its log live while developing, run it yourself instead:

```powershell
.venv\Scripts\python -m frontend.app --no-daemon  # terminal 1: UI only
.venv\Scripts\python -m backend.daemon            # terminal 2: daemon with its log in the console
```

Only one live daemon can run at a time; a second one exits with a message.

Play something in Spotify, then click the tray icon to open the sidebar and pick **Focused / Calm / Energized / Wind Down**. The session starts right away (or as soon as music plays). Pause nudging, quitting the daemon, or logging out ends it: the next 10 songs go back to your queue and the session playlist is deleted. The UI writes `config/config.json`, and the daemon's file watcher reacts within ~50 ms. The UI shows the live mood estimate from `data/state.json`.

Useful daemon flags:

| Flag | Effect |
|---|---|
| `--mock` | Scripted Spotify session sped up 30×. Needs no credentials. Theme edits go to sandbox files in `data/mock_workspace/`. |
| `--real-workspace` | In mock mode, edit the real VS Code/Terminal settings instead of the sandbox copies. |
| `--no-display` | Never touch the screen gamma. |
| `--duration N` | Stop after N seconds (simulated seconds in mock mode). |
| `-v` | Debug logging. |

## Development and releases

There is one codebase with two branches:

| Branch | Use |
|---|---|
| `dev` | Day-to-day work. Change, run from source and test here. |
| `main` | Released code only. Every release is built from `main` and tagged `vX.Y.Z`. |

**Development copy:** run from source as above. It uses `config/` and `data/` inside the project folder, so your test data never mixes with a user's.

**Released copy:** an installer users download from GitHub Releases. The installed app keeps its settings, accounts and logs in `%APPDATA%\AmbientMood` (copied from the shipped defaults on first run), because `C:\Program Files` is read-only. The song catalog is shipped inside the installer. Set `AMBIENT_MOOD_HOME` to point either copy at another folder.

**Making a release:**

```powershell
git switch main
git merge dev                                   # bring in the tested work
.\packaginguild.ps1 -Version 1.1.0            # tests -> dist\AmbientMood\ -> dist\AmbientMood-Setup-1.1.0.exe
git tag v1.1.0
git push origin main --tags
gh release create v1.1.0 dist\AmbientMood-Setup-1.1.0.exe --title "Ambient Mood 1.1.0" --notes "What changed"
git switch dev
```

The installer step needs Inno Setup 6 (`winget install JRSoftware.InnoSetup`); without it `build.ps1` builds only the `dist\AmbientMood\` folder.

**Spotify limit for other users:** the Spotify app behind `spotify.client_id` is in Spotify's *Development mode*, which only lets in the Spotify accounts you add under *User Management* in the [developer dashboard](https://developer.spotify.com/dashboard). Add each user's Spotify email there before they sign in. Opening the app to everyone needs Spotify's extended-quota approval; check Spotify's current rules first.

## Tuning (`config/config.json`)
- `moods.*`: coordinates, VS Code theme, Terminal scheme and Kelvin for each mood.
- `markov`: `grid_size`, `beta` (pull toward the target), `gamma` (penalty for big jumps).
- `audio.mode`: `"session"` (default: the playlist session) or `"queue"` (Phase 1: append one bridging track at a time).
- `session`: `cycle_s` (90), `window_size` (20), `target_length` (15), `min_length` (8), `max_adds_per_cycle` (3), `max_removals_per_cycle` (2), `remove_radius_mood`/`remove_radius_target` (0.35), `search_radius` (0.08), the musical weights `w_tempo`, `w_key`, `w_loudness`, `w_feel`, `w_energy_jump`, `mmr_lambda`, `settle_radius`, `allow_explicit`, `handback_count`.
- `audio.deviation_threshold` (default 0.2, queue mode): bridging starts only when the mood is further than this from the target.
- `audio.cooldown_s`: minimum gap between bridging tracks.
- `inference`: `ema_alpha`, `skip_weight`, `skip_threshold`, and the poll range `poll_min_s`/`poll_max_s` (30–60 s).
- `display`: `step_kelvin`, `step_interval_s`, `min_kelvin`, `max_kelvin`.

## Notes
- **Screen warmth limit:** by default Windows rejects gamma ramps warmer than about 3400–4000 K. The daemon detects this and stops at the warmest value Windows accepts. To unlock the full range, set the DWORD `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\GdiIcmGammaRange` to `256`, then sign out and back in.
- **Windows Terminal:** the daemon sets `profiles.defaults.colorScheme`. A profile that sets its own `colorScheme` keeps it.
- **Backups:** the first theme edit in each daemon session writes a `settings.json.bak` next to the file being edited.
- **Telemetry:** the `events` table holds `[ts, valence, energy, intervention_type, skipped, ...]`. `FeedbackLog.skip_rate_by_intervention()` compares skip rates for bridging tracks against normal tracks.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The suite covers Markov chain math, bridging-track selection, JSONC edits (comments, trailing commas, BOM, CRLF), display ramps, the feature fallback chain, skip detection, telemetry, a simulated 10-minute daemon session with injected 429s and network errors, the live config watcher, and UI clicks via pytest-qt (each click must update `config.json` in under 100 ms).
