## v3.9.17 — 2026-09-09 — Rotation-consistent normalize + fixed GOP for RTMP

Root-caused two remaining sources of the "periodic zoom-in / zoom-in-more /
snap-back-to-fit" artifact that could still occur even with Normalize-
Before-Live (v3.9.9) and the locked canvas (v3.9.5) enabled.

### app.py
- **`normalize_video_for_live()`**: removed `-noautorotate` from both the
  source-audio and silent-audio ffmpeg commands. It was disabling ffmpeg's
  automatic Display-Matrix rotation correction on decode with no
  compensating `transpose` filter, so a portrait mobile clip (rotate=90/270)
  was normalized in its RAW sensor (landscape) pixel layout. Meanwhile
  `get_video_info()` — used to size the locked canvas in `auto` resolution
  mode — already swaps `width`/`height` for the SAME rotation tag. The two
  functions disagreed about whether rotation had been applied, so any
  portrait clip in the playlist was scaled/padded into the wrong shape
  relative to the canvas it was sized for. Dropping `-noautorotate` makes
  normalization apply the same rotation `get_video_info()` assumes, so
  portrait and landscape clips share one consistent, correctly-oriented
  canvas for the whole stream.
- **`build_ffmpeg_cmd()`**: the libx264/hardware encoder branches never set
  a keyframe interval — x264's default scenecut-driven GOP (~250 frames,
  ~8s at 30fps) with `-sc_threshold` unset lets keyframes land at
  unpredictable spacing. Several RTMP ingest platforms react to a missed
  keyframe window by holding/repeating the last good frame or falling back
  to a different internal transcode rendition until the next keyframe —
  which can present exactly like a sudden scale/zoom change that later
  snaps back, and explains why the artifact can appear on one destination
  platform and not another with an otherwise-identical local pipeline.
  Every encoder branch now sets a fixed 2-second GOP: `-g <2*fps>
  -keyint_min <2*fps>`, plus `-sc_threshold 0` for libx264 so no extra
  scenecut keyframes land off that grid.
- `MARIO_VERSION` bumped to **3.9.17**.

### Diagnostic note (not a code change)
If the zoom still reproduces after this fix, first record locally
(`record=true`, or `rtmp_url` empty + v4l2 output) while also live to the
affected platform. If the local recording is clean, the artifact is being
introduced downstream of Mario (the platform's ingest/transcode pipeline,
or — when consumed as a browser "camera" via v4l2loopback — that site's
own `getUserMedia`/`applyConstraints` resolution renegotiation), not in
this codebase. Also confirm the running profile actually has
`normalize_before_live: true` — a playlist/profile saved before v3.9.9
may have persisted `false` and silently kept using the older, unnormalized
live path.

- **`prepare_normalized_playlist()`**: sources are now deduped once, then
  normalized CONCURRENTLY on a bounded thread pool
  (`NORMALIZE_MAX_WORKERS = min(4, cpu_count)`) instead of one ffmpeg
  process at a time. Each clip's normalization is an independent
  subprocess, so on any multi-core machine this cuts "Preparing Stream"
  wall-clock time roughly by the worker count for playlists with several
  sources — the recurring complaint that Normalize-Before-Live takes a
  long time before every Start. First-failure-aborts-start semantics are
  unchanged: on the first error, remaining jobs are cancelled/drained and
  the same `RuntimeError` is raised.

### Tests
- `python3 -m py_compile app.py auth.py state.py` ✓
- `pytest -q` → 35 passed.
- Manual concurrency check (mocked ffmpeg): 6 clips at ~0.2s each finished
  in ~0.4s (2 rounds of 4 workers) instead of ~1.2s serially; failure
  still propagates as `RuntimeError`.

## v3.9.16 — 2026-06-10 — Per-clip start-offset (WMP-style trim)

Every playlist item now supports `start_offset_seconds`. Frontend gets a
slider + HH:MM:SS input + reset/preview buttons per row; backend honors
the offset through both code paths (normalize ON and OFF), persists it in
SQLite, and survives save/load/export of saved playlists.

### app.py
- `clean_playlist_items()` accepts, sanitizes, and clamps
  `start_offset_seconds`. Float ≥ 0; if `info.duration` is known the
  value is clamped to `duration − 1` so a clip never starts at its end.
- New `normalized_map_key(src, offset)` — `_normalized_paths` is now
  keyed by `"abs_src|offset"` so the same source at two different
  offsets gets two distinct cache files instead of colliding.
- `build_normalized_cache_key()` includes `start_offset_seconds` in the
  hash → editing the offset regenerates the cache automatically.
- `NORMALIZATION_VERSION` bumped to **3** (forces one-time cache refresh
  for existing users).
- `normalize_video_for_live()` adds input-level `-ss <SECS>` BEFORE
  `-i src` when `start_offset_seconds > 0`. Works for both audio paths
  (source-audio and silent-AAC); the unsafe `[0:a?]` is still avoided.
- `prepare_normalized_playlist()` passes per-clip offset, dedupes by the
  composite key, and writes progress messages like
  `Normalizing 2/4 — clip.mp4 from 07:00` /
  `Using cached normalized clip 2/4 from 07:00`.
- `build_ffmpeg_cmd()` concat tempfile now writes the concat demuxer's
  `inpoint <SECS>` directive immediately after `file '…'` when
  Normalize-Before-Live is OFF AND the clip has an offset. When
  Normalize is ON the cache file already starts at the offset, so no
  `inpoint` is emitted (concat stream layout stays uniform).
- New helper `_fmt_hms()` shared with progress messages.
- `MARIO_VERSION` bumped to `3.9.16`.

### state.py
- `_ensure_playlist_columns()` auto-adds
  `start_offset_seconds REAL NOT NULL DEFAULT 0` if missing — old DBs
  upgrade in place; existing rows default to 0.
- `save_playlist()` / `load_playlist()` round-trip the new column.

### Saved-playlist JSON files
- No format change needed: `/api/playlists/save` writes raw items and
  `/api/playlists/load` re-runs `clean_playlist_items()`, so the new
  field is preserved for both old and new files.

### static/js/app.js
- Per-item UI: range slider (0 → duration), HH:MM:SS text input,
  Reset, and Preview-from-offset buttons. Live label
  `Clip starts at: 07:00` (or `Beginning`) with a "near end" warning
  when offset ≥ 90% of duration.
- Helpers: `_fmtHMS`, `_parseHMS`, `_clampOffset`, `setClipOffset`,
  `setClipOffsetText`, `resetClipOffset`, `previewClipFromOffset`.
  Invalid HH:MM:SS input resets to 0 with a toast.
- Edits debounce-sync (~600 ms) to `/api/playlist/reorder` so rapid
  slider drags don't spam the backend.
- `addToPlaylist` / `addAllToPlaylist` seed `start_offset_seconds: 0`.

### templates/index.html / static/css/dashboard.css
- Playlist row split into `.pl-row` + `.clip-trim` block; new
  `.ct-slider`, `.ct-text`, `.ct-label.warn` styles.

### Tests
- `python3 -m py_compile app.py auth.py state.py` ✓
- `pytest -q` → 35 passed.

### Known limitations
- The Preview button opens the source video in a popup and seeks via
  `video.currentTime`; the real FFmpeg-served preview endpoint is
  unchanged. If popups are blocked, the offset still saves correctly.
- "End at time" / trim-range is not in this pass (Start-from only, as
  scoped). The cache key already includes the offset so adding `-to`
  later only requires extending the same key.
- Existing normalized caches built under `NORMALIZATION_VERSION=2` will
  be regenerated on first Start (one-time cost).

## v3.9.15 — 2026-06-10 — Real Normalize/Start progress tracking

### app.py
- New in-memory `_start_progress` object + `_set_start_progress()` /
  `_reset_start_progress()` / `_snapshot_start_progress()` helpers
  (thread-safe; contains no secrets, only basenames).
- `start_stream()` now stamps progress through every phase: `validating`
  (3%) → `syncing` (6%) → `preparing` (8%) → `normalizing` (10–85%) →
  `building` (88–90%) → `launching` (96%) → `live` (100%). On failure the
  panel is left visible with `phase='error'` and a clear `error` message.
- `prepare_normalized_playlist()` updates `current_index`, `current_clip`,
  `message`, and `percent` per clip; distinguishes reused-cache vs
  freshly-encoded clips. Structure leaves room for adding per-file FFmpeg
  `-progress` parsing in a later pass.
- New `_launch_ffmpeg()` progress stamps at the build/launch boundaries
  plus `[START] launching FFmpeg` log line.
- New endpoint: `GET /api/stream/start-progress` (auth-required, rate-
  limited, GET-only). Returns `{success, progress:{...}}`. Frontend polls
  every 500 ms while `_startInFlight` is true.
- Added explicit `[START] / [NORMALIZE] complete / [START] launching FFmpeg
  / [START] stream started pid=…` log lines. No stream keys logged.
- `MARIO_VERSION` bumped to `3.9.15`.

### static/js/app.js
- New "Preparing Stream" panel poller. On Start, the panel is shown
  immediately, polling begins, and `_renderProgress()` repaints the bar,
  step label, current clip, `i/N` counter, and status line every tick.
- On success → panel reaches 100 % "Stream started" then auto-hides after
  3 s. On error → panel stays visible with red bar and full error message;
  Start buttons re-enable. On already-running → panel shows 100 % "Stream
  already running" and hides shortly after.
- Poller is stopped in `finally`, so it never leaks.

### templates/index.html
- Dashboard Quick Actions card now contains `#startProgressPanel` with
  bar/percent/step/clip/index/status/error fields.

### static/css/dashboard.css
- New `.start-progress` styles: bar, fill, error/done states.

### Known limitations
- Per-file FFmpeg `-progress` parsing is not yet plugged in; current
  granularity is one step per clip (start + finish). The helper layout
  was kept intentionally simple so a future pass can add intra-clip
  progress without touching the API contract.
- The Stop button during the prepare phase still lets the in-flight
  normalization finish the current clip; FFmpeg will simply not be
  launched if the request has already errored out before the launching
  phase. A true cooperative cancel is left for a later pass.

## v3.9.14 — 2026-06-10 — All Start buttons share label state

### static/js/app.js
- `checkStatus()`, `_forceLiveUI()`, and `_setStartButtonsState()` now
  apply label changes to EVERY button returned by `_allStartButtons()`
  (Dashboard `.js-start-stream` + Live Studio `#startBtn`), not only
  `#startBtn`. Live → "▶ Live" on all; Idle → "▶ Start Stream" on all;
  Starting → "⏳ Starting…" on all.
- No "⏳ Starting…" label can persist on a button after the stream goes
  live or idle.

## v3.9.13 — 2026-06-10 — Start lock hardened (lock-before-await)

### static/js/app.js — startStream()
- `_startInFlight = true` now set IMMEDIATELY after the two guard checks,
  BEFORE any `await`, log, or `syncPlaylistToBackend()` call. Rapid clicks
  can no longer enter the start flow twice.
- `disableAllStartButtons()` + `showStartingState()` disable every start
  button (dashboard quick-action + Live Studio #startBtn) via a shared
  selector `[data-action="start-stream"], .js-start-stream, #startBtn`.
- Added a backend `/api/status` pre-check inside the lock: if the server
  already reports `streaming=true`, the flow exits with an info toast
  ("Stream already running") and `_forceLiveUI(true)` — no
  "Preparing videos…", no `syncPlaylistToBackend()`, no POST.
- "Already running" is surfaced as `info`, never `error`.
- `checkStatus()` now updates ALL start buttons (not just `#startBtn`)
  and sets `window._isStreaming` so the early-bail path is reliable.

### templates/index.html
- Dashboard quick-action Start and Live Studio Start now both carry
  `data-action="start-stream"` and class `js-start-stream`. Both call
  the same `startStream()` and respect the same lock. Keyboard shortcut
  `S` also calls `startStream()` and inherits the lock.

---

## v3.9.12 — 2026-06-10 — Idempotent Start Stream (no re-normalize while live)


### app.py — POST /api/stream/start
- Phase 1 idempotent guard moved to the TOP of the handler, BEFORE
  validation, playlist sync, shuffling, normalization, and FFmpeg launch.
  While `streaming` is true and `ffmpeg_process.poll() is None`, the
  endpoint returns **HTTP 200** with `{success:true, already_running:true,
  message:"Stream already running", pid, status:"live"}`. No re-normalize,
  no concat rebuild, no second FFmpeg, no output glitch.
- Stale-state recovery: if `streaming=true` but the process is dead, the
  flags are cleared and the request proceeds to a fresh start.
- Race-safe: the same check is repeated inside `stream_lock` just before
  `_launch_ffmpeg` so two concurrent requests cannot both launch.
- `400 "Stream is already running"` is removed — already-running is now
  a normal, idempotent success.
- Debug logs: `[START] request received`, `[START] already running pid=…`,
  `[START] starting normalization / launch`, `[START] launched ffmpeg pid=…`.

### static/js/app.js — Start/Stop locks + already_running handling
- `startStream()` bails early with `[START] ignored because
  startInProgress` / `[START] ignored because already live` BEFORE any
  "Preparing videos…" log line is emitted, so the normalization banner can
  no longer reappear while the stream is live.
- Success branch checks `d.already_running` first and shows a neutral info
  toast ("Stream already running") instead of an error. Legacy
  400-already-running fallback retained for older backends.
- `stopStream()` gains a `_stopInFlight` lock + `!_isStreaming` early
  return, "⏳ Stopping…" button state, and cannot be spammed.

### Tests
- `python3 -m py_compile app.py auth.py state.py` → OK
- `pytest -q` → **35 passed**

## v3.9.11 — 2026-06-09 — Start-Stream feedback + gunicorn read-only fix

### static/js/app.js — Start Stream UX
- Start button now shows "⏳ Starting…" toast + "Starting stream…" toast, is
  disabled while the request is in flight, and cannot be spammed
  (`_startInFlight` guard + early-return when `_isStreaming`).
- On success: emit "Stream started" toast, force topbar pill → Live, set
  `streamStatus` → Live, switch dashboard `dmStatus` → LIVE, enable Stop +
  Skip, disable Start (`_forceLiveUI(true)`), then call refreshStatus /
  refreshDashboard / refreshHealthPage / refreshStreamStats.
- 400 "Stream is already running" is handled explicitly: warn-toast
  "Stream already running" + force UI into Live state instead of generic
  error.
- Any other non-2xx response surfaces the exact backend `error` field in
  both the toast and the logs panel.
- `/api/status` is now the source of truth: `checkStatus()` repaints the
  Start button label/disabled state every 5 s (and `startStream` triggers
  an extra `checkStatus()` 400 ms after the response). Streaming label
  switched from "Streaming" → "Live".
- Fixed wrong element id (`startStreamBtn` → `startBtn`).

### static/js/app.js — Health checklist wording
- "Stream daemon" row renamed to "Stream Engine"; shows neutral
  Idle (yellow dot) when not streaming, green Live when streaming, and
  only goes red if `/api/health` explicitly reports `ok:false` or
  `checks.stream.ok:false`. `refreshHealthPage` now also fetches
  `/api/status` to know the live state.

### gunicorn_conf.py + mario.service — read-only `.gunicorn` fix
- Gunicorn's heartbeat/control files were being written to
  `/home/<user>/.gunicorn`, which fails under `ProtectHome=read-only`
  ("Control server error: Read-only file system: '/home/mario/.gunicorn'").
- `gunicorn_conf.py` now forces `worker_tmp_dir` and `tmp_upload_dir` to
  `/tmp/mario_gunicorn` (override via `MARIO_GUNICORN_TMP`).
- `mario.service` adds `/tmp/mario_gunicorn` to `ReadWritePaths` and sets
  `Environment=MARIO_GUNICORN_TMP=/tmp/mario_gunicorn`.

### app.py
- Bumped `MARIO_VERSION` → 3.9.11.

---

## v3.9.10 — 2026-06-09 — Normalize audio fix + setup.sh hardening

### app.py — audio bug in Normalize Before Live
- Removed the unsafe `[0:a?]` reference from `normalize_video_for_live()`
  filter_complex; FFmpeg fails the whole encode on silent inputs with that
  syntax.
- New helper `_probe_has_audio(src)` runs ffprobe to detect whether a source
  has an audio stream BEFORE the command is built.
- Two distinct FFmpeg commands:
  - **Has audio:**  `-i src -vf <scale/pad/setsar/setdar/fps/format>
    -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p
    -c:a aac -ar 48000 -ac 2 out.mp4`
  - **Silent:**     `-i src -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000
    -vf <…> -map 0:v:0 -map 1:a:0 -shortest -c:v libx264 …
    -c:a aac -ar 48000 -ac 2 out.mp4`
- Every normalized cache file ends up with identical AAC stereo 48 kHz audio,
  so the concat demuxer sees a stable layout across mixed (sound + silent)
  playlists.
- `NORMALIZATION_VERSION` bumped 1 → 2 (invalidates stale cache).
- Safe logs: `source_has_audio=…`, `audio_mode=source|silent`, `output=…`.

### setup.sh — install / systemd reliability
- Detects REAL invoking user via `SUDO_USER` fallback; refuses to install as
  root unless explicitly confirmed. No more accidental
  `mario@root.service`.
- Always installs project code into `/home/<user>/mario-stream` via rsync
  (excluding venv / __pycache__ / .git / data dirs) regardless of where the
  script is run from.
- Creates writable folders and DB/history files in `~/mario_data/`,
  `chown` to the real user.
- Writes `~/.mario_env` in pure `KEY=value` form (no `export`),
  upserts keys idempotently (`MARIO_DB_PATH`, `MARIO_HISTORY_FILE`,
  `MARIO_SCAN_ROOTS`, `MARIO_NORMALIZED_CACHE_DIR`), strips legacy
  `export` prefixes, preserves existing `MARIO_PASSWORD`.
- Disables stale `mario@root.service` when the real user is not root.
- Installs `mario@.service` and offers interactive enable/start prompts;
  honours `--yes` and `--no-start`.

### mario.service
- Unchanged in v3.9.10 — `ReadWritePaths` already covers
  `mario_data`, `mario_media`, `mario_playlists`, `mario_profiles`,
  `mario_recordings`, `mario_thumbs`, `mario_watermarks`, `/tmp`, so
  SQLite + normalized_cache writes succeed under
  `ProtectHome=read-only`.

---

## v3.9.9 — 2026-06-09 — REAL "Normalize Before Live" (live zoom fix)

The v3.9.8 UI toggle did nothing on the backend — live FFmpeg still consumed
the original source files, so the mid-clip zoom artifact (SAR/DAR/rotation
metadata change + filter reinit) still happened at fixed timestamps. v3.9.9
ships the actual pipeline.

### app.py — backend normalization
- New helpers `get_normalized_cache_dir()`, `build_normalized_cache_key()`,
  `normalize_video_for_live()`, `prepare_normalized_playlist()`.
- Each source is pre-encoded into `~/mario_data/normalized_cache/<sha1>.mp4`
  before live FFmpeg starts, with FIXED canvas, fps, SAR=1, DAR=W/H,
  yuv420p, and a stereo 48 kHz AAC track (silent sources get an `anullsrc`
  mix so concat layout never changes).
- Cache key = abs path + size + mtime + W + H + fps + fit_mode +
  `NORMALIZATION_VERSION`. Sources reuse the cache when unchanged; cache is
  invalidated automatically on edit, resolution change, or fps change.
- `_launch_ffmpeg` runs normalization AFTER it has locked the canvas, then
  REBUILDS the FFmpeg command so the concat tempfile references the cache
  paths — not the originals. Logs prove it:
  `[NORMALIZE] source=…`, `[NORMALIZE] cache=…`, `[NORMALIZE] reused=…`,
  `[LIVE] using normalized playlist=true`.
- Normalization failure raises a clean error — live MUST NOT start; the UI
  surfaces `Normalization failed: …`. There is no silent fallback.
- Monitor's clip-tracker map now contains both original AND cache paths so
  per-clip stats keep working with normalized inputs.
- `build_ffmpeg_cmd`: when `_normalized_paths` is set, the concat file is
  written with cache paths and `src_all_audio` is forced True (every cache
  always has audio). The final live filter chain (scale+pad+setsar+setdar+
  fps+yuv420p) is preserved as a safety layer.

### setup.sh
- Creates `~/mario_data/normalized_cache` and `~/mario_media`.
- `~/.mario_env` is now written WITHOUT `export` so systemd
  `EnvironmentFile=` reads every key. Bash users: `set -a; source
  ~/.mario_env; set +a`. New keys: `MARIO_DATA_DIR`,
  `MARIO_NORMALIZED_CACHE_DIR`, broadened `MARIO_SCAN_ROOTS`
  (`~/mario_media`, `~/Downloads`).
- Idempotent appender migrates older env files to the new keys and tolerates
  legacy `export X=Y` lines.

### mario.service
- `ReadWritePaths=` covers the full data tree:
  `/home/%i/{mario_data,mario_media,mario_playlists,mario_profiles,mario_recordings,mario_thumbs,mario_watermarks}` + `/tmp`.

### static/js/app.js — UI feedback
- `startStream()` shows "Preparing videos… Normalizing N clip(s)" and
  "Video Stability: ON · Mode: <fit> · Canvas locked", disables the start
  button while preparing, surfaces `Normalization failed: …` on backend
  errors, and warns when the toggle is OFF.

### Limitations
- First start per source spends real time encoding (libx264 veryfast crf18);
  subsequent starts reuse the cache instantly.
- Cache is keyed per (W, H, fps, fit_mode); changing any of those reencodes.
- Cache is not auto-pruned — remove `~/mario_data/normalized_cache` manually
  if it grows too large.

---

## v3.9.7 — 2026-06-09 — Fix SQLite "unable to open database" under systemd

`POST /api/playlist/reorder` was returning 500 because
`state.save_playlist()` raised `sqlite3.OperationalError: unable to open
database file`. Root cause: under `ProtectHome=read-only` the app could
only write to paths listed in `ReadWritePaths=`, but `~/mario_state.db`
was sometimes missing / unwritable and there was no dedicated data dir.

### setup.sh
- Creates `~/mario_data/` (chmod 755) plus pre-touches
  `~/mario_data/mario_state.db` and `~/mario_data/mario_history.json`
  (chmod 664) so systemd can open them on first run.
- Writes `MARIO_DB_PATH` and `MARIO_HISTORY_FILE` into `~/.mario_env`
  (also appended to existing env files — idempotent).
- One-shot migration: copies legacy `~/mario_state.db` and
  `~/mario_history.json` into `~/mario_data/` if the new files are empty.

### mario.service
- `ReadWritePaths=` now includes `/home/%i/mario_data` so the SQLite DB
  and history JSON remain writable under `ProtectHome=read-only`.

### app.py
- `/api/playlist/reorder` wraps `state.save_playlist()` and returns a
  clear JSON error (`{"error":"Database is not writable", ...}`, HTTP 500)
  on `sqlite3.OperationalError` instead of a silent 500.

### static/js/app.js
- `syncPlaylistToBackend()` now surfaces failures via `toast()` with
  "Cannot save playlist: database is not writable." when the backend
  reports a DB error.

### state.py
- (already creates `os.path.dirname(DB_PATH)` before connecting — verified.)

### Tests
- `pytest -q` → 35 passed.

---

## v3.9.6 — 2026-06-09 — Internal /login page (no more browser Basic-Auth popup)

The browser's native Basic-Auth dialog is replaced with a proper Flask
session-based login flow served from `/login`. Credentials still come from
`MARIO_USER` / `MARIO_PASSWORD`. Auth is NOT removed.

### Backend (app.py)
- **Flask session**: `app.secret_key` persisted at `~/.mario_secret` (or
  `MARIO_SECRET_FILE` / `MARIO_SECRET_KEY` env). Session cookie is
  `mario_session`, `HttpOnly`, `SameSite=Lax`, 12 h lifetime
  (`MARIO_SESSION_HOURS`).
- **`GET /login`**: renders dark-themed login page, sets CSRF cookie,
  honors `?next=` (relative paths only).
- **`POST /login`**: validates credentials with `hmac.compare_digest`,
  sets `session['mario_user']`, redirects to `next`. Failures return 401
  with the form re-rendered.
- **`/logout`** (GET or POST): clears the session and redirects to `/login`.
- **Rate limiter**: 8 failed login attempts per IP per 15 min returns 429
  (configurable via `MARIO_LOGIN_MAX_ATTEMPTS` / `MARIO_LOGIN_WINDOW_SEC`).
- **`_auth_gate`** now accepts **session OR Basic-Auth** (Basic-Auth kept
  for CLI / curl). When unauthenticated:
  - HTML requests → `302 /login?next=…`
  - JSON / API requests → `401 {"error":"Login required","login_url":"/login"}`
  - **No `WWW-Authenticate: Basic` header is ever sent** → browsers no
    longer show their native Basic-Auth dialog.
- **CSRF**: `_csrf_ok()` now also accepts `csrf_token` form field (login
  form uses it). All POST/PUT/DELETE API endpoints still require the
  `X-CSRF-Token` header — unchanged.
- `/` is no longer in `_PUBLIC_ENDPOINTS` — the dashboard cannot be
  reached before login.

### Frontend
- `templates/login.html`: standalone dark UI (Inter font, cyan accent),
  hidden CSRF + `next` fields, autofocus on username.
- `templates/index.html`: added a sign-out button (⎋) in the topbar.
  Global `fetch` wrapper now redirects to `/login?next=…` when any
  same-origin call returns `401 {login_url}` (session expired).

### Tests
- New `tests/test_login.py` (6 tests): unauth redirect, JSON 401 without
  `WWW-Authenticate`, login form renders, successful login → session,
  bad password → 401, logout clears session.
- Full suite: **35/35 passing**.

### Migration
- No action required. On first start the app writes `~/.mario_secret`
  (0600). Existing `MARIO_USER` / `MARIO_PASSWORD` continue to work.
- curl / scripted clients can keep using Basic-Auth (the CLI path is
  preserved); browsers will use the new login page.

---

## v3.9.5 — 2026-06-09 — Locked output canvas (mid-stream zoom fix)

This release fixes the bug where the live stream could spontaneously zoom in
several minutes into playback or on a clip transition, especially when the
playlist mixed clips with different resolutions or aspect ratios.

### Backend (app.py)
- **Canvas lock**: `build_ffmpeg_cmd` now honours `_locked_wh` on `params`.
  `_launch_ffmpeg` stamps the chosen `(W, H)` into both the live `params`
  dict and `last_stream_params` immediately after the first build, so
  auto-restart resume and the `/api/stream/skip` rebuild reuse the EXACT
  same canvas. `auto_res_mode` can no longer pick a different canvas
  mid-stream.
- **Stable filter tail for every fit mode**: every chain now ends with
  `setsar=1, setdar=W/H, fps=FPS, format=yuv420p` (previously only
  `setsar=1, format=yuv420p`). This pins SAR, DAR, fps and pixel format
  so a later clip with different stream parameters cannot change the
  geometry FFmpeg hands to v4l2loopback / RTMP.
- **Default `fit` filter (unchanged shape, now with locked tail)**:
  `scale=W:H:force_original_aspect_ratio=decrease,
   pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black,
   setsar=1, setdar=W/H, fps=FPS, format=yuv420p`.
  No `crop=`, no `force_original_aspect_ratio=increase` in the default path.
- **Constant frame rate output**: added `-vsync cfr` to the ffmpeg
  command line so timing drift across clip transitions can't shrink or
  stretch frames downstream.
- **Output-side geometry pinning**: RTMP and tee outputs now carry
  `-s WxH -pix_fmt yuv420p`; the v4l2 output carries
  `-s WxH -r FPS -pix_fmt yuv420p`. Belt-and-suspenders against any
  container/encoder negotiating a different frame size from what the
  filter graph emits.
- **Geometry debug log line** on stream start:
  `[GEOMETRY] canvas=WxH fps=N fit_mode=fit crop=False pad=True
  canvas_locked=True filter=...`. Visible in the Live Logs panel and
  in `journalctl -u mario@<user>` — confirms at a glance that no crop is
  in use and that the canvas is locked. Never logs stream keys (uses
  the existing `_redact` pipeline).
- Version bumped to **3.9.5**.

### Manual test checklist (live-stream geometry stability)

1. **Mixed aspect ratio, long clip + transition** — Playlist:
   `landscape_1920x1080.mp4` (≥3 min) then `portrait_1080x1920.mp4`.
   Output `1280x720`, Fit / No Zoom. Expected: first clip plays at full
   width with no zoom for the full 3+ min, transition does NOT zoom, the
   portrait clip shows centered with black side bars (no crop).
2. **Resolution staircase** — Playlist: `720p.mp4`, `1080p.mp4`,
   `4k.mp4`. Output `1280x720`. Expected: every transition keeps the
   same 1280x720 canvas — no zoom, no crop, no scaling artifacts.
3. **Single long clip** — One 10-minute video, any resolution. Expected:
   no zoom-in at any point in the 10 minutes (proves it isn't a periodic
   filter-graph reinit).
4. **Both outputs** — Repeat tests 1/2 once with v4l2 output and once
   with an RTMP target. Both must remain geometrically stable.
5. **Verify in logs**: `[GEOMETRY] ... crop=False pad=True
   canvas_locked=True` must appear on every stream start in `fit` mode.

---

## v3.9.4 — 2026-06-09 — Hardened no-zoom live stream + Video Fit Mode

### Backend (app.py)
- `validate_stream_params` accepts `fit_mode` ∈ `{fit, fill, stretch}`,
  defaulting to `fit`. Unknown values are coerced to `fit`.
- `build_ffmpeg_cmd` now switches the video filter chain on `fit_mode`:
  - **fit** (default): `scale=W:H:force_original_aspect_ratio=decrease,
    pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black, setsar=1, format=yuv420p`.
    Preserves aspect ratio, pads with black bars. **Never crops, never
    auto-zooms** — even when the playlist mixes portrait and landscape clips.
  - **fill**: `scale=...=increase, crop=W:H, setsar=1, format=yuv420p`.
    Opt-in, may crop edges.
  - **stretch**: `scale=...=disable, setsar=1, format=yuv420p`. Opt-in,
    distorts video — not recommended.
- `auto_res_mode` default remains `first` (safe canvas selection).

### Frontend
- Live Studio gains a **Video Fit Mode** selector with a live status badge
  ("Fit / No Zoom", "Fill / Crop (may crop)", "Stretch (distorts)").
- `getStreamSettings()` / `applyStreamSettings()` include `fit_mode`, so
  saved profiles persist and restore the setting. Default profiles that
  omit it stream as Fit / No Zoom.

### Audit
- Default FFmpeg video filter (verified, no crop):
  `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p`
- 1280×720 output + vertical source → black side bars (no crop).
- 1080×1920 output + horizontal source → black top/bottom bars (no crop).
- No `crop=` filter is emitted unless the user explicitly selects Fill.
- No `force_original_aspect_ratio=increase` unless user selects Fill.

## v3.9.3 — 2026-06-08 — Interactive systemd install in setup.sh

### setup.sh
- Detects `CURRENT_USER="$(whoami)"` and computes
  `SYSTEMD_UNIT="mario@${CURRENT_USER}.service"`. Honors
  `MARIO_SYSTEMD_UNIT` override.
- Installs the template unit to `/etc/systemd/system/mario@.service`
  and runs `sudo systemctl daemon-reload` automatically.
- New interactive prompts via `ask_yes_no` helper:
  - "Enable Mario Stream to start automatically after VPS reboot?" (default Y)
  - "Start Mario Stream now?" (default Y)
- Non-interactive support:
  - `--yes` / `-y`: enable autostart and start service without asking.
  - `--no-start`: complete setup and (optionally) enable autostart,
    but skip the immediate start.
  - No TTY: safe defaults — autostart yes, start yes.
- Final summary prints unit name, autostart status, run status, app
  URL, and the useful `status / journalctl / restart / stop / disable`
  commands.

## v3.9.2 — 2026-06-08 — Autostart, Server Controls, dark-theme lock

### UI / theme
- Locked dashboard to dark mode. Inline pre-script forces
  `data-theme="dark"` and overwrites stale `mario_theme=light`
  in localStorage before CSS evaluates. Light theme is temporarily
  disabled (toggle button shows a toast).
- New cyan-on-charcoal palette baked into `:root` so the UI renders
  correctly even before theme JS runs. `html[data-theme="light"]` is
  overridden to use the dark palette as a safety net.
- Contrast fixes for cards, inputs, topbar pills, sidebar items,
  legacy status pills and buttons.
- Added `<meta name="color-scheme" content="dark">`.

### Autostart / deployment
- `systemd` template (`mario@.service`) usage is documented in
  README + setup.sh (`daemon-reload`, `enable --now`, `status`,
  `journalctl`, `restart`).
- `docker-compose.yml` already had `restart: unless-stopped` —
  documented along with `docker compose ps / logs / restart`.
- `_deployment_info()` helper detects systemd vs docker vs manual,
  reads `systemctl is-enabled`, probes whether passwordless
  `sudo systemctl restart` works, and surfaces a `reboot_allowed`
  flag based on `MARIO_ALLOW_SERVER_REBOOT`.
- `/api/health` and `/api/app/info` now include a `deployment` block.

### Server Controls (System Health page)
- New panel shows deployment mode, unit name, autostart status,
  restart-controls availability and reboot-enabled state.
- New endpoints (auth + CSRF + POST only, fixed command, no shell):
  - `POST /api/system/restart-app` → `sudo -n systemctl restart <unit>`
    in a background thread after returning JSON. Refuses to run
    inside Docker.
  - `POST /api/system/reboot-server` → `sudo -n /sbin/reboot`,
    gated by `MARIO_ALLOW_SERVER_REBOOT=1` AND a body field
    `{"confirm":"REBOOT"}`. UI also requires the user to type
    `REBOOT` in a prompt.
- README documents the narrow NOPASSWD sudoers lines required
  for each button. `NOPASSWD: ALL` is explicitly warned against.

### Env vars added
- `MARIO_SYSTEMD_UNIT`, `MARIO_SYSTEMD_USER`, `MARIO_ALLOW_SERVER_REBOOT`.

### Preserved
- CSRF / apiFetch on every mutation.
- Playlist sync (incl. drag/drop async sync from v3.9.1).
- Backup export/import using `state.DB_PATH`.
- Preview extension allowlist, log redaction.
- Docker bind-mount layout, systemd `ProtectHome=read-only` hardening.

## v3.9.1 — 2026-06-08 — Dashboard UI integration fixes

Pure UI fixes on top of v3.9.0. No backend behaviour changes.

- Topbar `tbStreamPill` is now driven by `checkStatus()` (live / offline / error).
- Sidebar footer (`sbFootAuth`, `sbFootStream`) shows real auth + stream state.
- `showPage('dashboard')` now also calls `refreshHealthPage()` so the dashboard
  health checklist is no longer stuck on "Loading".
- `renderHealthList()` reads `h.checks.{ffmpeg,v4l2loopback,disk}.ok` from
  `auth.system_health()` instead of the missing `h.ffmpeg` flag.
- Playlist drag/drop reorder now awaits `syncPlaylistToBackend()`.
- `static/css/dashboard.css` no longer contains literal `<style>` / `</style>`
  tags. Added topbar reset so generic legacy `header{}` styles do not affect
  `.dash-topbar`.
- Theme toggle uses `id="themeToggleBtn"` instead of
  `document.querySelector('.theme-btn')` (which previously mutated the
  snapshot/backup buttons).
- Logs page FFmpeg tab fetches `/api/logs/recent?n=100` (redacted) with a
  legacy fallback to `/api/stream/logs?n=60`.
- `applyStreamSettings()` now restores `rtmp_url_2`, watermark fields,
  `record`, `smart_shuffle`, `mute_audio`, and `extra_audio`.
- Keyboard shortcuts: Space = preview only; S = start (confirm); X = stop
  (confirm); R = restart (confirm); typing in inputs is ignored.
- All POST/PUT/DELETE still go through `apiFetch` (CSRF preserved).

## v3.9.0 — 2026-06-08 — Dashboard UI redesign (MVP)

Backend unchanged. UI restructured into a sidebar-driven dashboard.

### Frontend
- **New shell** (`templates/index.html`): left sidebar (Dashboard, Live Studio,
  Media Library, Playlist Builder, Profiles, Scheduler, Recordings, Backups,
  System Health, Settings, Logs), sticky topbar (page title + stream status
  pill + quick actions), section-based page switching. Collapsible on desktop,
  drawer on mobile. "Cyan on charcoal" control-room palette.
- **Code split**: inline `<style>` extracted to `static/css/dashboard.css`
  (445 lines), inline `<script>` extracted to `static/js/app.js` (1000+ lines).
  CSRF monkey-patch + `apiFetch` preserved verbatim in the page head.
- **New Dashboard page**: 8 metric tiles (status, playlist size, device,
  current clip, RTMP, recording, disk, version), quick-actions row, health
  checklist driven by `/api/health`.
- **New System Health page**: runtime info from `/api/app/info` (Python,
  FFmpeg, user, DB path, scan roots, auth/CSRF status, disk free).
- **Logs separated** into its own sidebar page; existing `#logArea` /
  `#ltApp` / `#ltFFmpeg` IDs preserved so legacy log JS keeps working.
- **Backups page**: dedicated export/import controls with overwrite warning.
- All existing card markup, element IDs, function names, and API endpoints
  preserved — every legacy handler (`startStream`, `scanFolder`,
  `addAllToPlaylist`, `saveProfile`, `addScheduleJob`, `exportBackup`,
  `importBackup`, `checkStatus`, `togglePreview`, …) still works unchanged.

### Backend (3 new read-only endpoints)
- `GET /api/health` — extended JSON snapshot (auth status, CSRF, v4l2,
  playlist size, disk free MB, disk warning). Used by dashboard checklist.
- `GET /api/app/info` — runtime info: version, Python, FFmpeg version, user,
  DB path, recordings path, scan roots, auth/CSRF flags, free disk.
  No secrets, no stream keys.
- `GET /api/logs/recent?n=100` — last N redacted ffmpeg log lines.

### Verification
- `python3 -m py_compile app.py auth.py state.py` → OK.
- `./venv/bin/pytest -q` → **29 passed in 0.85s** (no regressions).
- Smoke test via Flask test client: `/`, `/api/health`, `/api/app/info`,
  `/api/logs/recent`, `/api/version`, `/static/css/dashboard.css`,
  `/static/js/app.js` all return 200.

### Not in this pass (deferred to next release)
- Recordings page UI (endpoint already exists at `/api/recordings`).
- Settings page UI (env-var documented in README instead).
- Drag-and-drop reorder polish, keyboard shortcuts modal, first-run wizard.
- Confirmation modals beyond the ones already in legacy code.
- Splitting `app.js` into per-domain modules (`stream.js`, `media.js`, …).

## v3.8.3 — 2026-06-08


Final hardening pass (4 issues). No UI redesign, no stack changes.

- **app.py `backup_export` / `backup_import`:** now read from and restore
  into `state.DB_PATH` (honours `MARIO_DB_PATH`). Docker installs that point
  the DB at `/home/mario/mario_data/mario_state.db` now back up and restore
  the real database instead of an empty `~/mario_state.db`. Archive entry
  name remains `mario_state.db` for backward compatibility.
- **app.py `/api/media/preview`:** added strict extension allowlist
  (`_VIDEO_EXT_SET | _AUDIO_EXT_SET`). Unsupported types return
  `{success: false, error: "Unsupported preview file type"}` with HTTP 400
  instead of being served as `application/octet-stream`.
- **app.py `clean_playlist_items`:** safe parsing for `volume` (via `_f()`,
  clamped 0.0–4.0, default 1.0) and `muted` (accepts bool / int / string
  variants `1/true/yes/on`, anything else → False). Malicious imported
  playlist JSON like `{"volume":"evil"}` no longer raises `ValueError`.
- **tests/test_phase_fixes.py:** removed module-level `import app` so
  collection no longer touches the developer's real `~/mario_state.db`.
  Each test now receives a `mario_app` fixture that re-imports the module
  after `conftest.isolated_home` has overridden `HOME`/`MARIO_PASSWORD`
  /`MARIO_CSRF`. Added 3 new regression tests (safe-string volume,
  preview-extension reject, backup uses `state.DB_PATH`). **29/29 pass.**

## v3.8.2 — 2026-06-06



Post-audit hardening (7 issues). No UI redesign, no stack changes.

- **setup.sh:** install `python3-venv` so `./venv` creation works on fresh
  Ubuntu/Debian. Pre-create `~/mario_state.db` and `~/mario_history.json`
  (mode 600) so `systemd` with `ProtectHome=read-only` + `ReadWritePaths=`
  can open them on first start.
- **docker-compose.yml:** stop bind-mounting individual files
  (Docker auto-creates a *directory* with that name when the host file is
  missing, which then breaks SQLite). Use a single `~/mario_data` directory
  bind mount and point the app at it via new env vars `MARIO_DB_PATH` and
  `MARIO_HISTORY_FILE`. Header comment documents UID 1000 chown step.
- **state.py / app.py:** honor `MARIO_DB_PATH` / `MARIO_HISTORY_FILE` env
  vars; create parent dir if missing.
- **app.py `validate_stream_params`:** `record_dir` now must be the default
  `~/mario_recordings` OR pass `_path_in_allowlist()`. Rejects arbitrary
  host paths (was previously trusted blindly).
- **app.py audio chain (Phase 8):** switched `src_has_audio = any(...)` to
  `src_all_audio = all(...)`. The concat demuxer only exposes `[0:a]` when
  *every* input has audio, so the old logic crashed FFmpeg when the first
  clip was silent but later clips had audio. With external audio + mixed
  playlist we now use the external track alone instead of referencing
  a non-existent `[0:a]`.
- **app.py `clean_playlist_items()`:** new shared helper. Both
  `/api/stream/start` and `/api/playlists/load` now re-validate every item
  (object shape, path string, MARIO_SCAN_ROOTS allowlist, supported
  extension, file exists, sanitized muted/volume/info). Imported playlist
  JSON is no longer trusted.
- **templates/index.html `loadProfiles()`:** rebuilt profile cards via
  `document.createElement` + `textContent` + `addEventListener` + dataset.
  No more inline `onclick="applyProfile(${escJs(...)})"` — profile names
  from imported backups can no longer break out of attribute context.
- **tests:** added 5 regression tests (record_dir allowlist, default
  record_dir, playlist extension reject, playlist allowlist reject,
  playlist sanitization). 26/26 pass.

## v3.8.1 — 2026-06-06
### Critical fixes
- **Boot-guard now actually runs under gunicorn/Docker**: `_enforce_public_auth()` was *called* before its `def` in v3.8.0 — Python silently no-op'd the import-time check. Moved the function definition above the call site. Verified: `MARIO_HOST=0.0.0.0` + empty `MARIO_PASSWORD` exits 2; `MARIO_ALLOW_UNSAFE=1` overrides.
- **setup.sh always creates `./venv`** and installs requirements there. Matches `mario.service` (`/home/%i/mario-stream/venv/bin/gunicorn`). No more "global install if pip works, venv if not" inconsistency.
- **docker-compose volumes follow the non-root `mario` user**: mounts moved from `/root/...` to `/home/mario/...`; `HOME` and `MARIO_SCAN_ROOTS` exported to match. Persistent data now actually persists.
### Backend fixes
- **Phase 7 — external audio with silent videos**: `build_ffmpeg_cmd` now probes `info.has_audio` across the active playlist. If no clip has audio and `extra_audio` is set, the filter graph uses the external track alone (no `[0:a]` reference). If neither exists, output cleanly with `-an`.
- **Phase 8 — path hardening**: `extra_audio`, `watermark`, `bumper_path`, `/api/thumb`, and `/api/clip/silent-check` now all require `_path_in_allowlist()` AND a matching extension. Arbitrary local file reads via FFmpeg are blocked.
- **state.save_playlist / load_playlist** now persist `muted` + `volume` columns. `ALTER TABLE … ADD COLUMN` is auto-applied on first use → backward compatible with v3.7 / v3.8.0 DBs.
### Frontend fixes
- **Phase 4 — profile global_volume round-trip**: profiles save the multiplier (0..4, 1.0=100%); applying a profile now converts back to the slider's percent (0..200). Legacy profiles that stored percent are preserved via a `<=4` heuristic.
- **Phase 5 — per-clip volume/mute controls disabled** with a clear tooltip ("Per-clip volume/mute is not yet wired into FFmpeg — use the global volume control"). The backend `-f concat` path cannot apply per-clip gain. No more fake controls.
- **Phase 9 — profile XSS finalised**: profile-card name + meta now use `esc()`, `onclick=` handlers pass through `escJs()` (JSON-stringify), `<option value/text>` escaped.
### Tests
- `pytest -q` → 21 passed (same suite as v3.8.0; covered by `test_phase_fixes.py` + smoke + validation).

## v3.8.0 — 2026-06-06
### Frontend
- New explicit `apiFetch()` helper: auto JSON Content-Type for plain-object bodies, leaves FormData untouched, always same-origin. CSRF token still auto-attached by the v3.7 monkey-patch — apiFetch just adds ergonomics.
- Refactored every POST/PUT/DELETE call site to `apiFetch`: scan, thumb, playlist reorder, playlists save/load/delete, profiles save/delete, history clear, stream start/stop/skip, scheduler add/toggle/delete, backup import, clip silent-check.
### Playlist sync (Phase 2)
- `syncPlaylistToBackend()` is now async, returns success boolean, and logs failures.
- `addAllToPlaylist`, `addToPlaylist`, `removeFromPlaylist`, `clearPlaylist`, `shufflePlaylist`, `loadPlaylist` all await backend sync — eliminates "Playlist is empty" on Start Stream.
- `startStream()` re-syncs the playlist immediately before POST `/api/stream/start` and aborts on sync failure with a clear log.
# Changelog

All notable changes to Mario Camera Streamer. Format: [version] – date.

## [3.8.0] — 2026-06-06
### Security
- Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` denying camera/mic/geo/FLoC.
- Content-Security-Policy on HTML responses (`frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`).
- `pwa_manifest()` rebuilt as a plain JSON dict — removes Jinja-templating + dead `if False else` branch.
- Per-stream concat tempfile via `tempfile.mkstemp()` (was fixed `/tmp/mario_playlist.txt` — race / predictable path).

### Fixed
- **CSRF in the browser** — `index.html` now ships a `fetch` wrapper that primes `/api/csrf` and auto-attaches `X-CSRF-Token` on every same-origin POST/PUT/PATCH/DELETE. Fixes blanket `403 CSRF token missing` from the UI.
- **Scheduler runs under gunicorn** — `_ensure_scheduler()` moved to module import time (was only called inside `if __name__ == '__main__'`).
- `ffmpeg_process` pointer protected by `_ffmpeg_lock` across stop/skip/monitor/shutdown.
- `tests/conftest.py` no longer double-nests the import root; full suite (12 tests) now passes.

### Added
- `<link rel="manifest">` + `<meta name="theme-color">` + strict referrer meta on `index.html`.
- Concat tempfiles tracked in a bounded deque and cleaned up on graceful shutdown.

## [3.6.0] — 2026-05-17
### Added
- GitHub Actions CI (`.github/workflows/ci.yml`) — pytest matrix on Python 3.10/3.11/3.12 + Docker build job.
- PWA manifest (`/manifest.webmanifest`) — installable on mobile.
- `SHORTCUTS.md` keyboard shortcuts spec + wiring snippet.
- Proper `.gitignore` + `.dockerignore`.

### Changed
- Dockerfile: multi-stage style, non-root `mario` user (uid 1000, in `video`+`audio` groups), `tini` for zombie reaping, gunicorn entrypoint.

## [3.5.0] — 2026-05-17
### Added
- **Production WSGI** — `gunicorn_conf.py` (1 worker / N threads — required because streaming state is in-process). `mario.service` now boots gunicorn.
- **History → SQLite** — sessions stored in `mario_state.db` (table `history`, indexed by `started`, capped at 500 rows). Legacy `~/mario_history.json` auto-migrated on first boot and renamed to `.migrated`.
- **Test suite** — `tests/test_smoke.py` + `tests/test_validation.py` (isolated `$HOME` fixture, runs with `pytest project-files/tests`).
- `pytest` + `gunicorn` added to `requirements.txt`.

### Changed
- `_load_history` / `_save_history` are now thin compat shims over `state.history_*`.

## [3.4.0] — 2026-05-17
### Added
- Webhook notifications (Telegram + Discord) on stream `start` / `stop` / auto-restart `error`.
- Audio loudness normalization — `audio_normalize` + `audio_norm_i/tp/lra` params (EBU R128).
- Pre-roll bumper — `bumper_path` plays once before the playlist loop.
- Disk space in `/health` — `disk_free_mb`, `disk_warning` < 500MB, hard-fail < 100MB.

### Fixed
- SSE `current_index` clamp when the playlist shrinks mid-stream.

## [3.3.0] — 2026-05-17
### Added
- CSRF double-submit cookie (`GET /api/csrf` + `X-CSRF-Token` header).
- MJPEG singleton broadcaster (one ffmpeg → N subscribers, ref-counted).
- State locking for `current_index` / `stream_stats` / `ffmpeg_logs`.
- Exponential auto-restart backoff (2→60s, `MARIO_MAX_AUTO_RESTARTS`).
- Zip-bomb protected restore (size + file-count caps).
- Precise scheduler (minute-boundary wake-up).
- Weighted-random smart shuffle (by play count).
- Pi temperature + throttle in `/health` (`vcgencmd get_throttled`).
- `/api/recordings` + `/api/recordings/cleanup` (rotate by max_keep + max_age_days).

### Fixed
- Playlist reorder path validation against `MARIO_SCAN_ROOTS`.
- Profile name regex sanitization.
- Atomic history writes (`os.replace`).
- SSE backpressure cap (`MARIO_SSE_MAX`).

## [3.2.0]
### Added
- Display-Matrix rotation awareness (`get_video_info`).
- `auto_res_mode` + `target_orientation` (canonical canvas).
- Hardware encoders: `h264_v4l2m2m`, `h264_omx`, `h264_nvenc`, `h264_qsv`.
- `MARIO_SCAN_ROOTS` allow-list + path traversal guard.
- `MARIO_STRICT_ORIGIN` + `MARIO_ALLOWED_ORIGINS` origin check.
- Graceful shutdown handlers (`SIGTERM`/`SIGINT`/`atexit`).

### Fixed
- Constant-time password compare (`hmac.compare_digest`).
- Even-dimension enforcement (H.264 requirement).

## v3.9.8 — Normalize Before Live (UI toggle)
- Added "Normalize Before Live" toggle in Stream Settings (default ON).
- Badge shows ON (green) / OFF (dim) reflecting current state.
- Wired through getStreamSettings/applyStreamSettings → backend `normalize_before_live` param.
- Backend default remains ON; toggle persists in stream profiles.
