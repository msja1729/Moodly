# PLAN.md — Ambient Mood Regulator

> Status: **Phase 1 implemented** (2026-10-07; sections 0–6 below). **Phase 2 (queue-average mood and queue reshaping) implemented against the simulated Spotify** (2026-10-08; section 7; live test on a real account still to do, §7.11 step 1). Change history is in `MODIF.md`.

## 0. Context

`SPEC.md.md` describes a local Python app with two parts:
- A **daemon** that works out your current mood from what you're playing on Spotify. It places the mood as a point in a 2D space: valence (how positive the music sounds) against energy. It then nudges you toward a target mood in three ways: it queues "bridging" tracks chosen by a Markov chain, it switches your VS Code and Windows Terminal themes, and it changes your screen color temperature.
- A **tray/sidebar UI** where you pick the target mood. The UI writes the choice to `config.json`.

The project folder is empty apart from the spec, so everything gets built from scratch. Target platform: **Windows 11**, Python 3.10+.

### ⚠️ Problems found in the spec, with my proposed fixes (please review)

| # | Problem | Proposed fix |
|---|---|---|
| R1 | **Spotify deprecated `GET /audio-features` and `GET /recommendations` for new apps (Nov 2024).** A newly registered app gets HTTP 403 from both. Without this, the valence/energy/acousticness inputs the spec relies on are not available. | Add a pluggable `FeatureProvider` with a fallback chain: (1) Spotify audio-features, which still works for older apps; (2) **ReccoBeats** API, a free service that returns the same features from a Spotify track ID; (3) a **local CSV dataset**, such as the Kaggle "Spotify Tracks" dataset (~100k+ tracks with valence/energy/acousticness). Results are cached in SQLite. |
| R2 | The spec doesn't say where bridging tracks come from, and `/recommendations` is gone (see R1). | Build a **candidate pool** from your saved tracks, top tracks and playlists, plus the local dataset. Every track in the pool gets features from R1 and is indexed by (valence, energy). |
| R3 | `add_to_queue` and the currently-playing endpoints need **Spotify Premium**. | The real client needs Premium. All development and tests run against a **mock Spotify client**, so nothing is blocked if you don't have Premium. |
| R4 | Polling every 30–60 s can't see skips directly. | Detect skips with a heuristic. When the track ID changes, estimate how far the previous track got (last `progress_ms` + time elapsed since that poll). If it got through less than ~80% of its duration, count it as skipped. Also schedule the next poll near the expected end of the track, still clamped to the 30–60 s range. |
| R5 | On Windows, `sbc` only controls brightness, not color temperature. | Change color temperature with the Win32 `SetDeviceGammaRamp` call through `ctypes`, converting Kelvin to RGB multipliers. Some machines restrict the gamma range; on those I'll document a registry tweak or clamp the range. Linux `xrandr` would be an optional extra backend. |
| R6 | VS Code's `settings.json` is **JSONC**: it can contain comments and trailing commas. A normal `json.load` → `json.dump` round-trip deletes the comments. | Edit the file surgically: parse it tolerantly only to validate, then change just the value of the `workbench.colorTheme` key in the original text (or insert the key if it's missing). Write to a temp file, then swap it in with `os.replace`. Windows Terminal (`…\LocalState\settings.json`, `profiles.defaults.colorScheme`) gets the same treatment. |
| R7 | The spec puts API keys in `config.json`, but the UI writes to that same file all the time. | Keep secrets in a `.env` file (loaded with `python-dotenv`). `config.json` holds moods, coordinates, paths and tunables. I'll add a `.gitignore` too. |
| R8 | Naming inconsistencies: the spec file is called `SPEC.md.md`, and the acceptance criteria say `actuator_workspace.py` while the file list says `actuators/workspace.py`. | Use the file list's names. Optionally rename the spec to `SPEC.md`. |

UI framework: **PyQt6**. It has a native tray icon (`QSystemTrayIcon`), frameless sidebar windows, and `pytest-qt` for automated click tests. CustomTkinter would need `pystray` for the tray, and its click events are harder to test.

---

## 1. Project structure

Files the spec asks for are unmarked. `+` marks helper files I'm adding.

```
Mood/
├── PLAN.md, SPEC.md, README.md+, .env.example+, .gitignore+
├── config.json                 # moods, coordinates, themes, Kelvin, tunables, target_mood
├── requirements.txt
├── shared/                     +
│   └── config_io.py            + load/validate/atomic-write config.json (used by UI & daemon)
├── frontend/
│   ├── app.py                  # QApplication, tray icon, sidebar show/hide
│   └── components.py           # MoodButton, Sidebar layout, QSS styling
├── backend/
│   ├── daemon.py               # asyncio main loop, orchestration, watchdog bridge
│   ├── spotify_client.py       + Protocol + RealSpotifyClient (spotipy) + MockSpotifyClient
│   ├── features.py             + FeatureProvider chain (Spotify → ReccoBeats → CSV) + SQLite cache
│   ├── inference.py            # polling, skip detection, EMA mood vector
│   ├── mood_space.py           + 2D grid, Markov transition matrix (pure math)
│   ├── feedback.py             # SQLite telemetry + skip-rate evaluation
│   └── actuators/
│       ├── audio.py            # pacing + bridging-track selection + queue injection
│       ├── workspace.py        # VS Code + Windows Terminal JSONC hot-swap
│       └── display.py          # Kelvin → gamma ramp (Windows), xrandr (Linux)
├── data/                       + tracks.csv (optional dataset), mood.db (SQLite)
└── tests/
    ├── conftest.py             + fixtures: tmp config, mock client, fake clock
    ├── test_ui.py
    ├── test_inference.py
    └── test_actuators.py       # includes Markov chain math tests
```

## 2. Core design

### 2.1 Mood space and targets (`config.json`)
- A mood is a vector **m = (valence, energy) ∈ [0,1]²**. Acousticness is used only as a tiebreaker when choosing among candidate tracks.
- Default targets (these are starting values and you can tune them):

| Mood | (v, e) | VS Code theme | Terminal scheme | Kelvin |
|---|---|---|---|---|
| Focused | (0.50, 0.45) | Default Dark Modern | Campbell | 5500 |
| Calm | (0.60, 0.25) | Quiet Light / Solarized | One Half Light | 4500 |
| Energized | (0.80, 0.85) | Default High Contrast / vibrant | Vintage | 6500 |
| Wind Down | (0.40, 0.15) | Solarized Dark | Dark+ | 3500 |

### 2.2 Inference (`inference.py`)
- `poll()` gets the current track and its features from the provider chain.
- **Mood estimate**: an exponential moving average over recent tracks, `m_t = α·f_track + (1−α)·m_{t−1}` with α≈0.3. Skipped tracks get a reduced weight (×0.3) because skipping signals you didn't want that mood.
- Detects track changes and decides skipped vs. completed (R4), then sends a `TrackChanged(prev, skipped)` event to the daemon.

### 2.3 Markov chain (`mood_space.py`): the core requirement
- Split the space into an **N×N grid** (default N=10, so 100 states, each cell 0.1 wide).
- From state *i*, the chain can only move to cells within **Chebyshev radius 1** (itself plus up to 8 neighbors). That keeps shifts gradual: at most ~0.14 per step.
- Transition probabilities:
  `P[i→j] ∝ exp(−β · d(j, target)) · exp(−γ · d(i, j)²) · w_skip(i→j)`
  - β pulls the chain toward the target, and γ penalizes big jumps.
  - `w_skip` is a **learned factor from feedback**: a Beta(1+completions, 1+skips) mean for each direction (8 compass directions + stay). Directions that keep getting skipped become less likely.
- Rows are normalized and the target cell is absorbing. Functions: `build_transition_matrix(target, params, skip_stats)`, `next_waypoint(current, rng)`, and `expected_steps_to_target()`, which uses the fundamental matrix and helps both testing and the UI.

### 2.4 Audio actuator (`actuators/audio.py`)
- Triggered on each `TrackChanged` event, or on a target change, **only if ‖m − target‖₂ > 0.2** (acceptance criterion).
- Steps: sample a waypoint cell from P[current cell] → take the cell's center as the waypoint coordinates → run a nearest-neighbor search over the candidate pool in (v, e), using acousticness as the tiebreaker and excluding recently played/queued tracks → `add_to_queue(uri)`.
- **Pacing**: at most one injected track per natural track change, with a cooldown so the queue isn't flooded. Every injection is logged with `intervention_type="audio_bridge"`.

### 2.5 Workspace actuator (`actuators/workspace.py`)
- `apply_theme(mood)` → `set_jsonc_key(path, key_path, value)` (R6). VS Code and Windows Terminal both reload the theme on their own when the file changes.
- Writes are atomic and keep a `.bak` copy. If the result doesn't re-parse, the original is restored. This runs in a thread and takes under 500 ms.
- Runs when the target mood changes, and again once the estimate gets within 0.1 of the target. Changes are hysteretic: the theme won't flip back and forth when the estimate hovers near a boundary.

### 2.6 Display actuator (`actuators/display.py`)
- `kelvin_to_rgb(k)` uses the Tanner Helland approximation to get R/G/B multipliers.
- `set_temperature(k)` writes a gamma ramp through `ctypes` (`GetDC` + `SetDeviceGammaRamp`). The temperature **ramps gradually** (e.g. 100 K per minute) toward the target Kelvin rather than jumping. The original ramp is restored when the daemon exits.
- Backend chosen by platform: Windows gamma, Linux `xrandr --gamma`, or a no-op fallback.

### 2.7 Feedback (`feedback.py`)
- SQLite `events(id, ts, valence, energy, intervention_type, track_id, skipped)` covers the acceptance criterion `[timestamp, current_mood_vector, intervention_type, skipped_boolean]`.
- An event is written on every song change. The intervention type is `none`, `audio_bridge`, `theme`, or `display`.
- `skip_stats()` returns per-direction counts that feed into `w_skip`, and `skip_rate_by_intervention()` is for evaluation. A CSV export is optional.

### 2.8 Daemon (`daemon.py`)
- Runs on `asyncio`. The `spotipy` calls are blocking, so they run in `asyncio.to_thread`.
- Tasks:
  - a poll loop (30–60 s with jitter, scheduled near track end; R4)
  - a display ramp loop
  - a config-watch bridge: the `watchdog` observer thread hands events to the main loop via `loop.call_soon_threadsafe`, debounced at about 50 ms
- Errors and HTTP 429 responses get exponential backoff and respect the `Retry-After` header; the daemon logs them and keeps running.
- Shuts down cleanly on SIGINT, restoring the gamma ramp first.
- `--mock` flag: uses `MockSpotifyClient` and a fake clock.

### 2.9 Frontend (`frontend/`)
- A `QSystemTrayIcon`. Clicking it opens a frameless, rounded **sidebar** anchored to the bottom-right above the taskbar, styled like the Windows control center.
- The sidebar has 4 toggle tiles (only one active at a time). It also shows the current mood estimate, read from a small `state.json` that the daemon writes, plus a Pause toggle.
- A click calls `config_io.set_target_mood()`, which does an atomic write in well under 100 ms. The UI never blocks on the daemon.

## 3. Implementation phases (in order)

1. **Scaffold**: folders, `requirements.txt` (PyQt6, spotipy, watchdog, python-dotenv, httpx, pytest, pytest-qt, pytest-asyncio), `config.json`, `.env.example`, venv, and an empty pytest run.
2. **Config I/O**: `shared/config_io.py` (schema check, atomic write) plus tests.
3. **Markov math**: `mood_space.py` plus thorough tests. This is pure logic, so I'll build it first.
4. **Spotify client + features**: the Protocol, a mock client with scripted sessions, the real client using spotipy OAuth (scopes: `user-read-currently-playing`, `user-read-playback-state`, `user-modify-playback-state`, `user-library-read`, `user-top-read`), the provider chain, and the cache.
5. **Inference**: EMA and skip detection, with tests.
6. **Feedback**: SQLite logger, with tests.
7. **Audio actuator**: candidate pool, waypoint → track selection, pacing, with tests.
8. **Workspace actuator**: JSONC surgical edit, with tests against mock VS Code and Windows Terminal files (including comments and trailing commas).
9. **Display actuator**: Kelvin conversion tests; the gamma call itself is tested manually.
10. **Daemon orchestration**: wire it together, add the watcher, and run the 10-minute mocked session.
11. **Frontend**: tray, sidebar, buttons, and pytest-qt tests.
12. **End-to-end + README**: manual run with real Spotify (if you have Premium) and setup docs.

## 4. Test plan (maps to acceptance criteria)

| Acceptance criterion | Test |
|---|---|
| Tray launches, sidebar opens on click | `test_ui.py`: tray icon visible; simulating `activated` shows the sidebar |
| Mood click → config.json < 100 ms | `test_ui.py`: `qtbot.mouseClick` on each button, check the file contents and that elapsed time < 0.1 s |
| 10-min mocked session, no crash | `test_inference.py`: `MockSpotifyClient` with 15+ tracks, skips, 429s and null playback, under a fake clock (finishes in seconds) |
| Deviation > 0.2 → bridging track queued | `test_actuators.py`: queued track lies between current and target and moves closer to the target; nothing is queued when deviation ≤ 0.2 |
| Workspace JSON edit without corruption | `test_actuators.py`: other keys and comments survive byte-for-byte; theme changed; bad file → restored |
| Telemetry row on song change | `test_inference.py` / feedback test: row has ts, (v, e), intervention_type, skipped |
| Markov math | rows sum to 1; only radius-1 moves; probability mass drifts toward the target; target is absorbing; expected steps are finite; skip stats lower probabilities |

## 5. Verification
- `pytest -q`: everything green.
- `python -m backend.daemon --mock`: watch the logs as the mood estimate converges, tracks get queued, and theme/display changes happen.
- `python -m frontend.app`: click moods and confirm the daemon logs pick up the new target immediately.
- Manual: the VS Code theme switches live; the screen gets warmer on Wind Down and is restored on exit.
- Real run (optional): `.env` with Spotify credentials, listen for ~15 minutes, then inspect `data/mood.db`.

## 6. Open questions for you
1. Do you have **Spotify Premium**? It's needed for the real queue and playback APIs; otherwise the real integration stays mock-only.
2. Is your Spotify developer app new (registered after Nov 2024)? If so, audio-features is blocked, and we'll use ReccoBeats or the CSV dataset.
3. Are you OK with **PyQt6** and with storing secrets in `.env` instead of `config.json`?
4. Do you have preferred themes and color schemes for each mood?


---

## 7. Phase 2: queue-average mood and queue reshaping (planned 2026-10-08)

### 7.1 Goal (as requested)
- Everything the user listens to on Spotify (songs now; podcasts too) is turned into a **2D point (valence, arousal)**.
- The user chooses a **target point**.
- The user's **current mood is the average of the points of the songs in their queue**, at any moment.
- On a regular cycle, the backend:
  1. searches the **whole track database** for songs close to the current mood (and a step toward the target), which also **match musically** (tempo, key, loudness, ...),
  2. **inserts** them into the queue **at good positions in between** the existing songs,
  3. **removes** queued songs that are **far from both the current mood and the target**.
- The queue therefore drifts toward the target, step by step.
- **Music never stops** unless the user stops it in Spotify. Everything happens in the user's own Spotify account.

### 7.2 What Spotify allows (findings, 2026-10-08)
| Need | Spotify Web API | Consequence |
|---|---|---|
| Read the queue | `GET /me/player/queue`: currently playing + upcoming items (about 20) | OK |
| Add to the queue | `POST /me/player/queue`: **appends to the end only** | Can't insert at a position |
| Remove from / reorder the queue | **No endpoint** (long-standing developer request, still unsupported) | Can't remove |
| Edit a playlist | `POST/DELETE/PUT /playlists/{id}/items`: insert **at any position**, remove, reorder | Full control |
| Start playback of a playlist at a given song and millisecond | `PUT /me/player/play` with `context_uri`, `offset`, `position_ms` | Seamless hand-over |

Also from Spotify's **February 2026 changes** for development-mode apps:
- `/playlists/{id}/tracks` was renamed to `/playlists/{id}/items`, and creating a playlist is `POST /me/playlists`. spotipy still calls the old paths, so our client calls the new ones directly.
- The **`product` field (premium/free) was removed from `/me`**. Our Premium check relies on it (see 7.8). Spotify has postponed enforcing these removals for apps created before 11 Feb 2026, which is why it still works for our app today.
- Development mode allows **5 users** (allowlist), and the app owner needs Premium. Refresh tokens expire **6 months** after sign-in.

**Proposed approach (A): a managed session playlist.** Because the real queue can't be edited, the app plays a private playlist it owns in the user's account, **"Ambient Mood session"**, and treats that playlist's upcoming songs as "the queue":
1. **Hand-over:** when a session starts, the app reads the current song + upcoming queue, writes them into the session playlist (current song first), and starts the playlist **at the same song and the same millisecond** (`position_ms`). The music continues without a gap; the user only sees the context change to "Ambient Mood session".
2. From then on, insertions and removals are playlist edits at exact positions.
3. Songs the user adds with Spotify's own "Add to queue" still play first. They are read (and counted in the mood average) but never removed, since Spotify doesn't allow it.
4. If the user starts something else (another playlist/album), the app **stops managing** and says so in the sidebar; it does not fight the user. It never pauses or stops playback.

**Fallback (B), only if (A) misbehaves on real devices:** append-only queue plus **auto-skip** of far songs when they start (emulated removal; the user may hear a fraction of a second). No insertion at positions.

**Risk to verify first (spike):** that Spotify clients pick up edits to the playlist that is currently playing for the upcoming songs. Mitigation: every cycle compares Spotify's reported "next" song with the playlist's; on mismatch it re-syncs at the next track boundary.

### 7.3 Mood model
- **Point of a song:** `(valence, arousal)` in [0, 1]². **Arousal = Spotify's `energy`** feature (the standard arousal proxy).
- **Point of a podcast episode** (no audio features exist): estimated from the episode title + description with the **NRC VAD lexicon** (valence/arousal scores for ~20k English words; free for research use). The mean over matched words is rescaled to [0, 1]. If the lexicon file is absent, or too few words match, the episode is left out of the average. Episodes are never inserted or removed by the app.
- **Listening window W** = the current song + the upcoming items (session playlist + Spotify's own queue), up to `window_size` (default 20).
- **Current mood** `m = mean(point(i) for i in W with a known point)`. This is exactly the average requested. (A weighted variant, closer songs counting more, can be switched on in config, but the default is the plain average.)
- **Deviation** `d = ‖m − t‖` for target `t`. Skips are still detected and logged (telemetry and Markov `w_skip`), but no longer change the mood estimate.

### 7.4 The database (catalog)
- **Source:** the public *Spotify Tracks Dataset* (Hugging Face `maharshipandya/spotify-tracks-dataset`, about 114,000 tracks, 20 MB CSV, no login). Columns used: `track_id, track_name, artists, track_genre, popularity, duration_ms, explicit, valence, energy, tempo, key, mode, loudness, danceability, acousticness, instrumentalness, speechiness, time_signature`.
- **Import:** `python -m backend.catalog import` downloads it into the SQLite table `catalog` in `data/mood.db` (one-time, about a minute).
- **Plus the user's own music:** top and saved tracks get features through the existing provider chain (Spotify → ReccoBeats → catalog) and are added with `source = "library"`. Familiar songs get a small bonus when ranking.
- **In memory:** a NumPy matrix of the normalized features for vectorized search. Brute force over 114k rows takes a few milliseconds, so no extra index library is needed.

### 7.5 Picking songs (each cycle)
1. **Waypoint (Markov chain, unchanged core requirement):** sample the next grid cell from P[cell(m)] toward the target. The waypoint `w` lies one step from `m` toward `t`. Adding songs near `w` (not exactly at `m`) is what moves the average.
2. **Retrieval (radius search):** catalog songs within radius `r` (default 0.08) of `w` in (valence, arousal). `r` grows ×1.5 until at least `min_candidates` (200) are found. Filters:
   - strictly closer to the target than `m` (as in Phase 1)
   - not in the window, not played in the last `recent_exclusion` songs, not removed earlier in this session
   - not skipped by this user before; explicit tracks only if `allow_explicit`
3. **Ranking (lower score is better):**
   `score(c) = ‖p_c − w‖ + w_tempo·D_tempo + w_key·D_key + w_loud·D_loud + w_feel·D_feel − w_fam·familiar(c) − w_pop·popularity(c)`
   - `D_tempo` = `min over k ∈ {½, 1, 2} of |log2(tempo_c · k / tempo_ref)|`. Half or double time counts as a match.
   - `D_key` = distance on the **Camelot wheel** (same key or relative major/minor = 0, adjacent = small, far = large), / 6.
   - `D_loud` = loudness difference (dB) / 20. `D_feel` = mean |Δ| of danceability and acousticness.
   - The "ref" values are those of the songs around the slot where `c` would be inserted (7.6), so each pick fits its neighbours.
4. **Variety (MMR, maximal marginal relevance):** from the best 50, pick songs one by one, maximizing `λ·relevance − (1−λ)·max similarity to songs already picked` (λ = 0.7). Same-artist pairs count as very similar. This avoids five near-identical songs.
5. **How many:** enough to keep the window at `target_length` (default 15) upcoming songs, at most `max_adds_per_cycle` (3), so changes stay gradual.
6. **At the target** (`d ≤ settle_radius`, default 0.05): maintenance only. The app adds songs near `t` to keep the queue full and removes nothing that is close to `t`.

### 7.6 Where to insert (positions "in between")
- Positions 0 (now playing) and 1 (next, already buffered by Spotify) are never touched.
- **Trajectory order:** among upcoming songs, picks closer to the current mood go earlier and picks closer to the target go later. Ordering is by projection onto the direction `m → t`, so the queue reads as a smooth path from now to the target.
- **Cheapest insertion:** within the slots allowed by that order, choose the gap (a, b) minimizing `T(a, c) + T(c, b) − T(a, b)`, where `T` is the transition cost between consecutive songs (tempo, key, arousal and loudness jumps, same weights as 7.5). This is the classic cheapest-insertion heuristic from route planning.

### 7.7 What to remove
- Upcoming song `j` is removed if it is **far from both**: `‖p_j − m‖ > remove_radius_mood` **and** `‖p_j − t‖ > remove_radius_target` (defaults 0.35 and 0.35).
- **Protected:** now playing, next, podcasts, songs in Spotify's own queue (can't be removed), and songs without a known point.
- At most `max_removals_per_cycle` (2). Never let the window drop below `min_length` (8). Removals are paired with insertions.
- Removed songs are remembered for the session and not re-added.

### 7.8 Cycle, timing, and "never stop"
- A **cycle** runs on each song change, on a target change (immediately), and otherwise every `cycle_s` (default 90 s), never more often than every 30 s. Spotify polling stays at 30–60 s as the spec requires, with backoff on 429.
- **Never stop:**
  - the session playlist always keeps at least `min_length` upcoming songs
  - shuffle is turned off for the session
  - the app never calls pause
  - if the user pauses or stops in Spotify, the app stays idle until they press play again
- **Premium fix:** if `/me` no longer returns `product` (Feb 2026 change), the account is linked as "Premium unconfirmed". The first playback command decides: a `403 PREMIUM_REQUIRED` response marks it as not Premium, removes it, and the sidebar shows "You don't have Spotify Premium".
- **New scopes:** `playlist-read-private`, `playlist-modify-private`. Existing users reconnect once (the account window handles this).

### 7.9 Code changes
| File | Change |
|---|---|
| `backend/catalog.py` (new) | Dataset download/import, SQLite `catalog`, in-memory matrix, radius search |
| `backend/selection.py` (new) | Pure functions: tempo/Camelot/transition distances, scoring, MMR, cheapest insertion, removal rule |
| `backend/episodes.py` (new) | Podcast episode → (valence, arousal) from text (NRC VAD) |
| `backend/features.py` | `Features` gains tempo, key, mode, loudness, danceability, instrumentalness, speechiness; cache table migrated |
| `backend/spotify/client.py` | `playback_state()`, `queue()`, session-playlist create/read/insert/remove/reorder via `/items`, `play_context(offset, position_ms)`; the mock simulates playlist playback |
| `backend/spotify/auth.py` | New scopes; "Premium unconfirmed" handling |
| `backend/inference.py` | Mood = queue average (7.3); skip detection kept for telemetry |
| `backend/actuators/audio.py` | Becomes the **queue manager**: hand-over, cycle (pick, insert, remove), reconcile with Spotify, stop when the user takes over |
| `backend/daemon.py` | Cycle scheduling, new state fields for the UI (queue mood, queue size, last added/removed, managing yes/no) |
| `frontend/components.py` | Status card shows queue mood, adds/removals, "managing" state |
| `config/config.json` | New `queue` section (all defaults above) |
| `tests/backend/` | New `test_catalog.py`, `test_selection.py`, `test_queue_manager.py` |

### 7.10 Decisions (confirmed by the user 2026-10-08)
1. **Session playlist: yes.** At the start of a session, a playlist is created from the songs in the user's queue and played instead of the queue. Songs are added to and removed from the playlist. The mood is the average of the songs in the playlist (from the one now playing onward). **When the session is done, the playlist is deleted.**
2. **Dataset:** the 114k-track dataset is the database.
3. **Podcasts: left out** for now. They are not in the average, and never inserted or removed. (The NRC VAD text estimate is not built.)
4. **Mood = plain average** of the window.
5. **Live test: later.** Everything is built and tested against the simulated Spotify first.

### 7.11 Implementation order
1. **Spike** on a real account: create the session playlist, hand over at the same millisecond, insert/remove while playing, confirm the Spotify app follows. Decides A vs B.
2. Catalog import + tests.
3. Extended features + cache migration.
4. `selection.py` (pure, heavily tested).
5. Spotify client playlist/queue methods + mock playlist playback.
6. Queue manager + queue-average inference.
7. Daemon wiring, UI status, Premium fix, new scopes.
8. Podcast estimator.
9. Docs (SPEC, MANUAL, README, MODIF).

### 7.12 Tests (acceptance)

(Implemented in `tests/backend/test_selection.py`, `test_catalog.py`, `test_session.py`; the sidebar in `tests/frontend/test_ui.py`.)
| Requirement | Test |
|---|---|
| Mood = average of queue points | Window with known points → exact mean; podcasts/unknown excluded |
| Picks near the mood, one step toward the target | Every pick lies within `r` of the waypoint and closer to `t` than `m` |
| Musical fit | Tempo (incl. half/double), Camelot and loudness distances; picks beat random candidates on transition cost |
| Variety | MMR never picks two songs by the same artist when alternatives exist |
| Insert in between | Inserted positions ≥ 2, follow the m→t order, minimize the insertion cost |
| Remove far songs | Only songs far from both m and t; protected items never removed; caps respected |
| Converges | Mocked 60-minute session: queue average moves monotonically (within noise) toward `t` and settles within `settle_radius` |
| Never stops | Mock: upcoming never drops below `min_length`; no pause calls; user pause → app idle; user switches context → app stops managing |
| Spotify limits | 429 backoff; `/items` endpoints used; `PREMIUM_REQUIRED` handling |

### 7.13 As built (2026-10-08) and what changed from the plan
**Files:**

| File | What it is |
|---|---|
| `backend/catalog.py` | `python -m backend.catalog import` downloads the dataset and stores **89,741 unique tracks** in `data/catalog.db` (genres of duplicate rows merged). `TrackTable` loads them into NumPy arrays (about 0.4 s). |
| `backend/selection.py` | Pure functions: `queue_mood`, `tempo_distance`, `camelot`/`key_distance`, `transition_cost`, `rank_candidates`, `mmr_select`, `plan_insertions`, `choose_removals`, `plan_cycle`. One cycle over the full catalog takes about 15 ms. |
| `backend/actuators/session.py` | `PlaylistSession`: `start` (hand-over at the same song and millisecond), `cycle`, `end` (hand back, then delete). It records the playlist in `data/session.json` so a crash leftover is deleted next time. |
| `backend/spotify/client.py` | Live calls: `player_state`, `queue`, `create_playlist`, `playlist_items`/`add`/`remove` via `/items`, `play_context`, `set_shuffle`, `delete_playlist` (`DELETE /me/library`, falling back to the pre-2026 unfollow endpoint). It detects `PREMIUM_REQUIRED`. The mock now simulates playlist playback, user pause and the user switching to other music. |
| `backend/features.py` | Features gain tempo, key, mode, loudness and danceability. The cache table is migrated in place. |
| `backend/daemon.py` | `audio.mode = "session"` (new default) or `"queue"` (Phase 1). A cycle timer is added. The mood shown is the queue average. Pause nudging, quit, or a new target ends or starts the session. |
| `frontend/components.py` | The status card shows "N songs ahead · +a −r last update", or why the session is waiting. |

**Changed while building, after testing in simulation:**
1. **Strict path order for insertions.** At first, new songs could land near the front of the queue. They then played first, the original songs never left the window, and the average stalled at a deviation of about 0.34. A new song now always goes *after* every upcoming song that is less far along the mood → target path, then into the cheapest gap from there.
2. **No growth beyond the window.** Once 19 songs are upcoming, a cycle only replaces what it removed. Before this, the playlist kept growing.
3. **Artists are a hard rule** in MMR: no repeated artist while another artist is available.
4. **Your own "Add to queue" songs** are detected by matching the whole overlap of Spotify's queue with the playlist (`manual_prefix_length`). This stays correct right after hand-over, when those songs were also copied into the playlist, and when Spotify appends autoplay songs. Their playlist copies are removed, so nothing plays twice.
5. **Repeats when the catalog runs dry.** Songs played in the last 50 are not picked again. If that leaves no good pick near the waypoint (common with only a small library), only the last 10 are excluded.
6. **Session end hands back the next 10 songs** to Spotify's queue before deleting the playlist, so the music continues. If the user started other music, nothing is handed back.
7. **Premium fallback:** an account whose `/me` reply has no `product` is linked as "Premium not confirmed". A `403 PREMIUM_REQUIRED` on the first playback command marks it Not Premium and removes it.

**Simulation results** (`tests/backend/test_session.py`):
- A user starting on loud, happy songs with target Focused, with a dense catalog (1,600 tracks), goes from a deviation of 0.59 to 0.05 in about 3 hours. It then stays within about 0.02.
- The queue holds 15–19 songs and playback never stops.
- With only about 150 songs it reaches the target in about 1.5 hours, then hovers within about 0.1–0.2 because it runs out of songs. This is why the catalog import is recommended.
- The speed is set by the definition (mood = average of about 20 queued songs): songs have to play, or be removed, before the average moves.

**After the 2nd live run (user requests):**
- Insert positions are **random in the middle** by default (`insert_mode = "random"`); the path order is optional (`"path"`).
- A **new target clears up to 5 songs far from it**.
- Session steps are serialized, and a 20 s grace period plus 2 confirmations are required before concluding "the user took over".

Open problems are tracked in `LOG.md`.

**Still to do:** the live test on a real account (§7.11 step 1): hand-over smoothness, whether Spotify follows edits to the playing playlist, and whether deleting the playlist via `DELETE /me/library` works for the owner.

