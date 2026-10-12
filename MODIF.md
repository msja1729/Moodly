# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M17 · Live sidebar and glass design that follows the mood (at the end, after the summary).

---

## Summary of earlier modifications (M1–M16)

| # | Date | Title | Problem | Modification |
|---|---|---|---|---|
| M1 | 10-08 | **Spotify account linking with a Premium gate** | Spotify only lets Premium accounts control playback. | First-run "Connect Spotify" window; the account's plan is checked; non-Premium accounts are refused and nothing is saved. Scope `user-read-private`. |
| M2 | 10-08 | **Sign-in without the developer's keys (PKCE)** | The client-secret flow tied the app to the developer's keys. | Authorization Code + PKCE; no secret shipped; public Client ID in `config.json`. |
| M3 | 10-08 | **Several accounts, choice at launch, Log out** | One account per computer; no way to switch. | Account window on every launch; active account + Log out in the sidebar and tray; the daemon follows the active account. |
| M4 | 10-08 | **Repository reorganized** | A half-finished reorganization by another session. | Reverted and redone: `config/`, `backend/spotify/`, `data/spotify_accounts/`, `tests/backend|frontend/`, `docs/`. |
| M5 | 10-08 | **SPEC/PLAN back at the root; PLAN text repaired** | PLAN.md garbled by an encoding error. | Moved back; repaired to UTF-8. |
| M6 | 10-08 | **Phase 2 planned** | Spotify's API can't insert into or remove from the queue. | Design: private "Ambient Mood session" playlist; mood = queue average; Markov waypoint, catalog search, MMR, cheapest insertion, removal rule. |
| M7 | 10-08 | **Phase 2 implemented** | (the plan above) | Session playlist with seamless hand-over; add/remove cycles every 90 s; 89,741-track catalog; hand-back and delete at the end. 179 tests. |
| M8 | 10-08 | **First live run fixes** | Daemon crashed at startup (cache handler type); playlist delete returned 403; tests used the live config. | Cache handler fixed; unfollow first; tests reset the config. Found: many Telugu/Hindi songs have no mood data. |
| M9 | 10-08 | **Second live run: takeover bug, random inserts, clearing on mood change** | Sessions ended a second after starting; two overlapping steps made two playlists. | Grace period + 2-cycle takeover rule; serialized steps; random insert positions (default); a new target clears up to 5 unfitting songs. |
| M10 | 10-08 | **Third live run: autoplay taken for the user's queue** | With an empty queue, Spotify's autoplay was protected and nothing was added. | `manual_prefix_length` = 0 when the playlist has no upcoming songs. |
| M11 | 10-08 | **Fourth live run: edits not reaching Spotify's queue** | Spotify plays a snapshot of the playlist. | Resync after edits (same song, same moment); stale-snapshot songs skipped; user-queued songs matched against both copies. |
| M12 | 10-10 | **Deployable release + development setup** | Users can't run two terminals; Program Files is read-only; catalog needed a command. | The tray app runs the daemon; single instance; `%APPDATA%\AmbientMood` for user data; PyInstaller + Inno Setup installer; `main`/`dev` branches; flaky watcher test fixed. |
| M13 | 10-10 | **Uninstall/upgrade closes the app cleanly (0.1.1)** | The uninstaller force-killed the app, leaving the session playlist and a warm screen. | The app quits normally when `%APPDATA%\AmbientMood\data\app.quit` appears (also on the account window); the (un)installer creates it and waits up to 40 s. |
| M14 | 10-11 | **Song recommender: taste + mood + top-2 languages** | Songs came from a 2022 dataset by mood only: not the user's taste or languages, no new releases. | New `recommender/` package. Taste profile (Spotify top tracks + Moodly plays/skips); top-2 languages; songs of favourite + Deezer-related artists via Spotify search (incl. the last two years); language from marker → script → LRCLIB lyrics → romanized words → artist; mood features via ReccoBeats or an iTunes preview analysed by ReccoBeats; selection adds a language filter and `− 0.12·taste − 0.03·freshness`. Live: Telugu 52%, Hindi 33%; pool 36 → 66 → 86 songs. 218 tests. |
| M15 | 10-11 | **Session buttons and a song pool prepared per session** | Before the pool was ready, and when it had nothing near a step, random catalog songs (any language) were added; sessions started on their own. | Sidebar: Start session (at launch), End session, New session. On start, new session and every target change the session "prepares": nothing is added until the pool holds 15 useful songs (max 10 min; 2 songs kept ahead so music never stops). The pool is aimed at the goal (songs leading away are pruned); no catalog songs for mood steps; the pool is cleared from disk at session end. 222 tests. |
| M16 | 10-11 | **Release 0.2.0 for Windows and Mac** | M14–M15 were only on `dev`; no Mac installer. | Mac support (data in `~/Library/Application Support`, VS Code path per platform, sidebar under the menu bar); `build_mac.sh` + GitHub workflow build Apple silicon and Intel `.dmg` files on release; config with a BOM accepted; `dev` merged into `main`, `v0.2.0` released with plain-language notes. 224 tests. |

---

## M17 · Live sidebar and glass design that follows the mood · 2026-10-12 · `🆕 LATEST`

**Problems:**
1. **Lag:** the song, the mood dot and the details updated late (up to a minute) and not together. Causes:
   - the daemon polled Spotify every 30–60 s
   - the mood dot moved only on a session cycle (at most every 30 s)
   - the sidebar re-read `state.json` every 1.5 s
2. **Design:** the sidebar should look like the reference `Glass design UI.jpg`, and its colour should change with the mood.

**Modifications:**
1. **Faster, exact polling** (`backend/inference.py`, config `inference`):
   - every **3 s** while music plays, and 0.4 s after the song is due to end; 15 s when paused or idle (was 30–60 s)
   - backoff on rate limits is unchanged
2. **Mood moves with the song** (`backend/actuators/session.py`): the session keeps the last window's (uri, point). When a song starts, `_advance_mood()` drops the songs before it and re-averages at once, without waiting for the next cycle.
3. **State written when it matters** (`backend/daemon.py`):
   - `state.json` gains `track_name`, `track_artists`, `progress_ms`, `duration_ms`, `is_playing`, `observed_at`
   - `write_state()` writes at once when anything visible changes, else every 10 s (progress alone doesn't trigger a write)
4. **Sidebar reacts instantly** (`frontend/app.py`): a `QFileSystemWatcher` on `data/` and `state.json` refreshes 30 ms after the daemon replaces the file. The 1.5 s timer stays as a fallback. The progress bar moves by itself every 250 ms.
5. **Glass design** (`frontend/components.py` rewritten, `frontend/app.py` `GlassWindow`):
   - **Panel:** 330 × 680, 24 px corners; tinted gradient glass with two dark diagonal bands, a soft highlight and a thin bright edge; Acrylic blur behind on Windows
   - **Content:** window dots (red hides it); the "Ambient Mood" title; fading dividers; NOW PLAYING (song, artist, progress bar, mood map, status line)
   - **MOODS rows:** the selected row is a lighter glass pill with a white edge bar and a chevron; a dot marks the mood closest to the current one
   - **Account row:** SESSION rows, then the account row with avatar initial, name, "Spotify Premium" and round Log out / Quit buttons
6. **Colour follows the mood:**
   - `mood_color(valence, energy)` picks the hue by direction from neutral (amber happy+energetic, rose tense, violet sad, teal calm, blended between) and the richness by distance
   - the window glides to each new tint over 0.9 s (eased)
   - the selected row, avatar and target dot pick up the tint
7. **Docs:** SPEC (frontend design, dynamic colour, sync, polling, acceptance criteria), MANUAL (sidebar picture and controls), README (preview `docs/sidebar.png`, polling settings).

**Tests:** 228 pass. New: poll timing; the mood re-averaged on a song start; state written only on visible change or every 10 s; a new `state.json` shown at once via the watcher (song, artist, progress, mood); four distinct hues and the tint gliding to the mood colour; the closest-mood dot. The design was checked by rendering the sidebar in three moods (`docs/sidebar.png`).

**Not done / limits:**
- Not yet checked live against Spotify: polling every 3 s is ~20 Spotify calls a minute per user.
- On Windows the blur behind the glass needs Windows 10/11 Acrylic. On macOS the glass is painted without the system blur.
