# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M16 · Release 0.2.0 for Windows and Mac (at the end, after the summary).

---

## Summary of earlier modifications (M1–M15)

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

---

## M16 · Release 0.2.0 for Windows and Mac · 2026-10-11 · `🆕 LATEST`

**Problem:** M14–M15 lived only on `dev`; there was no installer for Mac users.

**Modifications:**
1. **Mac support in the code:**
   - **User data:** `~/Library/Application Support/AmbientMood` in the installed Mac app (`shared/config_io.py`).
   - **VS Code theme:** `platform_path()` maps the Windows setting to `~/Library/Application Support/Code/User/settings.json` on macOS and `~/.config/Code/User/settings.json` on Linux. The Windows Terminal theme is skipped where it doesn't exist (`backend/actuators/workspace.py`).
   - **Sidebar:** opens under the menu bar (top-right) on macOS (`frontend/app.py`).
2. **Mac packaging:**
   - `packaging/AmbientMood.spec` builds `AmbientMood.app` on macOS. It is a menu-bar app (`LSUIElement`), with bundle id `io.github.msja1729.moodly`.
   - `packaging/build_mac.sh <version>` runs tests (Qt offscreen) → PyInstaller → a smoke test (the packaged daemon runs a mock session and stops cleanly) → `AmbientMood-<version>-mac-<arch>.dmg` with an Applications link.
   - `.github/workflows/release-mac.yml` runs it on GitHub's Mac machines (Apple silicon `macos-latest`, Intel `macos-15-intel`) when a release is published, or by hand. It attaches the `.dmg` files to the release.
3. **Config robustness:** `config.json` saved with a byte-order mark (some Windows editors add one) is now accepted; before, the app exited at start.
4. **Release:** `dev` merged into `main`; tag `v0.2.0`; GitHub release with the Windows installer and plain-language notes; the Mac `.dmg` files are added by the workflow.

**Tests:** 224 pass (new: platform paths, BOM config). The Windows 0.2.0 installer was tested end to end:
- install
- packaged daemon session (mock)
- the UI starts with no session and shows the account window
- uninstall closes it cleanly

**Not done / limits:**
- The Mac app has not been tried on a real Mac; only the GitHub build and its smoke test run.
- It is not signed with an Apple Developer ID, so macOS asks the user to allow it once.
- No screen warmth on Mac yet.
