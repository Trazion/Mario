# Mario Camera Streamer

## Run
```bash
source venv/bin/activate
python app.py
```

Default: binds to **127.0.0.1:5000**, debug **off**.

## Environment variables
| Variable          | Default     | Notes                                     |
|-------------------|-------------|-------------------------------------------|
| `MARIO_HOST`      | `127.0.0.1` | Set `0.0.0.0` for LAN access              |
| `MARIO_PORT`      | `5000`      |                                           |
| `MARIO_DEBUG`     | `0`         | `1` = Flask debug (dev only)              |
| `MARIO_USER`      | `mario`     | Basic-auth username                       |
| `MARIO_PASSWORD`  | *(empty)*   | Set to enable HTTP Basic Auth on all APIs |
| `MARIO_GITHUB_REPO`        | *(empty)* | `owner/repo` → enables `/api/version/check` |
| `MARIO_ALLOW_AUTO_UPDATE`  | `0`       | `1` → enables `POST /api/version/apply` (git ff-only, refuses dirty tree, rate-limited 2/min) |
| `MARIO_SCAN_ROOTS`         | *(common media dirs)* | Colon-separated allowlist for `/api/scan` + `/api/playlist/add`. Empty = legacy unrestricted |
| `MARIO_STRICT_ORIGIN`      | `0`       | `1` → reject POST/PUT/DELETE from cross-origin browsers (CSRF protection) |
| `MARIO_ALLOWED_ORIGINS`    | *(empty)* | Comma-separated extra origins allowed when `MARIO_STRICT_ORIGIN=1` |
| `MARIO_HW_ENCODER`         | `libx264` | Default video encoder. `h264_v4l2m2m`/`h264_omx` (Pi), `h264_nvenc` (NVIDIA), `h264_qsv` (Intel), `h264_vaapi` |
| `MARIO_CSRF`               | `1`       | `0` disables CSRF double-submit cookie (headless / CLI setups) |
| `MARIO_MAX_AUTO_RESTARTS`  | `10`      | Consecutive crash cap before auto-restart gives up |
| `MARIO_SSE_MAX`            | `20`      | Max concurrent SSE subscribers |
| `MARIO_MJPEG_MAX`          | `10`      | Max concurrent MJPEG preview subscribers (shared ffmpeg) |
| `MARIO_MAX_UPLOAD_MB`      | `16`      | Multipart upload body cap |
| `MARIO_BACKUP_MAX_MB`      | `50`      | Zip-bomb guard: max uncompressed restore size |
| `MARIO_BACKUP_MAX_FILES`   | `500`     | Zip-bomb guard: max files in a backup zip |
| `MARIO_LOG_LEVEL`          | `INFO`    | `DEBUG` / `INFO` / `WARNING` / `ERROR` (structured stdout logs) |
| `MARIO_TELEGRAM_TOKEN`     | _(empty)_ | Telegram bot token for stream notifications |
| `MARIO_TELEGRAM_CHAT_ID`   | _(empty)_ | Telegram chat/channel ID to notify |
| `MARIO_DISCORD_WEBHOOK`    | _(empty)_ | Discord webhook URL for stream notifications |
| `MARIO_WEBHOOK_EVENTS`     | `start,stop,error` | Which events fire webhooks |

## v3.7 changelog
- **Security headers everywhere** — `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, plus a Content-Security-Policy on HTML responses (`frame-ancestors 'none'`)
- **CSRF fixed in the browser** — `index.html` now ships a `fetch` wrapper that primes `/api/csrf` once and auto-attaches `X-CSRF-Token` on every same-origin POST/PUT/PATCH/DELETE. No more `403 CSRF token missing` from the UI
- **Scheduler runs under gunicorn** — `_ensure_scheduler()` moved to module import (was only called inside `if __name__ == '__main__'`, so scheduled jobs never fired in production)
- **PWA manifest cleanup** — `pwa_manifest()` returns a JSON dict directly (no Jinja templating, no `if False else` dead branch). `<link rel="manifest">` + `<meta name="theme-color">` added to `index.html`
- **`ffmpeg_process` lock** — `_ffmpeg_lock` protects the process pointer across `stop`/`skip`/`monitor`/`shutdown`
- **Per-stream concat tempfile** — `tempfile.mkstemp()` replaces the fixed `/tmp/mario_playlist.txt` (race-free, 0600, auto-cleaned on shutdown)
- **Test fixture path fix** — `tests/conftest.py` was double-nesting `project-files/project-files` so the suite couldn't import `app`. 12/12 tests now pass

## v3.6 changelog
- **CI workflow** — `.github/workflows/ci.yml` runs pytest on Python 3.10/3.11/3.12 + builds Docker image
- **PWA manifest** — `/manifest.webmanifest` for install-on-mobile
- **Dockerfile hardening** — non-root `mario` user, `tini` for zombie reaping, boots gunicorn
- **Keyboard shortcuts spec** — `SHORTCUTS.md`
- **Proper `.gitignore` + `.dockerignore`** — exclude runtime data, caches, venvs

## v3.5 changelog
- **Production WSGI** — `gunicorn_conf.py` (workers=1, threads=16). `mario.service` boots via gunicorn now. Run manually: `gunicorn -c gunicorn_conf.py app:app`
- **History → SQLite** — `mario_state.db` table `history` (capped 500 rows). `~/mario_history.json` auto-migrated once and renamed `.migrated`
- **Test suite** — `pytest project-files/tests` (smoke + validation, isolated `$HOME`)

## v3.4 changelog
- **Webhook notifications** — Telegram + Discord on stream `start` / `stop` / auto-restart `error` (fire-and-forget, non-blocking)
- **Audio loudness normalization** — `audio_normalize` + `audio_norm_i/tp/lra` params (EBU R128 single-pass loudnorm)
- **Pre-roll bumper** — `bumper_path` plays once before the playlist loop (skipped on auto-restart resume)
- **Disk space in `/health`** — `disk_free_mb` + `disk_warning` (< 500MB) + hard-fail < 100MB
- **SSE current_index clamp** — fixes stale index when playlist shrinks mid-stream

## v3.3 changelog
- **CSRF double-submit cookie** — `GET /api/csrf` then echo `X-CSRF-Token` header on mutating requests. CLI escape hatch: `X-Mario-Cli: 1` + Basic-Auth.
- **MJPEG singleton broadcaster** — one ffmpeg reads `/dev/video10`, fan-out to N subscribers; ref-counted teardown
- **State locking** — `current_index` / `stream_stats` / `ffmpeg_logs` now race-free; SSE uses monotonic log sequence instead of deque indices
- **Auto-restart backoff** — exponential 2→60s with `MARIO_MAX_AUTO_RESTARTS` cap (prevents crash-loops)
- **Zip-bomb protected restore** — pre-flight uncompressed-size + file-count caps; stops stream before overwriting `mario_state.db`; atomic temp-rename writes
- **Precise scheduler** — sleeps to the next minute boundary (was `time.sleep(30)` which could miss minutes)
- **Real smart-shuffle** — weighted-random by play_count (was deterministic sort with random tiebreak)
- **Playlist reorder validation** — submitted paths must already be in playlist or in `MARIO_SCAN_ROOTS`
- **Profile name sanitization** — same regex as playlists (path traversal block)
- **History file** — locked + atomic rename writes
- **Snapshot cache** — thread-safe
- **`/api/recordings` + `/api/recordings/cleanup`** — list + rotate `~/mario_recordings` (max_keep, max_age_days)
- **Pi temperature + throttle in `/health`** — `cpu_temp` + `pi_throttle` checks (`vcgencmd get_throttled`)
- **Body size cap** — `MAX_CONTENT_LENGTH = MARIO_MAX_UPLOAD_MB`
- **`GIT_TERMINAL_PROMPT=0`** — `/api/version/apply` fails fast instead of hanging on credential prompt
- **SSE backpressure** — `MARIO_SSE_MAX` clients, generator unregisters on disconnect
- **Structured logging** — stdout JSON-ish format, `MARIO_LOG_LEVEL`
- **Device existence check** — `start_stream` / preview / snapshot verify `/dev/videoN` exists before launching

## v3.2 changelog
- **Zoom artifact fix** — auto-resolution now picks a coherent target (first clip / dominant orientation) and respects rotation metadata; new `auto_res_mode` and `target_orientation` params
- **Hardware encoding** — `hw_encoder` param: `libx264` (default) · `h264_v4l2m2m`/`h264_omx` (Pi, ~5-10× less CPU) · `h264_nvenc` · `h264_qsv`
- **Security hardening** — constant-time password comparison (`hmac.compare_digest`), CSRF origin-check gate (`MARIO_STRICT_ORIGIN`), path allowlist for scan/add (`MARIO_SCAN_ROOTS`), playlist name sanitization
- **Graceful shutdown** — SIGTERM/SIGINT/atexit terminate ffmpeg children → no zombies holding `/dev/video10`
- **Fast v4l2 probe** — reads `/sys/module/v4l2loopback` instead of forking `lsmod` on every status poll

## v3.1 changelog
- **Loop modes** — `loop_mode`: `all` | `once` | `one` | `shuffle`
- **Auto-quality** — `auto_quality: true` picks bitrate from output resolution
- **Prometheus metrics** at `/metrics` (streaming, fps, frames, dropped, restarts)
- **Backup / Restore** — `GET /api/backup/export` (zip) · `POST /api/backup/import`
- **Version checker** — `/api/version` · `/api/version/check` (set `MARIO_GITHUB_REPO`) · `POST /api/version/apply` (opt-in safe self-update, set `MARIO_ALLOW_AUTO_UPDATE=1`)
- **Stream snapshot** — `/api/stream/snapshot.jpg?device=/dev/video10` (5s cache)
- **Silent-gap detection** — `POST /api/clip/silent-check {path, noise, min}`
- **Docker** — `docker compose up -d` (see `Dockerfile`, `docker-compose.yml`)
- **systemd** — `sudo systemctl enable --now mario@$USER.service` (see `mario.service`)

## v3 changelog
- **SSE** push-based stats + logs (`/api/stream/events`) — replaces 1-Hz polling
- **Mobile-friendly UI** — responsive at ≤960 px and ≤560 px
- **Hotkeys** — Space play/pause · N next clip · S stop · F preview · R restart · T theme
- **Skip** endpoint `/api/stream/skip` + ⏭ Next-clip button
- **Watermark** PNG overlay with position/opacity/scale
- **Recording** to MP4 in `~/mario_recordings`
- **Multi-RTMP** — broadcast to two RTMP destinations via `tee`
- **Basic Auth** + per-IP rate limiting on hot endpoints
- **Extended `/health`** — checks ffmpeg, v4l2loopback, pulse, disk, load
- **Auto resolution** = smallest common w×h across the playlist (was: first clip)
- **Smart shuffle** — favors least-played clips, no immediate repeats
- **Per-clip analytics** — play count + total seconds in `clip_stats` table
  (`GET /api/analytics`, `POST /api/analytics/reset`)

## Files
- `app.py` — Flask + ffmpeg orchestration
- `state.py` — SQLite persistence (playlist, jobs, analytics)
- `auth.py` — Basic Auth, rate limiter, extended health
- `templates/index.html` — UI

---

## v3.9.2 — Autostart, Server Controls, Dark Theme Lock

### Keeping Mario Stream alive after a VPS reboot

#### systemd (recommended for VPS / bare metal)

The unit ships as a template — use the `@user` instance form:

```bash
sudo cp mario.service /etc/systemd/system/mario@.service
sudo systemctl daemon-reload
sudo systemctl enable --now mario@$USER.service     # autostart on reboot
sudo systemctl status      mario@$USER.service
sudo journalctl -u         mario@$USER.service -f
sudo systemctl restart     mario@$USER.service
```

System Health → **Server Controls** shows whether autostart is enabled.

#### Docker

`docker-compose.yml` sets `restart: unless-stopped`, so the container
auto-restarts on crash and on host reboot (once Docker itself starts):

```bash
docker compose up -d
docker compose ps
docker compose logs -f
docker compose restart           # restart from the host
```

### Dashboard restart controls (optional)

The **Server Controls** panel on the System Health page exposes:

- **Restart App** — calls `POST /api/system/restart-app`, which runs
  `sudo -n systemctl restart <unit>`. Disabled in the UI unless the unit is
  detected AND the probe succeeds.
- **Reboot Server** — calls `POST /api/system/reboot-server`. Disabled
  unless `MARIO_ALLOW_SERVER_REBOOT=1`. Requires typing `REBOOT` (also
  enforced server-side).

Both endpoints are POST-only, require auth + CSRF, run a fixed command
(no shell interpolation, no user input), and respond with JSON before the
restart is scheduled in a background thread.

To allow the Restart App button to actually work, grant a **narrow**
NOPASSWD sudoers rule:

```bash
sudo visudo -f /etc/sudoers.d/mario-stream
```

Add (replace `YOUR_USER` with the user running the unit):

```
YOUR_USER ALL=NOPASSWD: /bin/systemctl restart mario@YOUR_USER.service
```

For the (dangerous) reboot button, additionally add:

```
YOUR_USER ALL=NOPASSWD: /sbin/reboot
```

…and export `MARIO_ALLOW_SERVER_REBOOT=1` in `~/.mario_env`.

> **Never** use `NOPASSWD: ALL`. Only whitelist the exact two commands
> above. The reboot rule is strictly optional.

### Environment variables added in v3.9.2

| Variable | Purpose |
| --- | --- |
| `MARIO_SYSTEMD_UNIT` | Override autodetected unit name (default `mario@<user>.service`). |
| `MARIO_SYSTEMD_USER` | Override user portion when autodetection picks the wrong account. |
| `MARIO_ALLOW_SERVER_REBOOT` | Set to `1` to enable the dangerous **Reboot Server** button. |

### Theme

v3.9.2 locks the UI to dark mode. The previous light palette caused
white-on-white rendering in several panels; light mode will return in a
later release once every component is re-audited.
