# SPEC.md: Ambient Mood Regulator

## Goal
A background daemon and lightweight desktop UI that monitors a user's emotional state via their active music listening habits and subtly nudges their environment (audio queue, IDE/terminal themes, and screen temperature) toward a user-selected target mood without interrupting active work.

## Architecture
*   **Frontend (The Sidebar UI):** A lightweight desktop application using **PyQt6**. The UI must be frameless and implement a translucent, frosted glass effect (macOS vibrancy/Windows 11 Acrylic) mimicking a modern OS control center. It runs in the system tray and provides toggle buttons for target moods.
*   **Backend (The Daemon):** A headless Python process managing the Spotify polling, Markov chain math, and environment actuators.
*   **Inter-Process Communication:** The frontend UI writes the user's selected mood to `config.json`. The backend daemon uses a file watcher to instantly detect updates to this file and adjust its target coordinates on the fly.
*   **Audio Data Provider:** A `FeatureProvider` fallback chain (Spotify API -> ReccoBeats API -> Local CSV; the recommender adds iTunes preview -> ReccoBeats audio analysis as the last step) to bypass Spotify's deprecated `/audio-features` endpoint for new developer apps.
*   **Song Recommender (Phase 3, `recommender/`):** A self-contained package that keeps a live pool of songs matching the user's **taste** and **top-2 languages**, with **mood features**, built from live API lookups (no downloaded database). The daemon refreshes it in a background thread; the session picks from it by mood. See "Song Recommendation (Phase 3)".
*   **Spotify Client:** The system integrates directly with the live Spotify Web API via `spotipy` to read active playback and inject tracks into the real user queue.
*   **Spotify Account Window (every launch):** Each time the app starts, before the tray icon appears, the frontend shows a **Spotify account** window. Each user uses **their own** Spotify account, and bridging tracks go into that account's queue. Several accounts can be linked on one computer; exactly one is active at a time.
    1. **Choose an account:** if any accounts are linked on this computer, the window lists them ("Choose a Spotify account"), one row per account with its name, "Spotify Premium", and a "last used" tag on the most recent one. Clicking a row re-checks that account with Spotify (token refresh + Premium) and makes it active. Buttons: **Connect another account** / **Quit**.
    2. **Premium notice (no accounts yet):** the window explains that the app adds songs to the user's Spotify queue and that Spotify only allows this for **Premium** accounts. Buttons: **Connect Spotify** / **Quit**.
    3. **Connecting an account:** opens Spotify's sign-in page in the browser with the account chooser forced (`show_dialog=true`, so *Not you?* lets the user switch accounts even if already signed in). The user picks an account and approves access.
    4. **Premium check:** the app reads the account's `product` from `/v1/me` (`user-read-private` scope).
        *   **Premium:** the account is linked, its token saved locally, and it becomes active. The window confirms "Linked to <name> (Spotify Premium)" with a **Continue** button.
        *   **Not Premium:** the window shows **"You don't have Spotify Premium"** with the account name and plan. Nothing is saved; a previously linked account that lost Premium is removed. Buttons: **Back** / **Use a different account**.
        *   **Sign-in expired or other error** (revoked access, denied, timed out, port busy, mismatched state): an error page with **Back** / **Try again**. An account whose saved sign-in no longer works is removed from the list.
    5. Closing the window without choosing a Premium account quits the app. If Spotify can't be reached when choosing a saved account, the saved account is used as-is so the app still starts offline.
*   **Account Display and Log Out (tray sidebar):** The sidebar's **top-right corner** shows the active Spotify account (green dot + name; full name in the tooltip) with a **Log out** link beneath it. The tray icon's tooltip includes the account name, and the tray right-click menu shows "Spotify: <name>" and a **Log out** item. **Log out** forgets the active account on this computer (deletes its saved token and removes it from the list), hides the sidebar, and shows the account window again so the user can pick another linked account or connect a new one; closing that window quits the app.
*   **Account switching in the daemon:** the daemon reads the token of whichever account is active on every Spotify request, so choosing a different account takes effect without restarting it, and after a log out its requests stop (it backs off) until an account is chosen again.
*   **Authentication (PKCE, no client secret):** Sign-in uses Spotify's Authorization Code flow with **PKCE**, so the app ships no client secret and is independent of the developer's own account. The only app setting is the public **Client ID** of the registered Spotify app, stored in `config/config.json` (`spotify.client_id`, `spotify.redirect_uri`). `SPOTIPY_CLIENT_ID` / `SPOTIPY_REDIRECT_URI` in `.env` may override them for development. The OAuth redirect is received by a local HTTP listener on `127.0.0.1` and can be cancelled from the window.

## Queue-Based Mood Regulation (Phase 2; design and as-built notes in `PLAN.md` §7)
*   **Mood space:** Everything the user listens to on Spotify is mapped to a 2D point **(valence, arousal)** in [0, 1]². For songs, arousal is Spotify's `energy` feature. Podcast episodes have no audio features and are **left out** of the mood for now; the app never inserts or removes them.
*   **Target:** The user provides target coordinates (a mood tile in the sidebar).
*   **Current mood = playlist average:** At any moment, the user's mood is the **average of the points of the songs in the session playlist** from the song now playing onward (plus any songs in Spotify's own queue), up to 20 songs.
*   **Where songs come from:** first the recommender's **taste pool** (Phase 3: the user's taste and top-2 languages, live). Fallback: a local SQLite catalog imported from the public Spotify Tracks Dataset (~114k tracks with valence, energy, tempo, key, mode, loudness, danceability, acousticness, genre), plus the user's top and saved tracks.
*   **Repeated queue reshaping cycle:** on every song change, on every target change, and otherwise every ~90 s, the backend:
    1. Picks a **waypoint** one Markov-chain step from the current mood toward the target.
    2. **Searches the taste pool (or, failing that, the catalog)** for songs close to the waypoint, i.e. close to the current mood and slightly toward the target, that are also **musically similar** to their neighbours in the queue (tempo, including half/double time; key on the Camelot wheel; loudness; danceability; acousticness). Picks are ranked by a weighted score and diversified with **Maximal Marginal Relevance** (no near-duplicates, no repeated artist).
    3. **Inserts** them **in between** the upcoming songs, never at the now-playing or next position. By default the positions are **random within the middle of the queue**, so the user hears the new songs soon (`session.insert_mode = "random"`). Optionally (`"path"`), they are ordered along the path from the current mood to the target, at the gap with the **cheapest insertion cost** (smallest tempo/key/energy/loudness jump).
    4. **Removes** upcoming songs that are **far from both the current mood and the target**, limited per cycle, so the queue never runs short. **When the user picks a new target**, up to 5 upcoming songs far from the new target are removed right away and replaced.
*   **Session playlist:** Spotify's Web API cannot remove songs from, or insert at a position in, the playback queue. So **at the start of a session, a private playlist "Ambient Mood session" is created from the song now playing and the songs in the user's queue, and played instead**, continuing the same song at the same millisecond (no audible gap). All additions and removals happen in that playlist. Songs the user adds through Spotify's own "Add to queue" are counted and play once, but are never removed.
*   **Session end:** when the session is done (pause nudging, quitting the app or daemon, logging out), the next 10 songs are handed back to Spotify's queue so the music continues, and **the playlist is deleted**. If the user starts other music themselves, the session ends and the playlist is deleted without handing anything back. A playlist left behind by a crash is deleted on the next start.
*   **Keeping Spotify in sync:** Spotify plays its own copy of a playlist taken when playback started, so after each cycle that changes the playlist, the app plays it again from the same song at the same moment so that Spotify's queue shows the edits. A song from Spotify's outdated copy that was removed meanwhile is skipped in favour of the current playlist. The user's own queued songs are never cut.
*   **Music never stops:** the session always keeps enough upcoming songs; the app never pauses playback. If the user pauses in Spotify, the app stays idle; if the user starts something else, the app stops managing and shows this in the sidebar.

## Song Recommendation (Phase 3)
*   **Goal:** every song the app adds matches the user's **mood step** (the Phase 2 waypoint, closer to the target), their **taste**, and one of their **top-2 languages** from recent listening. New releases are included.
*   **No downloaded database:** songs come from live API lookups. Everything looked up is cached in `data/recommender.db` (songs, languages, artists) and in the feature cache, so nothing is looked up twice and the pool survives restarts. The static catalog stays only as a fallback.
*   **Taste profile** (every 6 h): Spotify top tracks of the last ~4 weeks (weight 1.0→0.5 by rank) and ~6 months (0.5→0.25), plus songs played in Moodly in the last 14 days (+0.6 finished, −0.4 skipped). It gives the favourite artists (scaled 0..1) and the language shares.
*   **Top-2 languages:** the language with the largest share, plus the second if its share is ≥ 15% (`recommender.min_language_share`). `recommender.languages` can fix the list instead of `"auto"`.
*   **Language of a song.** Spotify provides none (and, since February 2026, no artist genres). It is decided in this order of trust:
    1. a language marker in the title or album (`[TELUGU]`, `- Hindi`)
    2. native script in the title, album or artist
    3. the script of the lyrics (LRCLIB)
    4. common words of Latin-letter lyrics, including romanized Hindi/Telugu/Tamil/Punjabi
    5. the main artist's usual language (at least 2 known songs, at least 70% in one language)

    Otherwise the language is unknown and the song is not used.
*   **Candidate songs:** for favourite artists and their **Deezer related artists**, Spotify search `artist:"X"` (2 pages) and `artist:"X" year:<last year>-<this year>` (new releases). Only songs credited to that artist are kept. Each refresh searches a bounded number of artists (`artists_per_refresh`, 8), and an artist is searched again only after 3 days.
*   **Taste score (0..1):**
    *   favourite artist: its weight
    *   related artist: 0.7 × the favourite's weight, lower further down the related list
    *   the user's own top/recent songs: at least 0.8
    *   then × (0.8 + 0.2 × language share / top share); artists the user skips in Moodly count for less
    *   songs below 0.05 are left out
*   **Freshness (0..1):** 1 for a song released today, falling to 0 at one year (`fresh_days`).
*   **Mood features:** feature cache → ReccoBeats → local CSV → **iTunes 30-s preview analysed by ReccoBeats** (at most `preview_budget` songs per refresh, best taste first). Songs Moodly finds in the queue without features are queued for analysis too.
*   **Pool:** songs with features, in the top-2 languages, with taste ≥ 0.05, one copy per song (title + main artist). It is used once it has at least `min_pool` (30) songs.
*   **Selection with the pool:** the Phase 2 rules apply unchanged (waypoint, closer to the target, musical fit, MMR), plus the language filter and the score term `− 0.12 × taste − 0.03 × freshness` (`session.w_taste`, `session.w_fresh`). If nothing in the pool is near the step, the fallbacks are tried in this order:
    1. a pool plan whose picks average ≤ 0.3 from the waypoint (`session.pool_max_jump`)
    2. the static catalog (any language)
    3. the emergency fill (pool first)
*   **Refresh:** at daemon start and every 30 min (`refresh_s`), in a background thread. Each refresh does bounded work (lyrics ≤ 40, previews ≤ 20) and stops early when the daemon stops. Live mode only (not `--mock`).

## Constraints
*   **Language & Runtime:** Python 3.10+ running locally. The backend must run as a non-blocking daemon.
*   **Security:** No client secret is shipped or required (PKCE). The public Spotify Client ID lives in `config/config.json`. Each user's tokens are stored only on their own machine in `data/spotify_accounts/` (one token file per linked account plus `index.json`; git-ignored) and are written **only for Premium accounts**. Log out deletes the active account's token. Any optional development overrides go in `.env`, separate from `config.json`.
*   **APIs & Integrations:**
    *   Live Spotify Web API (Premium required) for track polling and queue modification. Required scopes: `user-read-currently-playing`, `user-read-playback-state`, `user-modify-playback-state`, `user-library-read`, `user-top-read`, `user-read-private`.
    *   While the Spotify app is in Development mode, only users added under **User Management** in the Spotify developer dashboard (max 5 since Spotify's 2026 changes; the app owner needs Premium) can sign in. Wider distribution requires Spotify's extended quota (organizations only). Refresh tokens expire 6 months after sign-in, after which the user reconnects.
    *   Spotify's February 2026 changes: playlist items are edited through `/playlists/{id}/items` (the `/tracks` paths are removed) and playlists are created with `POST /me/playlists`. The user object may no longer include `product`; if it is missing, Premium is confirmed by the first playback command (`403 PREMIUM_REQUIRED` = not Premium).
    *   Phase 2 adds the scopes `playlist-read-private` and `playlist-modify-private` (existing users reconnect once).
    *   Phase 3 adds no Spotify scope: it uses top tracks (`user-top-read`), search and single-track lookups. Since February 2026 Spotify returns no artist genres and no popularity, has no new-releases or artist-top-tracks endpoints, and gives at most 10 search results per page.
    *   Phase 3 uses free services that need no API key: **Deezer** (related artists), **LRCLIB** (lyrics), **Apple iTunes Search** (30-s previews; ~20 calls/min), **ReccoBeats** (audio features; audio analysis of a preview). Each call has a timeout and per-host spacing; failures are skipped.
    *   OS-level APIs (Win32 `SetDeviceGammaRamp` via `ctypes` for Windows, `xrandr` for Linux) for screen temperature.
*   **Performance:** Spotify polling must happen asynchronously every 30-60 seconds to avoid API rate limits. Environment hot-swaps must execute in < 500ms to prevent UI freezing.
*   **Architecture:** Mood transitions for the audio actuator must be mathematically modeled as a Markov chain through a 2D latent space (valence vs. energy/arousal). Simple genre swapping is forbidden. In Phase 2 the chain chooses the waypoint toward which songs are picked each cycle.

## Inputs and Outputs
*   **Inputs (Frontend):** User clicks on the mood toggle buttons in the frosted-glass sidebar UI. On every launch, the user's choice of linked Spotify account (or a new sign-in) in the Spotify account window. Clicks on **Log out** in the sidebar or tray menu.
*   **Outputs (Frontend):** Updates the `target_mood` string in `config.json`. Writes linked Premium accounts' tokens and the active account to `data/spotify_accounts/`; removes the active account's token on log out. Displays the active account in the sidebar's top-right corner.
*   **Inputs (Backend):** 
    *   Real-time Spotify track IDs and their audio features (valence, energy, acousticness as float 0.0 - 1.0) fetched via the FeatureProvider chain.
    *   (Phase 2) The current song plus the upcoming queue, with tempo, key, mode, loudness and danceability per song; podcast episode titles/descriptions; the local track catalog.
    *   (Phase 3) The user's Spotify top tracks, Moodly's play/skip log, Spotify search results, Deezer related artists, LRCLIB lyrics, iTunes previews.
    *   User interaction events (track skips vs. track completions estimated by duration heuristics).
    *   File watcher triggers from `config.json` updates.
*   **Outputs (Backend):**
    *   Injected track URIs sent to the live Spotify Queue API.
    *   (Phase 2) Insertions at chosen positions and removals in the user's "Ambient Mood session" playlist; the queue-average mood, queue size and last additions/removals in `data/state.json` for the sidebar.
    *   (Phase 3) `data/recommender.db` (cache); `recommender` in `data/state.json`: status, languages, pool size.
    *   JSONC surgical string modifications to VS Code's `settings.json` and Windows/WSL Terminal `profiles.json` (preserving comments).
    *   Display API color temperature adjustments (mapped to Kelvin values).
    *   Local SQLite log entries mapping interventions to skip rates.

## Acceptance Criteria
*   **Onboarding:** On every launch the Spotify account window appears before the tray icon. With no linked accounts it opens on the "Spotify Premium required" page; otherwise it lists the linked accounts and the user selects one of them (or connects another).
*   **Onboarding:** The sign-in URL uses PKCE (`code_challenge`, `S256`), contains no client secret, and forces the account chooser (`show_dialog=true`).
*   **Onboarding:** A Premium account is linked, its token saved and it becomes active; a non-Premium account shows "You don't have Spotify Premium", saves nothing, and the user can retry with a different account.
*   **Onboarding:** The daemon refuses to start in live mode until a Premium account is active, and follows the active account when the user switches.
*   **Frontend (Account):** The sidebar's top-right corner shows the active account's name with a **Log out** link; the tray menu shows "Spotify: <name>" and **Log out**.
*   **Frontend (Log out):** Log out deletes only the active account's saved token, removes it from the list, and shows the account window again; closing that window quits the app.
*   **Frontend:** The UI successfully launches as a system tray icon and opens a frameless, frosted-glass sidebar menu when clicked.
*   **Frontend:** Clicking a mood button successfully updates the target coordinates in `config.json` in under 100ms.
*   **Backend (Inference):** The daemon successfully polls the live Spotify API for the currently playing track and retrieves audio features from the fallback chain without crashing.
*   **Backend (Actuation):** When the current mood vector deviates by >0.2 from the target coordinates, the audio actuator calculates a bridging track mathematically plotted between the current and target state, and successfully pushes it to the active Spotify queue.
*   **Backend (Workspace):** `actuator_workspace.py` successfully parses, modifies, and saves a mock VS Code `settings.json` to change the theme without corrupting other JSONC keys or comments.
*   **Backend (Telemetry):** The feedback loop logs an event containing `[timestamp, current_mood_vector, intervention_type, skipped_boolean]` to the local SQLite database when a song changes.
*   **Tests:** `pytest` suite passes for the Markov chain coordinate calculations, JSONC config parsing, UI click events, and Spotify accounts (Premium gate, PKCE URL, redirect handling, multi-account store and switching, account window states, sidebar account display and log out).

*   **Phase 2 (Queue mood):** The current mood equals the mean (valence, arousal) of the session playlist's songs from the one now playing onward; podcasts and songs without a point are excluded.
*   **Phase 2 (Selection):** Every inserted song lies near the Markov waypoint and closer to the target than the current mood; picks are musically compatible with their neighbours (tempo incl. half/double time, Camelot key, loudness) and diverse (MMR).
*   **Phase 2 (Insertion):** Songs are inserted only at positions ≥ 2. In the default random mode they land at random positions in the middle (never appended last while there is a middle); in path mode, never before an upcoming song that is less far along the current-mood → target path, at the cheapest-insertion gap.
*   **Phase 2 (Target change):** Picking a new target removes up to 5 upcoming songs far from it (protected songs excepted) and refills the queue.
*   **Phase 2 (Robustness):** Only one session step runs at a time; right after hand-over (20 s) and on a single observation, a different reported source is not treated as the user taking over.
*   **Phase 2 (Removal):** Only upcoming songs far from both the current mood and the target are removed; now playing, next, podcasts and the user's own queued songs are never removed; per-cycle caps and the minimum queue length are respected.
*   **Phase 2 (Convergence):** In a mocked 4-hour session with a dense catalog, the playlist average falls below 60% of the starting deviation within 2 hours and settles within 0.08 of the target.
*   **Phase 2 (Session lifecycle):** The session playlist is created from the current song + the user's queue, hand-over keeps the song and position, and the playlist is deleted when the session ends (after handing the next songs back to the queue).
*   **Phase 2 (Never stops):** The upcoming queue never drops below its minimum length; the app never pauses playback; user pause and user context switches are respected.
*   **Phase 3 (Languages):** The top-2 languages come from the user's recent listening (a second language only with ≥ 15% share); every song added from the pool is in one of them.
*   **Phase 3 (Language detection):** A title/album marker beats lyrics (KGF 2 `[TELUGU]` with Tamil lyrics → Telugu); native script in the title or lyrics decides next; romanized Hindi/Telugu lyrics are recognised.
*   **Phase 3 (Taste):** Songs by favourite artists score higher than songs by related artists; the user's own songs score ≥ 0.8; artists the user skips count for less; with equal mood fit, the better taste match is picked first.
*   **Phase 3 (New songs):** Each searched artist is also searched for the last two years; recent releases get a freshness bonus.
*   **Phase 3 (Fallback):** With no suitable pool song for a step, the session uses the static catalog; a pool below `min_pool` leaves the catalog in charge; the music never stops.
*   **Phase 3 (Background):** The refresh runs in a background thread, never blocks a cycle, stops when the daemon stops, and never looks the same thing up twice (cache).

## Output Files
*   `frontend/app.py` (Main UI loop, system tray logic, frosted glass setup)
*   `frontend/components.py` (Toggle buttons, layout styling, account name + Log out in the sidebar corner)
*   `backend/daemon.py` (Main background loop and orchestration)
*   `backend/spotify/client.py` (Live client and mock client)
*   `backend/spotify/auth.py` (PKCE sign-in, Premium check, `AccountStore` of linked accounts, switching and log out)
*   `frontend/onboarding.py` (Spotify account window: choose a linked account, Premium notice, waiting, checking, connected, not-Premium and error pages)
*   `backend/features.py` (FeatureProvider chain: Spotify -> ReccoBeats -> CSV)
*   `backend/inference.py` (Spotify polling, feature extraction, and mood calculation; Phase 2: queue average)
*   `backend/mood_space.py` (Markov transition matrix math)
*   `backend/actuators/audio.py` (Phase 1: Markov chain queue pacing and injection; still available as `audio.mode = "queue"`)
*   `backend/actuators/session.py` (Phase 2: the playlist session: create from the queue, hand-over, insert/remove cycles, hand back and delete)
*   `backend/catalog.py` (Phase 2: track database import and search)
*   `backend/selection.py` (Phase 2: scoring, musical similarity, MMR, cheapest insertion, removal rule)
*   `backend/actuators/workspace.py` (IDE and terminal JSONC hot-swapping)
*   `backend/actuators/display.py` (OS-level screen temperature control)
*   `backend/feedback.py` (Logging and intervention evaluation)
*   `recommender/engine.py` (Phase 3: background refresh, artists, pool, taste and freshness)
*   `recommender/profile.py` (Phase 3: taste profile, top-2 languages, freshness)
*   `recommender/language.py` (Phase 3: a song's language from markers, script, lyrics)
*   `recommender/sources.py` (Phase 3: Spotify search/top tracks, Deezer, LRCLIB, iTunes, ReccoBeats analysis)
*   `recommender/store.py` (Phase 3: SQLite cache, `data/recommender.db`)
*   `frontend/daemon_process.py`, `launcher.py`, `packaging/` (Release: the tray app runs the daemon; Windows installer)
*   `shared/config_io.py` (Atomic JSON config reads/writes)
*   `config/config.json` (Mapping definitions for target moods, coordinates; the public Spotify Client ID and redirect URI)
*   `.env` (Optional development overrides; no secrets required)
*   `data/spotify_accounts/` (The user's own linked Spotify accounts: `index.json` + one token file each, only for Premium accounts; git-ignored)
*   `requirements.txt`
*   `SPEC.md.md`, `PLAN.md`, `MODIF.md`, `LOG.md`, `docs/MANUAL.md` (Requirements, design plan, change log, open problems, user manual)
*   `tests/conftest.py` (Shared fixtures)
*   `tests/backend/test_inference.py`
*   `tests/backend/test_actuators.py`
*   `tests/backend/test_spotify_auth.py`
*   `tests/backend/test_selection.py`, `tests/backend/test_catalog.py`, `tests/backend/test_session.py` (Phase 2)
*   `tests/frontend/test_ui.py`
*   `tests/frontend/test_account_window.py`, `tests/frontend/test_daemon_process.py`
*   `tests/recommender/test_language.py`, `tests/recommender/test_engine.py`, `tests/recommender/test_integration.py` (Phase 3)