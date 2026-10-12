# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M27 · Songs no longer cut a second after they start; each session starts fresh (at the end, after the summary).

---

## Summary of earlier modifications (M1–M26)

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
| M22 | 10-12 | **Player controls, album cover, sync under 1 s, simpler mood path view** | The mood path page was hard to read; the sidebar lacked a cover and player buttons; sync took up to 3 s. | Cover art, Play/Pause and Next (sidebar → `data/player.cmd` → daemon → Spotify), a time readout, a small mood map at the side; polling 0.75 s / 2 s; the mood path page rewritten around "right now", "up next" and what Moodly did. 237 tests. |
| M23 | 10-12 | **Release 0.3.0 for Windows and Mac** | The new frontend and backend were only on `dev`. | `dev` merged into `main`; `v0.3.0` published with the Windows installer and both Mac `.dmg` files and plain-language notes (including "turn off Repeat"). |
| M24 | 10-12 | **A link to the mood path dashboard (dev only)** | Opening the dashboard took a separate command. | `tools/dev_dashboard.ps1`/`.sh` run the app with the trace; the app serves the dashboard and shows a link in the sidebar and tray menu. Never in the installed app; `dev` only. 238 tests. |
| M25 | 10-12 | **Songs from one of your playlists or Liked Songs** | Users wanted songs from a playlist of theirs, not only the general search. | A "Songs:" picker (For you / Liked Songs / own playlists); a playlist pool has no language filter, no top-up, repeats allowed, ready once read; "no songs closer" note. 242 tests. |
| M26 | 10-12 | **Good songs no longer removed; the mood is what you listen to** | Mass removals (relinked ids made the playlist look like the user's own queue, so every song was a "duplicate"); far-from-target-only removals after a new target; the mood counted Moodly's unplayed picks. | Songs matched by title + artists; misread-queue guard; one removal rule (far from both); mood = the song playing + the user's own songs; the planner steps from where the queue is heading. 248 tests. |

---

## M27 · Songs no longer cut a second after they start; each session starts fresh · 2026-10-12 · `🆕 LATEST`

**Problems (live test 15:11–15:20, Liked Songs → Energized):**
1. **Songs were cut 1–2 s after they started.** Three times a song started, the cycle about 3 s later added songs and reloaded the playlist, and a second later the next song played:
   - 15:17:06 *Chammak Challo* → 15:17:11 *Aarzu*
   - 15:17:57 *Chinni Gundelo* → 15:18:01 *Saiyaara*
   - 15:19:08 *Kalank* → 15:19:12 *Salam Anali*

   Cause: the reload (`play_context`) started the playlist "at position N" (the song playing in the app's listing), and Spotify started the song after it. So Spotify's count of the entries was one ahead of what its API lists. The constant "queue doesn't line up" warnings fit the same mismatch.
2. **The U-turn on the path** (0.59 → 0.53 at 15:17:11): *Chammak Challo* (0.94, 0.88) was cut after 2 s. The song after it, *Aarzu*, has no mood data, so the mood fell back to the user's own songs ahead (lower).
3. **A new session remembered the last one.** The played, added and removed songs carried over, so at 15:15:48 *Lutt Le Gaya* and *Dhurandhar* were removed as "already played": they had played in the 14:49 session.
4. **A mismatch from M26:** `start()` still recorded Spotify's copy of the playlist by id, while the rest compares title + artists.

**Modifications:**
1. **Reload at the song, not the position:**
   - `play_context(..., track_uri=)` uses Spotify's "start at this track" offset (`offset.uri`).
   - `_play_at()` names the song whenever it is in the playlist once (by position only if it appears twice).
   - This applies to the reload after edits, to moving on from a removed song, and to the hand-over at start.
2. **The song is looked up again after the edits**, before the reload. With a hidden entry, a song inserted "right after the current one" can land just before it.
3. **Each session starts fresh:** `start()` resets the played, added and removed songs, the pending skip feedback, the last window and `source_dry`.
4. `start()` records Spotify's copy by title + artists, and de-duplicates the hand-over songs the same way.
5. **Test mock:** `unlisted` entries that Spotify plays and counts but its API doesn't list, to reproduce the cut.

**Tests:** 250 pass. New:
- with a hidden entry, adding songs never cuts the song playing (this test fails with the old position-based reload)
- a new session forgets the last one
