# Mario Camera Streamer — gunicorn config (v3.9.11)
#
# IMPORTANT: Mario keeps streaming state (ffmpeg process, playlist index,
# MJPEG broadcaster, SSE subscribers) in *in-process* globals guarded by
# threading locks. Running multiple workers would split that state across
# processes — different workers would see different streaming/current_index
# values and fight over /dev/video10.
#
# Therefore: workers MUST stay = 1. Concurrency comes from threads.
#
# v3.9.11 — fix "Read-only file system: '/home/<user>/.gunicorn'":
#   Under systemd with ProtectHome=read-only, gunicorn cannot create its
#   control/heartbeat files under $HOME. Force them into a writable dir
#   (/tmp by default, override with MARIO_GUNICORN_TMP).
import os

bind         = f"{os.environ.get('MARIO_HOST','127.0.0.1')}:{os.environ.get('MARIO_PORT','5000')}"
workers      = 1                                # see note above
threads      = int(os.environ.get('MARIO_THREADS', '16'))
worker_class = 'gthread'
timeout      = int(os.environ.get('MARIO_TIMEOUT', '120'))   # SSE keeps conns open
keepalive    = 30
graceful_timeout = 10
accesslog    = '-'   # stdout
errorlog     = '-'
loglevel     = os.environ.get('MARIO_LOG_LEVEL', 'info').lower()
proc_name    = 'mario-stream'

# Writable temp/heartbeat dir (avoids $HOME under ProtectHome=read-only).
_tmp = os.environ.get('MARIO_GUNICORN_TMP', '/tmp/mario_gunicorn')
try:
    os.makedirs(_tmp, exist_ok=True)
except Exception:
    _tmp = '/tmp'
worker_tmp_dir  = _tmp
tmp_upload_dir  = _tmp
