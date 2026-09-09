"""Optional Basic-Auth + simple in-memory rate limiter + extended health."""
import os, time, shutil, subprocess, hmac
from collections import deque, defaultdict
from functools import wraps
from flask import request, Response, jsonify

# ── Auth ────────────────────────────────────────────────────────────
# Read env vars at call time so credentials can be rotated without restart
# and tests can patch os.environ.
def _creds():
    return (os.environ.get('MARIO_USER', 'mario'),
            (os.environ.get('MARIO_PASSWORD', '') or '').strip())

def _check(u, p):
    user, pw = _creds()
    if not pw:
        return False
    # constant-time comparison to prevent timing attacks
    try:
        return (hmac.compare_digest(str(u or ''), user) and
                hmac.compare_digest(str(p or ''), pw))
    except Exception:
        return False

def auth_required(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if not auth_enabled():
            return fn(*a, **kw)
        auth = request.authorization
        if not auth or not _check(auth.username, auth.password):
            return Response(
                'Login required', 401,
                {'WWW-Authenticate': 'Basic realm="Mario"'}
            )
        return fn(*a, **kw)
    return wrap

def auth_enabled():
    return bool(_creds()[1])


# ── Rate limiter (token bucket per IP+endpoint) ─────────────────────
_buckets = defaultdict(lambda: deque(maxlen=200))

def rate_limit(per_minute=60):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            now = time.time()
            ip  = request.headers.get('X-Forwarded-For', request.remote_addr or '?')
            key = f'{ip}:{request.endpoint}'
            q   = _buckets[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= per_minute:
                return jsonify({'error': 'Rate limit exceeded'}), 429
            q.append(now)
            return fn(*a, **kw)
        return wrap
    return deco


# ── Extended health ─────────────────────────────────────────────────
def system_health():
    out = {'ok': True, 'checks': {}}

    def add(name, ok, detail=''):
        out['checks'][name] = {'ok': bool(ok), 'detail': detail}
        if not ok:
            out['ok'] = False

    # FFmpeg
    try:
        r = subprocess.run(['ffmpeg','-version'], capture_output=True,
                           text=True, timeout=3)
        ok = r.returncode == 0
        ver = r.stdout.split('\n', 1)[0] if ok else r.stderr[:120]
        add('ffmpeg', ok, ver)
    except Exception as e:
        add('ffmpeg', False, str(e))

    # v4l2loopback — fast path: check /sys/module instead of forking lsmod
    try:
        loaded = os.path.isdir('/sys/module/v4l2loopback')
        add('v4l2loopback', loaded,
            'kernel module loaded' if loaded else 'module not loaded')
    except Exception as e:
        add('v4l2loopback', False, str(e))

    # PulseAudio / PipeWire
    try:
        r = subprocess.run(['pactl','info'], capture_output=True,
                           text=True, timeout=3)
        add('pulse', r.returncode == 0,
            'reachable' if r.returncode == 0 else r.stderr[:120])
    except Exception as e:
        add('pulse', False, str(e))

    # Disk space (>= 500 MB free in $HOME)
    try:
        s = shutil.disk_usage(os.path.expanduser('~'))
        free_mb = s.free // (1024*1024)
        add('disk', free_mb > 500, f'{free_mb} MB free')
    except Exception as e:
        add('disk', False, str(e))

    # CPU load (best-effort)
    try:
        load = os.getloadavg()
        cores = os.cpu_count() or 1
        add('load', load[0] < cores * 2,
            f'1m={load[0]:.2f}, cores={cores}')
    except Exception as e:
        add('load', True, f'unavailable ({e})')

    # Raspberry Pi / Linux CPU temperature + throttle state. Useful since
    # the Pi will silently throttle ffmpeg under thermal load and the user
    # sees "dropped frames" without knowing why.
    try:
        temp_c = None
        for p in ('/sys/class/thermal/thermal_zone0/temp',):
            if os.path.exists(p):
                with open(p) as fh:
                    temp_c = int(fh.read().strip()) / 1000.0
                break
        if temp_c is not None:
            add('cpu_temp', temp_c < 80.0, f'{temp_c:.1f}°C')
    except Exception:
        pass

    try:
        if os.path.exists('/usr/bin/vcgencmd'):
            r = subprocess.run(['vcgencmd', 'get_throttled'],
                               capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                # throttled=0x0 = healthy; any non-zero = under-voltage / throttle
                val = r.stdout.strip().split('=')[-1]
                add('pi_throttle', val == '0x0', val)
    except Exception:
        pass

    return out
