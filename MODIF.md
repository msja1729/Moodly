# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Newest first. The plan behind the work in progress is in `PLAN.md` §7; requirements are in `SPEC.md.md`.

---

## 2026-10-10 · Deployable release + development setup
**Why:** to ship an installer other people can use, while development continues from source.

**Changed:**
- **One launcher:** the tray app starts the daemon as a background process once an account is chosen, restarts it after a log-out or a crash, and stops it cleanly on quit (stop file; the daemon also stops if the UI process disappears). `--no-daemon` keeps the old two-terminal workflow. `frontend/daemon_process.py`, `launcher.py`.
- **Single instance:** a second live daemon exits with a message (`data/daemon.lock`).
- **Where files live:** development keeps `config/` and `data/` in the project folder. The installed app uses `%APPDATA%\AmbientMood`, copying the shipped `config.json` and `catalog.db` there on first run (a newer shipped catalog replaces the old one). `AMBIENT_MOOD_HOME` overrides. `shared/config_io.py`.
- **Daemon:** `--parent-pid`, `--stop-file`, `--log-file`.
- **Packaging:** PyInstaller spec, Inno Setup installer, `packaging/build.ps1`. The song catalog is shipped in the installer.
- **Git:** `main` (releases, tagged `vX.Y.Z`) and `dev` (work in progress).

**Tests:** 198 (new: first-run copy, stop watcher, single instance, real daemon start/stop as a subprocess). The packaged exe was smoke-tested: the daemon ran a mock session and stopped cleanly; the UI opened the account window.

## 2026-10-08 · Fourth live run: Spotify's queue didn't show the edits
**Seen live:** the session playlist was updated, but Spotify's upcoming songs didn't change. Spotify plays its own copy (snapshot) of a playlist taken when playback started; API edits don't reach it.

**Fixed:**
- **Resync after edits:** after a cycle that adds or removes songs, the playlist is played again from the same song at the same moment, so Spotify reloads it (`session.resync_after_edits`, default on). While one of the user's own queued songs is playing, the resync waits so the song isn't cut.
- **Stale-snapshot songs:** the app remembers Spotify's copy. A song in that copy but already removed from the playlist makes the app move on to the playlist straight away. Without this, the music would eventually have stopped (found in simulation).
- **User-queued songs:** they are identified by matching Spotify's queue against both its copy and the current playlist. A mismatch once made the app delete the playlist's own songs repeatedly.
- **Simulator:** the mock can behave like real Spotify (`follows_playlist_edits=False`). New tests check that the queue reflects edits after a resync, that the queue stays stale without it, that there's no resync when nothing changed, and that over 2 hours in both Spotify behaviours the user's songs play in full and the music never stops.

**Tests:** 190 (all pass except the known flaky watcher test, LOG #14). Logged in LOG.md as F13–F15, with new open items #16 (blip at resync) and #17 (a removed song may start before the next poll).

## 2026-10-08 · Third live run: autoplay mistaken for the user's queue
**Seen live:** the session started with only the current song (empty queue). Spotify's queue then held 20 autoplay suggestions, which the app classed as the user's own (protected) songs, so it added nothing. The playlist ran out and Spotify autoplay continued.

**Fixed:** `manual_prefix_length` returns 0 when the playlist has no upcoming songs, so the first cycle refills the playlist. Test added. The daemon was stopped mid-session; the leftover playlist is deleted at the next start (`data/session.json`). Logged as LOG.md F12.

## 2026-10-08 · Second live run: fixes, random insert positions, clearing on mood change
**Seen live:**
- Every session ended about a second after starting ("you started other music"), and each mood change repeated this. Two causes:
  - Spotify reports the old source for a moment after the switch.
  - A target change and the timer ran session steps at the same time, creating two playlists that each looked foreign to the other.
- One orphaned "Ambient Mood session" playlist was left in the user's library. It was deleted.

**Changed (user requests):**
- **Random insert positions:** new songs go at random positions in the middle of the upcoming songs (never at "now"/"next", not appended at the end), so they're heard soon. `session.insert_mode = "random"` (default) or `"path"` (the previous mood → target order + cheapest gap). Trade-off: in simulation the mood moves more slowly (0.59 → 0.19 in 4 h vs → 0.04 in 3 h with "path"), because the original songs stay in the queue longer.
- **Changing the mood clears songs that don't fit:** on a new target, up to 5 upcoming songs far from the new target (> 0.35) are removed and replaced (`session.max_removals_on_retarget`). The normal cycles keep the "far from both mood and target" rule.

**Fixed:**
- Session steps are serialized (an asyncio lock in the daemon).
- 20 s grace period after hand-over (`session.takeover_grace_s`). "User took over" now needs another source seen on 2 cycles in a row. "No source" counts only if the song playing is neither in the playlist nor in Spotify's queue.
- The mock can delay reporting the new source (`context_report_lag_s`), like Spotify.

**New:** `LOG.md`, a list of all open problems raised so far (15 open or partly addressed, 11 fixed).

**Tests:** 185 pass. New: both insert modes over a 4-hour session, a stale source after hand-over, overlapping steps → one playlist, clearing on a new target, random positions in the middle, and the takeover rule after the grace period.

## 2026-10-08 · First live run on a real account (Phase 2)
**Worked:**
- The account was linked as Premium and the catalog loaded (89,821 tracks, 107 from the user's library).
- The session started from 21 queued songs.
- **Hand-over confirmed on Spotify:** the same song kept playing, now from the "Ambient Mood session" playlist, with shuffle off.
- The first cycle added 1 song and removed 1.
- On Pause nudging, the next songs were handed back to the queue and the playlist was removed from the library.

**Fixed:**
- **Daemon crash at startup:** `ActiveAccountCacheHandler` didn't subclass spotipy's `CacheHandler`, which spotipy's auth manager requires (a bug from the multi-account change; no test built the live client). Fixed, and a test was added.
- **Playlist deletion:** `DELETE /me/library` returned 403 "Insufficient client scope" (it needs `user-library-modify`). The fallback unfollow call worked. The order is now swapped: unfollow first, then `/me/library`.
- **Tests:** they copied the user's live `config/config.json` (paused, target Energized) and failed. `tests/conftest.py` now resets the target and pause state to the defaults.

**Found (not yet addressed):**
- Only 10 of the user's 21 queued songs had mood data. Many are recent Indian film songs, missing from both the dataset (tracks up to ~2022) and ReccoBeats. Songs without data play and are never removed, but the mood is computed from fewer songs.
- After the session ended, playback was paused on the same song. It's not yet known whether the user paused it or whether deleting the playing playlist stopped it.

## 2026-10-08 · Phase 2 implemented: session playlist, queue-average mood, add/remove cycles (backend)
**Status:** implemented and tested against the simulated Spotify (179 tests pass). **Not yet tried on a real Spotify account** (the user chose to test live later).

**Decisions confirmed by the user:**
- **Session playlist:** at the start of a session, create a playlist from the songs in the user's queue and play it. Add and remove songs in the playlist instead of the queue. The mood is the average of the songs in the playlist. Delete the playlist when the session is done.
- **Podcasts:** left out (not in the average, never added or removed).
- **Live test:** later.

**What it does now:**
- **Start:** the song now playing + Spotify's queue are copied into a private **"Ambient Mood session"** playlist. Playback continues from it at the same song and millisecond, with shuffle off.
- **Mood:** the plain average (valence, arousal) of the song now playing + the next songs, up to 20. Arousal = Spotify `energy`.
- **Each cycle** (song change, target change, every 90 s):
  - a Markov-chain waypoint
  - a search of the catalog + the user's library near it, keeping only songs closer to the target
  - ranking by mood fit plus musical fit (half/double-time-aware tempo, Camelot key, loudness, danceability/acousticness)
  - MMR for variety (no repeated artists)
  - insertion along the mood → target path at the cheapest gap (never at "now" or "next")
  - removal of upcoming songs far from both the mood and the target (≤ 2 per cycle, queue ≥ 8, target 15)
- **Never stops:** refills below 8 upcoming songs. It never pauses. A user pause leaves it idle. If the user starts other music, it ends the session and deletes the playlist.
- **End** (pause nudging, quit, log out): the next 10 songs are handed back to Spotify's queue, then the playlist is deleted. A crash leftover is deleted on the next start.
- **Track catalog:** `python -m backend.catalog import` downloads the public Spotify Tracks Dataset and stores 89,741 tracks in `data/catalog.db`. A full cycle over it takes about 15 ms.
- **Premium:** if Spotify's reply has no `product` field (its 2026 change), the account is linked as "Premium not confirmed". A `PREMIUM_REQUIRED` answer then removes it.
- **New scopes:** `playlist-read-private`, `playlist-modify-private`. Existing accounts must reconnect once.

**Fixed during simulation** (details in `PLAN.md` §7.13):
- strict path order for inserts (the average used to stall at 0.34)
- no playlist growth beyond the 20-song window
- artists as a hard variety rule
- correct detection of the user's own queued songs right after hand-over and with autoplay
- allowing older repeats when a small catalog runs dry

**Simulation results:** deviation goes from 0.59 to ≤ 0.05 in about 3 hours on a dense catalog, then holds within about 0.02. The queue holds 15–19 songs and playback never stops.

**Files:**
- **New:** `backend/catalog.py`, `backend/selection.py`, `backend/actuators/session.py`, `tests/backend/test_catalog.py`, `tests/backend/test_selection.py`, `tests/backend/test_session.py`
- **Changed:**
  - `backend/features.py` (tempo/key/mode/loudness/danceability + cache migration)
  - `backend/spotify/client.py` (playlist/queue/playback calls on the 2026 `/items` endpoints, `PremiumRequired`, mock playlist playback)
  - `backend/spotify/auth.py` ("Premium not confirmed")
  - `backend/daemon.py` (session mode, cycle timer, state for the UI, ending the session on exit)
  - `backend/feedback.py` (skip learning includes playlist inserts)
  - `frontend/components.py` (session status in the sidebar)
  - `frontend/onboarding.py` ("Premium not confirmed yet")
  - `config/config.json` (`audio.mode`, `session` section, `paths.catalog_db`, `paths.session_state`)
  - `.gitignore`, `tests/frontend/test_ui.py`, `tests/backend/test_spotify_auth.py`
  - `README.md`, `docs/MANUAL.md`, `SPEC.md.md`, `PLAN.md`

## 2026-10-08 · Phase 2 planned: queue-average mood and queue reshaping (backend)
**Status:** planned; implemented in the entry above (with the user's decisions: session playlist deleted at the end, podcasts left out, live test later).

**Requested:**
- Map every song (and podcast) to a (valence, arousal) point.
- The user's mood = the average point of the songs in their queue.
- Repeatedly search the whole database for songs near the mood that are also musically similar (tempo etc.), insert them at positions in between, and remove queued songs far from both the mood and the target.
- Music must never stop unless the user stops it, and everything happens in the user's own Spotify account.

**Findings that shaped the plan:**
- Spotify's Web API **cannot remove songs from the queue or insert at a position**. Adding only appends to the end. The plan therefore uses a private **"Ambient Mood session" playlist** that the app maintains, with a seamless hand-over at the same song and millisecond. An append + auto-skip fallback is kept in reserve.
- **Spotify's February 2026 changes** for development-mode apps:
  - playlist editing moved to `/playlists/{id}/items`
  - `/me` may no longer include `product` (premium/free), so the Premium check needs a fallback
  - development mode allows **5 users** (earlier docs said 25; corrected), and the app owner needs Premium
  - refresh tokens expire 6 months after sign-in
- The public **Spotify Tracks Dataset** (Hugging Face, ~114k tracks, 20 MB, no login) has valence, energy, tempo, key, mode, loudness and danceability. It will be the searchable database.

**Algorithms chosen:**
- Markov-chain waypoint (kept from Phase 1)
- radius search over the catalog
- weighted score with tempo (half/double-time aware), Camelot key, loudness and feel distances
- Maximal Marginal Relevance for variety
- cheapest insertion for positions, ordered along the mood → target path
- the "far from both" removal rule, with caps

**Files changed:**
- `PLAN.md`: new §7 (Phase 2 design, decisions, order, tests)
- `SPEC.md.md`: new "Queue-Based Mood Regulation (Phase 2)" section, constraints (2026 Spotify changes, 5-user limit, new scopes), inputs/outputs, Phase 2 acceptance criteria, file list
- `README.md`: links and layout
- `MODIF.md`: created

## 2026-10-08 · SPEC.md and PLAN.md back at the project root; PLAN.md text repaired
- `SPEC.md.md` and `PLAN.md` moved back from `docs/` to the project root, on request. `docs/` keeps `MANUAL.md`.
- `PLAN.md` was garbled by a character-encoding error (dashes, arrows and Greek letters showed as `â€”`, `â†’`, `Î±`). Repaired to proper UTF-8; no wording changed.

## 2026-10-08 · Repository reorganized
- **Undone first:** another Claude session had started a partial reorganization (docs/ and split tests, with an orphaned test file and a missing fixture). Its moves were reverted, and the reorganization was redone in one pass.
- **New layout:**
  - `config/config.json` (was `config.json`)
  - `backend/spotify/client.py`, `backend/spotify/auth.py` (were `backend/spotify_client.py`, `backend/spotify_auth.py`)
  - `data/spotify_accounts/` (was `.spotify_accounts/`)
  - `tests/backend/`, `tests/frontend/`, with a shared `store` fixture in `tests/conftest.py`
  - `docs/MANUAL.md`
- All imports and path references updated, `.gitignore` updated, and a "Project layout" section added to `README.md`.

## 2026-10-08 · Several Spotify accounts, account choice at launch, account name + Log out in the tray
- **Account window on every launch:** it lists the accounts linked on this computer. The user selects one (it is re-checked for Premium) or connects another. With no accounts, it shows the Premium notice and Connect Spotify.
- **Sidebar:** the top-right corner shows the active account (green dot + name) with a **Log out** link. The tray menu shows "Spotify: <name>" and **Log out**, and the tooltip includes the name.
- **Log out:** deletes only the active account's saved token, removes it from the list, and reopens the account window. Closing that window quits the app.
- **Daemon:** it follows the active account on every request, so switching takes effect without a restart. After a log out it backs off instead of opening a sign-in prompt.
- **Storage:** `AccountStore` keeps one token file per account plus `index.json`.
- **Files:** `backend/spotify_auth.py`, `backend/spotify_client.py`, `backend/daemon.py`, `frontend/onboarding.py`, `frontend/components.py`, `frontend/app.py`, tests, `SPEC.md.md`, `README.md`, `MANUAL.md`.
- The user's own saved login was deleted on request (logged out).

## 2026-10-08 · Sign-in without the developer's keys (PKCE)
- Sign-in switched from the client-secret flow to **Authorization Code with PKCE**. No client secret is shipped or needed; each user signs in to their own account.
- The public **Client ID** moved from `.env` into `config.json` (`spotify.client_id`, `spotify.redirect_uri`). `.env` is now optional (development overrides only).
- The daemon refreshes tokens with PKCE too. Verified that Spotify accepts the PKCE sign-in link for the app.
- **Files:** `backend/spotify_auth.py`, `backend/spotify_client.py`, `config.json`, `.env.example`, `README.md`, `MANUAL.md`, tests.

## 2026-10-08 · Spotify account linking with a Premium gate
- **First-run Connect Spotify window:** explains that Premium is required, opens Spotify's sign-in with the account chooser forced (`show_dialog`), then reads the account's plan.
  - **Premium:** linked and saved.
  - **Not Premium:** "You don't have Spotify Premium", nothing saved, option to use a different account.
  - **Errors:** shown with Try again.
- **Daemon:** refuses to start until a Premium account is linked, instead of opening its own browser sign-in.
- **New scope:** `user-read-private` (needed to read the plan).
- **Files:** `backend/spotify_auth.py` (new), `frontend/onboarding.py` (new), `frontend/app.py`, `backend/daemon.py`, `backend/spotify_client.py`, `tests/test_spotify_link.py` (new), `README.md`, `SPEC.md.md`.

---

### Known issues
- `tests/backend/test_inference.py::TestDaemonSession::test_config_change_detected_by_watcher` fails intermittently: a file-watcher timing race on Windows that predates these changes. Not yet fixed.
