# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M15 · Session buttons and a song pool prepared per session (at the end, after the summary).

---

## Summary of earlier modifications (M1–M14)

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

---

## M15 · Session buttons and a song pool prepared per session · 2026-10-11 · `🆕 LATEST`

**Problem:**
1. Before the pool was ready (~1.5 min after start), songs came from the static catalog: any language, not the user's taste.
2. When the pool had nothing near a mood step, a catalog song was used for that step.
3. Sessions started on their own; there was no explicit start/end, and the pool stayed on disk between sessions.

**Modifications:**
1. **Session buttons** (`frontend/components.py`, `frontend/app.py`, `shared/config_io.py`):
   - At launch no session runs (`paused` set to true), and the sidebar shows **Start session**.
   - During a session the sidebar shows **End session** and **New session**; these replace "Pause nudging".
   - New session = `start_new_session()`: `paused = false`, `session_seq + 1`.
   - Status line: "No session - click Start session", "Preparing songs for you in Telugu, Hindi (7/15 ready) - nothing is added until then".
2. **Preparing phase** (`backend/daemon.py`, `backend/actuators/session.py`):
   - On Start session, New session and every target change, the session sets `preparing`.
   - While preparing, cycles only measure the mood: no adds, no removals.
   - Pool refreshes run back to back until `ready_songs` (15) useful songs exist, or `prepare_max_s` (600 s) has passed. Then a "ready" cycle starts adding, with the usual clearing of songs far from the target.
   - Exception: songs are added if fewer than `prepare_min_upcoming` (2) remain ahead, so the music never stops.
3. **Pool aimed at the goal** (`recommender/engine.py`):
   - `set_goal(target, mood)`.
   - Only **useful** songs are kept: closer to the target than the mood, or within 0.15 of the target once there.
   - On a target change, `prune()` removes songs that would lead away from it.
   - Artists whose known songs are near the target are searched first (`_artist_mood_fit`).
4. **No catalog songs for mood steps** (`backend/actuators/session.py`):
   - If the pool has no song near the step (no pick, or picks averaging more than `pool_max_jump` 0.3 from the waypoint), the cycle adds nothing and sets `needs_more_songs`. The daemon then starts a refresh (at most every 2 min).
   - The static catalog is used only by the emergency fill, to keep the music going.
5. **Pool cleared at session end** (`backend/daemon.py`, `recommender/store.py`):
   - End session, New session, quit and log-out stop the running refresh and run `Recommender.clear()`. That empties `data/recommender.db` (songs, artists, profile) with VACUUM and resets the in-memory pool.
   - Refreshes run only while a session runs.
   - The mood-feature cache (`data/mood.db`) is kept. It is a general cache of song measurements, which also makes rebuilding the next pool faster.
6. **Config:**
   - Added: `recommender.ready_songs` (15), `recommender.prepare_max_s` (600), `session.prepare_min_upcoming` (2), `session_seq`.
   - Removed: `recommender.min_pool`.
   - Changed meaning: `session.pool_max_jump` is now the threshold for "add nothing, grow the pool".
7. **Docs:**
   - SPEC: session buttons, session end clears the pool, the pool is goal-specific, preparing, no catalog steps, acceptance criteria.
   - Also updated: README, MANUAL (sidebar controls, session lifecycle).

**Tests:** 222 pass. New: session buttons write the config and swap visibility; status messages; preparing adds nothing but keeps 2 songs ahead; "nothing near the step" waits and sets `needs_more_songs` (no catalog); the full lifecycle (no refresh before Start → preparing → ready and adding from the pool → target change prepares again → New session ends, clears and restarts → End session clears and stops refreshing); the preparing time limit; goal filtering, `prune()` and `clear()`.

**Not done / limits:**
- Not yet run in a live session.
- Each session rebuilds its pool from scratch, so the first minutes of a session are spent preparing (the cached mood features speed this up).
- If the user's taste has few songs of the target mood, waiting can last up to 10 minutes. Afterwards the session adds from what the pool has, or nothing.
