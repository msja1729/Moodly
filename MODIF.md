# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M28 · No more lag; a popup asks where songs come from at Start / New session (at the end, after the summary).

---

## Summary of earlier modifications (M1–M27)

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
| M27 | 10-12 | **Songs no longer cut a second after they start; each session starts fresh** | After adding songs, the reload "at position N" started the next song (Spotify counts an entry its API doesn't list); a new session remembered the last one's played/added songs. | Reload at the song itself (`offset.uri`), looked up again after the edits; per-session reset. 250 tests. |

---

## M28 · No more lag; a popup asks where songs come from at Start / New session · 2026-10-12 · `🆕 LATEST`

**Problems (live test 15:26–15:33, Liked Songs):**
1. **The sidebar and the dashboard lagged behind Spotify.** Measured, the sidebar itself is fast (a refresh takes 0.7 ms, a full repaint 3.3 ms). The delay was in the backend:
   - On a new song, it ran the whole session step (read the playlist and queue, choose songs, add them, reload Spotify; 2 s or more, longer when Spotify was slow, e.g. a 5 s timeout at 15:30:12) before writing the new song to `state.json`.
   - It didn't poll Spotify meanwhile.
2. **"It doesn't add songs from Liked Songs."** It did: every song added from 15:28 on is in Liked Songs (checked against the account). 8 of the 11 songs in the screenshot are Liked Songs. The other two (*Ishq Jalakar*, *Samjhawan*) were already in the user's Spotify queue at Start session and were moved into the session playlist as the user's own songs. So no fix was needed for this.
3. **Pause/Play sometimes failed** with Spotify's 404 "no active device", right after a playlist reload.
4. **Wanted:** at Start session or New session, a popup asking where songs should come from (For you or a playlist), in the same design with the background blurred. Picking an option starts the session at once.

**Modifications:**
1. **Backend never holds up the sidebar** (`backend/daemon.py`):
   - A new song is written to `state.json` at once.
   - Live, session steps (song change, timer, ready) run as background tasks (`background_steps`, `_spawn`), so Spotify keeps being polled. The session lock still keeps steps one at a time.
   - Each step writes the state when it finishes. Tests run steps inline.
2. **Pause/Play:** one retry after 0.7 s on a 404.
3. **Sidebar refresh:** only when `state.json` really changed (its time and size). The data folder's logs and databases no longer trigger refreshes.
4. **Dashboard:** the trace is parsed once per change and served with an `ETag`. Unchanged polls get a 304 and the page keeps what it has.
5. **Start / New session popup** (`SourcePicker` in `frontend/components.py`, wired in `frontend/app.py`):
   - "Where should songs come from?": For you · new picks, Liked Songs (count), then your playlists (count; empty ones dimmed). The current choice is marked.
   - A glass card in the mood colour over a blurred snapshot of the sidebar, with a dark veil that fades in (180 ms).
   - Picking saves the source and closes the popup immediately, then starts (`set_paused(False)`) or restarts (`start_new_session`) the session.
   - Esc, Cancel, a click outside, or closing the sidebar cancels and starts nothing.
   - The "Songs:" row stays, for changing the source during a session.
   - `SidebarPanel.source_options()` and `is_current()` are shared by the row's menu and the popup.
   - Disabled rows are drawn at 40% opacity.
6. **Docs:** SPEC (popup, sync), MANUAL (Start session).

**Checked on screen:** the popup over the blurred sidebar, with Liked Songs marked and the empty playlist dimmed.

**Tests:** 252 pass.
- New:
  - a slow session step doesn't stop polling, and the playing song is shown
  - the popup can be cancelled (Esc, closing the sidebar)
- Updated:
  - the session buttons go through the popup: Liked Songs → start; New session → For you
