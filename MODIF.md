# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M22 · Player controls, album cover, sync under 1 s, a simpler mood path view (at the end, after the summary).

---

## Summary of earlier modifications (M1–M21)

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
| M17 | 10-12 | **Live sidebar and glass design that follows the mood** | Song, mood dot and details lagged up to a minute and weren't in sync; the sidebar didn't match the reference glass design. | Spotify polled every 3 s (and right after a song ends); mood re-averaged when a song starts; `state.json` written at once on visible changes and watched by the UI; live progress bar. Sidebar rebuilt after `Glass design UI.jpg` (rows with glass pill + edge bar + chevron, account row); tint follows the mood (amber/rose/violet/teal) and glides. 228 tests. |
| M18 | 10-12 | **Real glass: transparent, blurred background, round corners** | Square edges and thick, opaque glass: the Windows Acrylic call painted black behind Qt's transparent window, in a full rectangle. | Acrylic call removed; a snapshot of the screen behind the sidebar is taken as it opens, blurred and drawn under thinner glass (tint alpha 125/80/70). Same on Windows and macOS; checked on screen. 229 tests. |
| M19 | 10-12 | **Pointer effects** | The sidebar should react to the mouse like modern websites. | A light in the mood colour followed the pointer and the edge shone near it; rows: hover pill fade, inner light, icon nudge, click ripple; the selection slides between moods; mood map crosshair readout and breathing dot. 232 tests. |
| M20 | 10-12 | **Waves instead of the pointer light, bolder text, no crosshair or window dots** | The pointer light was too bright and hid the text; the text should be bolder; the map crosshair and window dots weren't wanted. | Soft waves behind the text when the pointer moves (one per 38 px, fade over 1.6 s); `ui_font()` with heavier weights; crosshair, readout and window dots removed. 232 tests. |
| M21 | 10-12 | **Mood path trace and live plot (testing)** | Wanted to see the mood move toward the target while listening. | Opt-in trace (`AMBIENT_MOOD_TRACE`) of every cycle/song change; a local web viewer (`tools/mood_trace_view.py`) plots it live. 233 tests. |

---

## M22 · Player controls, album cover, sync under 1 s, a simpler mood path view · 2026-10-12 · `🆕 LATEST`

**Request:**
1. The mood path page was too detailed: it was hard to see which songs were picked and where the music is now.
2. In the sidebar, show the song's cover and Play/Pause and Next buttons, with the mood map small and to the side. Keep the design and colours.
3. Get the sidebar in sync in under 1 s (it was up to 3 s).

**Modifications:**
1. **Sidebar Now Playing** (`frontend/components.py`):
   - `CoverArt`: the album cover, 64 px with rounded corners. It cross-fades (320 ms) on a song change; a tinted glass placeholder with ♪ shows when there's no cover.
   - `CoverLoader`: downloads covers in a background thread and keeps the last 24.
   - `MediaButton`: drawn icons (not font glyphs). Play/Pause is a solid white circle and Next is a glass circle; both shrink a little when pressed and are disabled when nothing plays.
   - A time readout next to the progress bar (1:23 / 4:49).
   - The mood map is now 96×72 px beside the status line, with no axis labels (they're in its tooltip).
2. **Player controls** (sidebar → daemon → Spotify):
   - A click writes `data/player.cmd` (`{"cmd": "play" | "pause" | "next"}`).
   - The daemon watches that folder, runs the command (`pause()` / `resume()` / `next_track()` on the client), then polls Spotify twice (after 0.25 s and 0.6 s) and writes the state at once.
   - The sidebar flips the button and freezes or restarts the bar immediately. An older state that arrives before Spotify confirms doesn't undo the click (for up to 3 s).
   - A stale command left over from before the daemon started is deleted.
3. **Cover URL** (`backend/spotify/client.py`): `Track.image` is the album image closest to 300 px. It isn't part of the track's identity. The state now includes `track_image`.
4. **Sync:** the daemon polls every 0.75 s while music plays (was 3 s) and every 2 s when paused (was 15 s), so a change shows in the sidebar within about a second.
5. **Mood path page** (`tools/mood_trace_view.py`), rewritten for reading at a glance:
   - **Right now:** a plain sentence ("Almost there. Your music is 0.08 away from Energized."), the song playing, the mood, the target, how the mood moved since the target was set, and a "% of the way" bar.
   - **Up next:** the next 8 songs in order, each tagged *picked by Moodly* or *your queue*, with its point and whether it pulls toward the target.
   - **Map:** "You are here", the target, one path line, the song playing now (named), and the next 6 songs as numbered dots (green = Moodly picked it).
   - **What Moodly did:** a plain-language feed ("Aimed one step happier and more energetic and added: …"); removals fold away.
   - **Show details:** brings back waypoints, every pick, the grid, and the valence/energy chart.
   - Fixes: progress is measured from when the current target was set (was −771%), and picks are matched ignoring capitals. The page refreshes every second.
6. **Docs:** SPEC (Now Playing, player, sync, performance), MANUAL (sketch, table, poll settings), `docs/sidebar-player.png`.

**Tests:** 237 pass. New:
- the buttons write the command; the button changes at once; a stale state doesn't undo it; Next
- the cover shows the downloaded image and falls back to the placeholder
- the daemon handles pause, play and next (the mock records a skip) and deletes the command
- the cover URL picks the 300 px image
- poll delays 0.75 s / 2 s

**Not done / limits:**
- About 80 Spotify requests a minute while playing. Spotify slows the app down if it asks too often (429); the app already waits as told when that happens.
- Covers need internet; offline, the placeholder shows.
