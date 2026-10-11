# MODIF.md: Change log

Every change made after the original build (Phase 1, completed 2026-10-07, 85 tests). Each modification has a number and a title (M1 is the oldest). The most recent one is tagged **`🆕 LATEST`**. Requirements are in `SPEC.md.md`, open problems in `LOG.md`, the Phase 2 design in `PLAN.md` §7.

**Latest:** M14 · Song recommender: taste + mood + top-2 languages (at the end, after the summary).

---

## Summary of earlier modifications (M1–M13)

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

---

## M14 · Song recommender: taste + mood + top-2 languages · 2026-10-11 · `🆕 LATEST`

**Problem:** songs were picked from a static 2022 dataset by mood alone, so they matched the mood step but not the user's taste or languages, and no new releases were included.

**Findings (tested live on the user's account) that decided the design:**
- Spotify (Feb 2026) returns **no artist genres**, no popularity, no new-releases / artist-top-tracks endpoints, and 10 search results per page.
- **ReccoBeats recommendations don't fit:** it knew 10 of the user's top 25 songs (almost no Telugu ones), and its results ignore language (Hindi seed → English and Portuguese songs). Not used.
- **Deezer related artists** match taste well (Sid Sriram → Devi Sri Prasad, Harris Jayaraj; Atif Aslam → Pritam, Arijit Singh).
- **LRCLIB lyrics** were found for 10 of 12 songs, mostly in native script, which makes language detection reliable.
- **iTunes previews + ReccoBeats audio analysis** give mood features for songs ReccoBeats doesn't know (*Maate Vinadhuga* → valence 0.28, energy 0.29).

**Modifications:**
1. **New package `recommender/`** (self-contained, so it can move to its own repository):
   - `profile.py`: taste profile from Spotify top tracks (4 weeks, 6 months) plus Moodly's plays and skips (14 days). It gives favourite artists and language shares. **Top-2 languages:** the second counts only with ≥ 15% share.
   - `language.py`: a song's language, decided in this order: title/album marker, native script, lyrics script, romanized lyric words, then the artist's usual language.
   - `sources.py`: Spotify (top tracks, search, track), Deezer (related artists), LRCLIB (lyrics), iTunes (preview matching), ReccoBeats (audio analysis), and `PreviewFeatureProvider`. Every call has a timeout and per-host spacing.
   - `store.py`: SQLite cache `data/recommender.db` (songs, languages, artists, profile).
   - `engine.py`: `Recommender.refresh()` runs profile → artists (favourites + related) → Spotify search (2 pages + last two years per artist) → languages → features → **pool** (top-2 languages, taste ≥ 0.05, features, one copy per song). It computes taste (favourite > related, language share, skip penalty) and freshness (1 → 0 over a year). Work per refresh is bounded, and it stops early on shutdown.
2. **Selection** (`backend/selection.py`): a language filter, plus the score terms `− w_taste·taste − w_fresh·fresh` (0.12, 0.03).
3. **Session** (`backend/actuators/session.py`): it picks from the pool, in the top-2 languages. If the pool has nothing near the step, it first accepts a bigger step from the pool (average ≤ `pool_max_jump` 0.3), then falls back to the static catalog. Queue songs without features are passed to the recommender for analysis. Features of queue songs are also looked up in the catalog table.
4. **Daemon** (`backend/daemon.py`): builds the recommender in live mode and refreshes it in a background thread at start and every 30 min. It hands the pool to the session once the pool has ≥ 30 songs, adds `recommender` (status, languages, pool size) to `state.json`, and stops the refresh on shutdown.
5. **Catalog types** (`backend/catalog.py`): `CatalogTrack` / `TrackTable` gained `language`, `taste`, `fresh`.
6. **Spotify client** (`backend/spotify/client.py`): `top_tracks_raw`, `search_tracks_raw`, `track_raw` (live + mock). No new scope.
7. **Config** (`config/config.json`): new `recommender` section, `session.w_taste`, `session.w_fresh`, `session.pool_max_jump`, `paths.recommender_db`.

**Results (live, read-only, user's account):** top languages **Telugu 52%, Hindi 33%** (Punjabi 10% and English 3% left out). Favourites: Pritam, Arijit Singh, Thaman S, Sid Sriram. The pool grew 36 → 66 → 86 songs over 3 refreshes (~1–1.5 min each, in the background), 16 of them released in the last year. Planned additions from the user's own queue (mood 0.49, 0.64):
- toward Calm: *Tainu Khabar Nahi*, *Samjhawan*, *Vaarevaa Vaarevaa*
- toward Energized: *Hoyna Hoyna*, *Jaan Se Guzarte Hain*

**Tests:** 218 pass (new: 18 in `tests/recommender/`: language detection, profile and top-2 rule, freshness, pool building, duplicates, lyrics budget and cache, artist fallback, skip penalty, early stop, language filter and taste in the score, pool vs catalog fallback, daemon background refresh and handover).

**Not done / limits** (in `LOG.md` #18–#21): the pool starts small and grows each refresh; about 25% of songs still have no known language; it depends on free third-party services; dubbed songs without a marker may get the wrong language. Not yet run inside a live session (only read-only checks).
