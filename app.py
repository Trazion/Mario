from flask import (Flask, render_template, jsonify, request, Response,
                   stream_with_context, make_response, session, redirect, url_for)
import subprocess, os, json, threading, glob, time, re, random, base64, sqlite3
import hmac, hashlib, signal, atexit, secrets, logging, sys, tempfile
from collections import deque, defaultdict
from pathlib import Path
from urllib.parse import urlparse
import state, auth

# Structured logging to stdout (works with journalctl/docker logs)
logging.basicConfig(
    level=os.environ.get('MARIO_LOG_LEVEL', 'INFO'),
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('mario')

app = Flask(__name__)
# Cap upload body size (16 MB) — protects /api/backup/import + others from
# memory-exhaustion DoS. Override with MARIO_MAX_UPLOAD_MB.
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MARIO_MAX_UPLOAD_MB', '16')) * 1024 * 1024

# ── Flask session / secret key (persisted so cookies survive restarts) ───────
def _load_or_create_secret():
    env = os.environ.get('MARIO_SECRET_KEY', '').strip()
    if env:
        return env
    path = os.environ.get('MARIO_SECRET_FILE') or os.path.expanduser('~/.mario_secret')
    try:
        if os.path.isfile(path):
            with open(path, 'r') as fh:
                v = fh.read().strip()
                if v: return v
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        v = secrets.token_urlsafe(48)
        with open(path, 'w') as fh: fh.write(v)
        try: os.chmod(path, 0o600)
        except Exception: pass
        return v
    except Exception:
        # Fallback: ephemeral key (sessions reset on restart)
        return secrets.token_urlsafe(48)

app.secret_key = _load_or_create_secret()
app.config.update(
    SESSION_COOKIE_NAME='mario_session',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=int(os.environ.get('MARIO_SESSION_HOURS', '12')) * 3600,
)

# ── Input validation ─────────────────────────────────────────────────────────
_RES_RE     = re.compile(r'^\d{2,5}x\d{2,5}$')
_HHMM_RE    = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_DEVICE_RE  = re.compile(r'^/dev/video\d{1,3}$')
_RTMP_RE    = re.compile(r'^rtmps?://[\w\.\-:/%@?=&]+$', re.I)
_PRESETS    = {'ultrafast','superfast','veryfast','faster','fast',
               'medium','slow','slower','veryslow'}
_OVERLAY_POS= {'tl','tr','bl','br'}
_HW_ENCODERS= {'libx264','h264_v4l2m2m','h264_omx','h264_vaapi','h264_nvenc','h264_qsv'}
_AUTO_RES_MODES = {'first','common','min'}
_ORIENTATIONS   = {'auto','landscape','portrait','square'}

# ── Path allowlist for scan / add (security: prevent arbitrary FS access) ────
def _expand_roots(spec):
    roots = []
    for p in (spec or '').split(':'):
        p = p.strip()
        if not p: continue
        try:
            roots.append(os.path.realpath(os.path.expanduser(p)))
        except Exception:
            pass
    return roots

SCAN_ROOTS = _expand_roots(os.environ.get(
    'MARIO_SCAN_ROOTS',
    '~/Videos:~/Music:~/mario_playlists:~/mario_recordings:~/mario_watermarks:/mnt:/media'
))

def _path_in_allowlist(path):
    """True if realpath(path) is inside any configured SCAN_ROOTS dir.
    When SCAN_ROOTS is empty (operator opted out), allow everything (legacy)."""
    if not SCAN_ROOTS:
        return True
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return any(rp == r or rp.startswith(r + os.sep) for r in SCAN_ROOTS)

_VIDEO_EXT_SET = {'.mp4','.avi','.mkv','.mov','.wmv','.flv','.webm','.m4v'}
_AUDIO_EXT_SET = {'.mp3','.wav','.aac','.flac','.ogg','.m4a','.opus'}
_MEDIA_EXT_SET = _VIDEO_EXT_SET | _AUDIO_EXT_SET | {'.png','.jpg','.jpeg','.webp'}

class ValidationError(ValueError): pass

def _f(v, lo, hi, default):
    try: x = float(v)
    except (TypeError, ValueError): return default
    return max(lo, min(hi, x))

def _i(v, lo, hi, default):
    try: x = int(v)
    except (TypeError, ValueError): return default
    return max(lo, min(hi, x))

def validate_stream_params(p):
    """Sanitize untrusted frontend params before they reach FFmpeg.
    Raises ValidationError on hard failures; clamps numeric ranges silently."""
    if not isinstance(p, dict):
        raise ValidationError('params must be an object')
    out = {}
    dev = str(p.get('device','/dev/video10'))
    if not _DEVICE_RE.match(dev):
        raise ValidationError(f'invalid device: {dev}')
    out['device'] = dev
    res = str(p.get('resolution','auto'))
    if res != 'auto' and not _RES_RE.match(res):
        raise ValidationError(f'invalid resolution: {res}')
    out['resolution'] = res
    out['fps']         = _i(p.get('fps',30), 1, 120, 30)
    out['buffer_size'] = _i(p.get('buffer_size',0), 0, 100000, 0)
    out['global_volume']= _f(p.get('global_volume',1.0), 0.0, 4.0, 1.0)
    out['mute_audio']  = bool(p.get('mute_audio', False))
    out['loop']        = bool(p.get('loop', True))
    out['auto_restart']= bool(p.get('auto_restart', False))
    out['shuffle']     = bool(p.get('shuffle', False))
    # Loop mode: 'all' (default) | 'once' | 'one' | 'shuffle'
    lm = str(p.get('loop_mode', 'all')).lower()
    if lm not in ('all','once','one','shuffle'):
        lm = 'all'
    out['loop_mode']   = lm
    if   lm == 'once':    out['loop'] = False
    elif lm == 'all':     out['loop'] = True
    elif lm == 'shuffle': out['loop'] = True; out['shuffle'] = True
    elif lm == 'one':     out['loop'] = True  # handled by single-clip stream_loop
    # Quality auto-adjust (dynamic bitrate based on resolution)
    out['auto_quality']= bool(p.get('auto_quality', False))
    preset = str(p.get('preset','ultrafast'))
    if preset not in _PRESETS:
        raise ValidationError(f'invalid preset: {preset}')
    out['preset'] = preset
    pos = str(p.get('overlay_pos','tl'))
    if pos not in _OVERLAY_POS:
        raise ValidationError(f'invalid overlay_pos: {pos}')
    out['overlay_pos'] = pos
    txt = str(p.get('text_overlay','') or '')[:200]
    # strip control chars + characters that break drawtext escaping
    txt = re.sub(r'[\x00-\x1f\x7f]', '', txt)
    out['text_overlay'] = txt
    rtmp = str(p.get('rtmp_url','') or '').strip()
    if rtmp and not _RTMP_RE.match(rtmp):
        raise ValidationError('invalid rtmp_url')
    out['rtmp_url'] = rtmp
    extra = p.get('extra_audio') or None
    if extra:
        extra = str(extra)
        if not _path_in_allowlist(extra):
            raise ValidationError('extra_audio not in MARIO_SCAN_ROOTS allowlist')
        if os.path.splitext(extra)[1].lower() not in (_AUDIO_EXT_SET | _VIDEO_EXT_SET):
            raise ValidationError('extra_audio: unsupported extension')
        if not os.path.isfile(extra):
            raise ValidationError('extra_audio file not found')
    out['extra_audio'] = extra
    out['vf_brightness']= _f(p.get('vf_brightness',0.0), -1.0, 1.0, 0.0)
    out['vf_contrast']  = _f(p.get('vf_contrast',  1.0),  0.0, 2.0, 1.0)
    out['vf_saturation']= _f(p.get('vf_saturation',1.0),  0.0, 3.0, 1.0)
    out['vf_grayscale'] = bool(p.get('vf_grayscale', False))

    # Watermark
    wm = p.get('watermark') or None
    if wm:
        wm = str(wm)
        if not _path_in_allowlist(wm):
            raise ValidationError('watermark not in MARIO_SCAN_ROOTS allowlist')
        if os.path.splitext(wm)[1].lower() not in {'.png','.jpg','.jpeg','.webp','.gif','.bmp'}:
            raise ValidationError('watermark: unsupported extension')
        if not os.path.isfile(wm):
            raise ValidationError('watermark file not found')
    out['watermark']     = wm
    out['watermark_pos'] = str(p.get('watermark_pos','br')) if str(p.get('watermark_pos','br')) in _OVERLAY_POS else 'br'
    out['watermark_opacity'] = _f(p.get('watermark_opacity',0.7), 0.0, 1.0, 0.7)
    out['watermark_scale']   = _f(p.get('watermark_scale',  0.15), 0.02, 0.5, 0.15)

    # Recording
    out['record']      = bool(p.get('record', False))
    default_rec_dir = os.path.expanduser('~/mario_recordings')
    rec_dir = str(p.get('record_dir', default_rec_dir) or default_rec_dir)
    # Phase 8: prevent writing recordings to arbitrary host paths.
    # Allow the default OR any path that is inside MARIO_SCAN_ROOTS.
    try:
        rec_real = os.path.realpath(rec_dir)
    except Exception:
        raise ValidationError('invalid record_dir')
    if rec_real != os.path.realpath(default_rec_dir) and not _path_in_allowlist(rec_real):
        raise ValidationError('record_dir not in MARIO_SCAN_ROOTS allowlist')
    out['record_dir']  = rec_real

    # Smart shuffle (no-immediate-repeat)
    out['smart_shuffle'] = bool(p.get('smart_shuffle', False))

    # Secondary RTMP (for multi-output)
    rtmp2 = str(p.get('rtmp_url_2','') or '').strip()
    if rtmp2 and not _RTMP_RE.match(rtmp2):
        raise ValidationError('invalid rtmp_url_2')
    out['rtmp_url_2'] = rtmp2

    # Hardware encoder (v3.2). 'libx264' = software default (always safe).
    hw = str(p.get('hw_encoder', 'libx264'))
    if hw not in _HW_ENCODERS:
        hw = 'libx264'
    out['hw_encoder'] = hw

    # Auto-resolution mode: 'first' (default, fixes zoom) | 'common' | 'min' (legacy)
    arm = str(p.get('auto_res_mode', 'first')).lower()
    if arm not in _AUTO_RES_MODES:
        arm = 'first'
    out['auto_res_mode'] = arm

    # Target orientation override (forces landscape/portrait/square output).
    orient = str(p.get('target_orientation', 'auto')).lower()
    if orient not in _ORIENTATIONS:
        orient = 'auto'
    out['target_orientation'] = orient

    # v3.4: audio loudness normalization (EBU R128 loudnorm) — single-pass.
    # Off by default; when on, applies after volume but before aresample.
    out['audio_normalize'] = bool(p.get('audio_normalize', False))
    out['audio_norm_i']    = _f(p.get('audio_norm_i',  -16.0), -70.0, -5.0, -16.0)
    out['audio_norm_tp']   = _f(p.get('audio_norm_tp',  -1.5), -9.0,   0.0,  -1.5)
    out['audio_norm_lra']  = _f(p.get('audio_norm_lra', 11.0),  1.0,  20.0,  11.0)

    # v3.4: optional pre-roll bumper (intro clip) prepended to playlist.
    bumper = p.get('bumper_path') or None
    if bumper:
        bumper = str(bumper)
        if not _path_in_allowlist(bumper):
            raise ValidationError('bumper_path not in MARIO_SCAN_ROOTS allowlist')
        if os.path.splitext(bumper)[1].lower() not in _VIDEO_EXT_SET:
            raise ValidationError('bumper_path: unsupported extension')
        if not os.path.isfile(bumper):
            raise ValidationError('bumper_path file not found')
    out['bumper_path'] = bumper

    # v3.9.8: robust live stability. Default ON: pre-encode every source
    # into a fixed canvas/fps/SAR/DAR/pixel-format cache before live FFmpeg
    # sees it. This prevents mid-file filter reinitialization zooms.
    out['normalize_before_live'] = bool(p.get('normalize_before_live', True))

    # v3.9.4: video fit mode — controls scale/pad behavior.
    # 'fit'     → letterbox (decrease + pad). Default. NEVER crops, NEVER zooms.
    # 'fill'    → cover/crop (increase + crop). May crop edges of source.
    # 'stretch' → ignore aspect ratio (disable). Distorts video — not recommended.
    fm = str(p.get('fit_mode', 'fit')).lower()
    if fm not in ('fit', 'fill', 'stretch'):
        fm = 'fit'
    out['fit_mode'] = fm

    return out

def clean_playlist_items(raw_items, require_exists=True):
    """Validate + sanitize a list of playlist items from untrusted JSON.
    Shared by /api/stream/start and /api/playlists/load so both endpoints
    enforce the same allowlist / extension / type rules.

    Raises ValidationError on the first bad item.
    """
    if not isinstance(raw_items, list):
        raise ValidationError('playlist must be a list')
    cleaned = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValidationError('each playlist item must be an object')
        path = raw.get('path')
        if not isinstance(path, str) or not path.strip():
            raise ValidationError('playlist item missing path')
        if not _path_in_allowlist(path):
            raise ValidationError(
                f'path not in MARIO_SCAN_ROOTS allowlist: {os.path.basename(path)}')
        if os.path.splitext(path)[1].lower() not in _MEDIA_EXT_SET:
            raise ValidationError(
                f'unsupported extension: {os.path.basename(path)}')
        if require_exists and not os.path.isfile(path):
            raise ValidationError(f'file not found: {os.path.basename(path)}')
        info = raw.get('info') if isinstance(raw.get('info'), dict) else {}
        # v3.8.3: safe parsing — never let untrusted JSON raise ValueError
        raw_muted = raw.get('muted', False)
        if isinstance(raw_muted, bool):
            muted = raw_muted
        elif isinstance(raw_muted, (int, float)):
            muted = bool(raw_muted)
        elif isinstance(raw_muted, str):
            muted = raw_muted.strip().lower() in ('1','true','yes','on')
        else:
            muted = False
        # v3.9.16: per-clip start offset (seconds). Sanitize → float, clamp
        # to [0, duration-1] when duration is known, else just to >= 0.
        try:
            sofs = float(raw.get('start_offset_seconds', 0) or 0)
        except (TypeError, ValueError):
            sofs = 0.0
        if sofs < 0 or sofs != sofs:  # NaN guard
            sofs = 0.0
        dur = 0.0
        try:
            dur = float(info.get('duration') or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur > 0:
            sofs = min(sofs, max(0.0, dur - 1.0))
        cleaned.append({
            'path':   path,
            'name':   str(raw.get('name') or os.path.basename(path))[:255],
            'info':   info,
            'muted':  muted,
            'volume': _f(raw.get('volume', 1.0), 0.0, 4.0, 1.0),
            'start_offset_seconds': round(sofs, 3),
        })
    return cleaned


def validate_job(d):
    if not isinstance(d, dict):
        raise ValidationError('job must be an object')
    name = str(d.get('name','')).strip()[:80] or f'Job {int(time.time())}'
    t = str(d.get('time','08:00'))
    if not _HHMM_RE.match(t):
        raise ValidationError('invalid time (HH:MM)')
    days = d.get('days',[0,1,2,3,4,5,6])
    if not isinstance(days, list) or not all(isinstance(x,int) and 0<=x<=6 for x in days):
        raise ValidationError('invalid days')
    return {'name': name, 'time': t, 'days': sorted(set(days)),
            'params': validate_stream_params(d.get('params', {}))}

# ── Directories ───────────────────────────────────────────────────────────────
PLAYLISTS_DIR  = os.path.expanduser('~/mario_playlists')
THUMBS_DIR     = os.path.expanduser('~/mario_thumbs')
PROFILES_DIR   = os.path.expanduser('~/mario_profiles')
DATA_DIR       = os.environ.get('MARIO_DATA_DIR') or os.path.expanduser('~/mario_data')
NORMALIZED_CACHE_DIR = os.environ.get('MARIO_NORMALIZED_CACHE_DIR') or os.path.join(DATA_DIR, 'normalized_cache')
HISTORY_FILE   = os.environ.get('MARIO_HISTORY_FILE') or os.path.expanduser('~/mario_history.json')
for _d in (PLAYLISTS_DIR, THUMBS_DIR, PROFILES_DIR, DATA_DIR, NORMALIZED_CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────
playlist           = []
current_index      = 0          # index of video currently streaming
ffmpeg_process     = None
# v3.7: protects ffmpeg_process pointer across stop/skip/monitor/shutdown.
_ffmpeg_lock       = threading.Lock()
streaming          = False
stream_lock        = threading.Lock()
# v3.3: protects current_index + stream_stats + log-counter from race conditions
_state_lock        = threading.Lock()
# v3.3: serializes reads/writes of HISTORY_FILE (otherwise concurrent
# session-end + /api/history/clear corrupts the JSON file).
_history_lock      = threading.Lock()
ffmpeg_logs        = deque(maxlen=200)
ffmpeg_logs_seq    = 0          # monotonically increasing line counter for SSE
stream_start_time  = None
auto_restart       = False
last_stream_params = {}
restart_count      = 0
restart_attempts   = 0          # consecutive crash counter — drives backoff
shuffle_mode       = False
normalization_status = {
    'enabled': True,
    'state': 'idle',
    'message': 'Ready',
    'current': 0,
    'total': 0,
    'cache_status': 'Ready',
    'error': '',
}

# Bounded SSE subscribers — protects against tab-bomb DoS.
SSE_MAX_CLIENTS    = int(os.environ.get('MARIO_SSE_MAX', '20'))
_sse_clients       = 0
_sse_lock          = threading.Lock()

# v3.7: track concat tempfiles so _graceful_shutdown can clean them up.
_concat_tempfiles  = deque(maxlen=8)
def _track_concat_tempfile(p):
    _concat_tempfiles.append(p)
    # Best-effort cleanup of older entries (keep last 2 for crash forensics).
    while len(_concat_tempfiles) > 2:
        old = _concat_tempfiles.popleft()
        try: os.unlink(old)
        except Exception: pass

stream_stats = {
    'fps':0,'bitrate':'0kbits/s','speed':'0x',
    'frames':0,'dropped':0,'size_kb':0,'time':'00:00:00'
}
_STATS_RE = re.compile(
    r'frame=\s*(?P<frames>\d+).*?fps=\s*(?P<fps>[\d.]+).*?'
    r'size=\s*(?P<size>\d+)kB.*?time=(?P<time>[\d:.]+).*?'
    r'bitrate=\s*(?P<bitrate>[\d.]+\S+).*?speed=\s*(?P<speed>\S+)', re.S)
_DROP_RE  = re.compile(r'(\d+) frame drop')

VIDEO_EXTENSIONS = ['*.mp4','*.avi','*.mkv','*.mov','*.wmv','*.flv','*.webm','*.m4v']
AUDIO_EXTENSIONS = ['*.mp3','*.wav','*.aac','*.flac','*.ogg','*.m4a','*.opus']


# ── v3.4: Webhook notifications (Telegram + Discord) ─────────────────────────
# Fires on stream start / stop / auto-restart-abandoned. Best-effort, never
# blocks the calling thread. Configure via env:
#   MARIO_TELEGRAM_TOKEN, MARIO_TELEGRAM_CHAT_ID
#   MARIO_DISCORD_WEBHOOK   (full webhook URL)
#   MARIO_WEBHOOK_EVENTS    (comma list: start,stop,error  default: all)
TELEGRAM_TOKEN   = os.environ.get('MARIO_TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('MARIO_TELEGRAM_CHAT_ID', '').strip()
DISCORD_WEBHOOK  = os.environ.get('MARIO_DISCORD_WEBHOOK', '').strip()
_WEBHOOK_EVENTS  = {e.strip().lower() for e in
                    os.environ.get('MARIO_WEBHOOK_EVENTS','start,stop,error').split(',') if e.strip()}

def _notify_webhooks(event, text):
    """Fire-and-forget Telegram + Discord notifier. Never raises."""
    if event not in _WEBHOOK_EVENTS:
        return
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) and not DISCORD_WEBHOOK:
        return
    def _send():
        import urllib.request, urllib.parse
        try:
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
                data = urllib.parse.urlencode({
                    'chat_id': TELEGRAM_CHAT_ID,
                    'text': f'[mario:{event}] {text}'[:4000],
                }).encode()
                req = urllib.request.Request(url, data=data,
                    headers={'Content-Type':'application/x-www-form-urlencoded'})
                urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            log.warning('telegram webhook failed: %s', e)
        try:
            if DISCORD_WEBHOOK:
                payload = json.dumps({'content': f'**[mario:{event}]** {text}'[:1900]}).encode()
                req = urllib.request.Request(DISCORD_WEBHOOK, data=payload,
                    headers={'Content-Type':'application/json'})
                urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            log.warning('discord webhook failed: %s', e)
    threading.Thread(target=_send, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_video_info(filepath):
    try:
        r = subprocess.run(
            ['ffprobe','-v','quiet','-print_format','json',
             '-show_format','-show_streams', filepath],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            dur  = float(data.get('format',{}).get('duration',0))
            w, h, has_audio, rotation = 1280, 720, False, 0
            for s in data.get('streams',[]):
                if s.get('codec_type')=='video':
                    w = s.get('width',1280); h = s.get('height',720)
                    # ZOOM FIX: respect Display Matrix rotation (mobile portrait
                    # videos report landscape w/h with rotation=±90).
                    try:
                        rot_tag = int(s.get('tags',{}).get('rotate', 0) or 0)
                    except (TypeError, ValueError):
                        rot_tag = 0
                    rot_sd = 0
                    for sd in s.get('side_data_list',[]) or []:
                        if sd.get('side_data_type') == 'Display Matrix':
                            try:
                                rot_sd = int(round(float(sd.get('rotation', 0))))
                            except (TypeError, ValueError):
                                rot_sd = 0
                    rotation = (rot_tag or rot_sd) % 360
                    if rotation in (90, 270, -90, -270):
                        w, h = h, w
                if s.get('codec_type')=='audio':
                    has_audio = True
            return {'duration':dur,'duration_str':fmt_dur(dur),
                    'width':w,'height':h,'rotation':rotation,
                    'has_audio':has_audio,
                    'size':os.path.getsize(filepath),
                    'size_str':fmt_size(os.path.getsize(filepath))}
    except Exception as e:
        print(f'ffprobe error: {e}')
    return {'duration':0,'duration_str':'00:00','width':1280,'height':720,
            'rotation':0,'has_audio':False,'size':0,'size_str':'0 B'}


def fmt_dur(s):
    s=int(s); h,m,sec=s//3600,(s%3600)//60,s%60
    return f'{h:02d}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'

def fmt_size(b):
    for u in ['B','KB','MB','GB']:
        if b<1024: return f'{b:.1f} {u}'
        b/=1024
    return f'{b:.1f} TB'

def check_v4l2():
    # Fast path — kernel exposes loaded modules under /sys/module.
    # Falls back to lsmod only if /sys is unavailable.
    if os.path.isdir('/sys/module/v4l2loopback'):
        return True
    try:
        return 'v4l2loopback' in subprocess.run(
            ['lsmod'], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return False

def get_virtual_devices():
    virtual=[]
    for dev in sorted(glob.glob('/dev/video*')):
        try:
            r=subprocess.run(['v4l2-ctl','--device',dev,'--info'],
                             capture_output=True,text=True,timeout=2)
            if 'v4l2 loopback' in r.stdout.lower() or 'dummy' in r.stdout.lower():
                virtual.append(dev)
        except: pass
    return virtual or ['/dev/video0']

# Cache expensive system checks — lsmod + ffmpeg -version are slow to run
# on every 5-second status poll. Refresh at most once every 30 seconds.
_sys_cache      = {'v4l2': False, 'ffmpeg': False, 'devices': ['/dev/video0']}
_sys_cache_time = 0.0
_sys_cache_lock = threading.Lock()

def get_system_status():
    global _sys_cache_time
    now = time.time()
    with _sys_cache_lock:
        if now - _sys_cache_time > 30:
            _sys_cache['v4l2']   = check_v4l2()
            _sys_cache['devices']= get_virtual_devices() if _sys_cache['v4l2'] else ['/dev/video0']
            try:
                subprocess.run(['ffmpeg','-version'], capture_output=True, timeout=3)
                _sys_cache['ffmpeg'] = True
            except Exception:
                _sys_cache['ffmpeg'] = False
            _sys_cache_time = now
    return _sys_cache.copy()

def build_audio_filter(global_volume, mute_audio, extra_audio):
    if mute_audio and not extra_audio: return [], ['-an']
    if extra_audio:
        ei=['-stream_loop','-1','-i',extra_audio]
        if mute_audio:
            af=f'[1:a]volume={global_volume}[aout]'
        else:
            af=(f'[0:a]volume={global_volume}[a0];'
                f'[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:normalize=0[aout]')
        return ei,['-filter_complex',af,'-map','0:v','-map','[aout]']
    return [],['-af',f'volume={global_volume}']

def build_ffmpeg_cmd(params):
    device       = params.get('device','/dev/video0')
    loop         = params.get('loop',True)
    resolution   = params.get('resolution','auto')
    fps          = int(params.get('fps',30))
    preset       = params.get('preset','ultrafast')
    buffer_size  = int(params.get('buffer_size',0))
    global_volume= float(params.get('global_volume',1.0))
    mute_audio   = bool(params.get('mute_audio',False))
    extra_audio  = params.get('extra_audio') or None
    rtmp_url     = params.get('rtmp_url','').strip()
    text_overlay = params.get('text_overlay','').strip()
    overlay_pos  = params.get('overlay_pos','tl')

    # Video filters
    vf_brightness  = float(params.get('vf_brightness', 0.0))    # -1.0 → +1.0
    vf_contrast    = float(params.get('vf_contrast',   1.0))    #  0.0 → +2.0
    vf_saturation  = float(params.get('vf_saturation', 1.0))    #  0.0 → +3.0
    vf_grayscale   = bool(params.get('vf_grayscale',   False))

    # New: watermark + recording + secondary RTMP
    watermark         = params.get('watermark') or None
    watermark_pos     = params.get('watermark_pos','br')
    watermark_opacity = float(params.get('watermark_opacity',0.7))
    watermark_scale   = float(params.get('watermark_scale',  0.15))
    record            = bool(params.get('record',False))
    record_dir        = params.get('record_dir', os.path.expanduser('~/mario_recordings'))
    rtmp_url_2        = (params.get('rtmp_url_2','') or '').strip()

    # ── Target output resolution ─────────────────────────────────────────────
    # ZOOM FIX (v3.9.5): the output canvas is LOCKED at stream start. Once a
    # stream begins, the resolution chosen here is stamped into
    # last_stream_params['_locked_wh'] (see _launch_ffmpeg). Subsequent
    # rebuilds for auto-restart resume or "skip" reuse that locked size,
    # so a later clip with a different aspect ratio can NEVER change the
    # output geometry mid-stream.
    locked_wh  = params.get('_locked_wh')
    auto_mode  = str(params.get('auto_res_mode', 'first')).lower()
    target_ori = str(params.get('target_orientation', 'auto')).lower()

    def _even(n):  # h264 requires even dimensions
        n = int(n); return n if n % 2 == 0 else n + 1

    if locked_wh and isinstance(locked_wh, (list, tuple)) and len(locked_wh) == 2:
        w, h = _even(locked_wh[0]), _even(locked_wh[1])
    elif resolution == 'auto':
        infos = [v['info'] for v in playlist if v.get('info')]
        if not infos:
            w, h = 1280, 720
        elif auto_mode == 'first':
            # Use FIRST clip's rotation-corrected dimensions — predictable
            # and removes the zoom artifact for single-orientation playlists.
            w = infos[0].get('width', 1280); h = infos[0].get('height', 720)
        elif auto_mode == 'min':
            # Legacy behavior (kept for backwards compat).
            w = min(i.get('width', 1280) for i in infos)
            h = min(i.get('height', 720) for i in infos)
        else:  # 'common' — pick dominant orientation bucket then max in bucket
            buckets = {'landscape': [], 'portrait': [], 'square': []}
            for i in infos:
                iw, ih = i.get('width', 1280), i.get('height', 720)
                if   iw > ih: buckets['landscape'].append((iw, ih))
                elif ih > iw: buckets['portrait'].append((iw, ih))
                else:         buckets['square'].append((iw, ih))
            winner = max(buckets, key=lambda k: len(buckets[k]))
            picks  = buckets[winner] or [(1280, 720)]
            w = max(p[0] for p in picks)
            h = max(p[1] for p in picks)

        # Forced orientation override (UI knob)
        if target_ori == 'landscape' and h > w: w, h = h, w
        elif target_ori == 'portrait' and w > h: w, h = h, w
        elif target_ori == 'square':
            side = max(w, h); w = h = side

        w, h = _even(w), _even(h)
    else:
        w, h = resolution.split('x')
        w, h = _even(w), _even(h)

    buf_flags     =['-fflags','nobuffer','-flags','low_delay'] if buffer_size==0 else []
    buf_size_flags=['-bufsize',f'{buffer_size}k']              if buffer_size>0  else []
    loop_flag     =['-stream_loop','-1']                       if loop           else []

    start_index = int(params.get('_start_index', 0)) % max(1, len(playlist))
    ordered = playlist[start_index:] + playlist[:start_index]

    # Phase 4B: loop_mode='one' → repeat ONLY the current/first clip.
    # The outer -stream_loop -1 already loops the concat file, so we
    # narrow `ordered` to a single entry. loop_mode='once' falls through
    # with loop=False (set in validate_stream_params).
    if str(params.get('loop_mode', '')).lower() == 'one' and ordered:
        ordered = [ordered[0]]

    # v3.7: per-stream concat tempfile (was fixed /tmp/mario_playlist.txt — race
    # condition + predictable path). 0600 perms; cleaned by _graceful_shutdown.
    fd, concat_path = tempfile.mkstemp(prefix='mario_concat_', suffix='.txt')
    bumper_path = params.get('bumper_path') or None
    _norm_map = params.get('_normalized_paths') or {}
    with os.fdopen(fd, 'w') as f:
        # v3.4: pre-roll bumper plays once before the loop. Skipped on auto-restart
        # resume so listeners don't hear the intro on every crash.
        if bumper_path and int(params.get('_start_index', 0)) == 0:
            safe_b = bumper_path.replace("'", "\\'").replace('\n', '').replace('\r', '')
            f.write(f"file '{safe_b}'\n")
        for v in ordered:
            src_path = v['path']
            try:
                sofs = max(0.0, float(v.get('start_offset_seconds', 0) or 0))
            except (TypeError, ValueError):
                sofs = 0.0
            # v3.9.16: prefer the offset-aware cache key (same source at
            # two different offsets gets two cache files); fall back to the
            # legacy plain-path key, then to the original source.
            use_path = None
            if _norm_map:
                try:
                    use_path = _norm_map.get(normalized_map_key(src_path, sofs))
                except NameError:
                    use_path = None
                if not use_path:
                    use_path = _norm_map.get(src_path)
            if not use_path:
                use_path = src_path
            # Escape single quotes and strip newlines to prevent FFmpeg concat injection
            safe_path = use_path.replace("'", "\\'").replace('\n', '').replace('\r', '')
            f.write(f"file '{safe_path}'\n")
            # v3.9.16: when Normalize-Before-Live is OFF the cache file is
            # the original source, so we honor start_offset via the concat
            # demuxer's `inpoint` directive. When it's ON the cache already
            # starts at the offset, so no inpoint needed.
            if sofs > 0 and use_path == src_path:
                f.write(f'inpoint {sofs:.3f}\n')
    _track_concat_tempfile(concat_path)

    inputs=[*buf_flags,*loop_flag,'-re','-f','concat','-safe','0','-i',concat_path]
    # (audio_map / extra_inputs are now built inside the unified filter_complex below)

    # ── Video filter chain ────────────────────────────────────────────────────
    # v3.9.4: fit_mode controls scale/pad behavior. Default 'fit' = letterbox,
    # which preserves aspect ratio and pads with black bars. NEVER crops or
    # auto-zooms — fixes the periodic "zoom-in then back" artifact caused by
    # mismatched source aspect ratios.
    fit_mode = str(params.get('fit_mode', 'fit')).lower()
    # v3.9.5: every chain ends with setsar=1, setdar=W/H, fps=FPS, format=yuv420p
    # so the output frames sent to v4l2loopback / RTMP are byte-for-byte the
    # same shape for the entire stream — no SAR/DAR drift, no fps drift,
    # no pixel-format switch when a clip with different params is decoded.
    _tail = ['setsar=1', f'setdar={w}/{h}', f'fps={fps}', 'format=yuv420p']
    if fit_mode == 'fill':
        # Cover the whole canvas: scale up to fill, then crop overflow.
        vf_parts = [
            f'scale={w}:{h}:force_original_aspect_ratio=increase',
            f'crop={w}:{h}',
            *_tail,
        ]
    elif fit_mode == 'stretch':
        # Ignore aspect ratio entirely — may distort.
        vf_parts = [
            f'scale={w}:{h}:force_original_aspect_ratio=disable',
            *_tail,
        ]
    else:
        # 'fit' (default): letterbox — preserve AR, scale down/up to fit, pad black.
        # NEVER crops, NEVER auto-zooms.
        vf_parts = [
            f'scale={w}:{h}:force_original_aspect_ratio=decrease',
            f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black',
            *_tail,
        ]

    # eq filter: only add if any value differs from default
    eq_needed = (vf_brightness != 0.0 or vf_contrast != 1.0 or vf_saturation != 1.0)
    if eq_needed:
        vf_parts.append(
            f'eq=brightness={vf_brightness:.3f}'
            f':contrast={vf_contrast:.3f}'
            f':saturation={vf_saturation:.3f}'
        )

    # grayscale via hue filter (sets saturation=0)
    if vf_grayscale:
        vf_parts.append('hue=s=0')

    if text_overlay:
        pos_map={'tl':'x=20:y=20','tr':'x=w-tw-20:y=20',
                 'bl':'x=20:y=h-th-20','br':'x=w-tw-20:y=h-th-20'}
        xy = pos_map.get(overlay_pos,'x=20:y=20')
        safe_text = text_overlay.replace("'","\\'").replace(':','\\:')
        vf_parts.append(
            f"drawtext=text='{safe_text}':{xy}:"
            f"fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=6"
        )

    # ── Build a unified filter_complex (video + watermark + audio) ───────────
    video_chain = ','.join(vf_parts)
    fc_parts = [f'[0:v]{video_chain}[v0]']

    extra_inputs = []
    last_video = '[v0]'
    next_input_idx = 1   # index in `-i` list for any extra inputs

    # Watermark overlay (separate input, then overlay onto video)
    if watermark:
        wm_idx = next_input_idx
        extra_inputs += ['-i', watermark]
        wm_pos = {
            'tl': '10:10', 'tr': 'main_w-overlay_w-10:10',
            'bl': '10:main_h-overlay_h-10',
            'br': 'main_w-overlay_w-10:main_h-overlay_h-10',
        }.get(watermark_pos, 'main_w-overlay_w-10:main_h-overlay_h-10')
        fc_parts.append(
            f'[{wm_idx}:v]scale=iw*{watermark_scale:.3f}*{w}/iw:-1,'
            f'format=rgba,colorchannelmixer=aa={watermark_opacity:.3f}[wm]'
        )
        fc_parts.append(f'{last_video}[wm]overlay={wm_pos}[v1]')
        last_video = '[v1]'
        next_input_idx += 1

    # Audio chain — only when not muting
    # Phase 7/8: with the concat demuxer, [0:a] only exists if EVERY input
    # has an audio stream. If even one clip is silent, referencing [0:a]
    # makes FFmpeg abort with "Stream map '0:a' matches no streams".
    # Therefore:
    #   - src_all_audio == True  → safe to use [0:a]
    #   - src_all_audio == False → must drop [0:a]. Use external audio
    #                              alone if provided, else -an.
    # v3.9.9: normalized cache files ALWAYS have a stereo audio track
    # (silent or mixed), so [0:a] is guaranteed when normalize is on.
    if params.get('_normalized_paths'):
        src_all_audio = True
    else:
        src_all_audio = bool(ordered) and all(
            bool((v.get('info') or {}).get('has_audio'))
            for v in ordered
        )
    audio_label = None
    if not mute_audio:
        # v3.4: optional loudnorm pass for consistent loudness across mixed clips
        norm_on  = bool(params.get('audio_normalize', False))
        norm_flt = ''
        if norm_on:
            ni = float(params.get('audio_norm_i',  -16.0))
            nt = float(params.get('audio_norm_tp', -1.5))
            nl = float(params.get('audio_norm_lra', 11.0))
            norm_flt = f',loudnorm=I={ni}:TP={nt}:LRA={nl}'
        if extra_audio and not src_all_audio:
            # Source playlist is silent or mixed → use external audio alone.
            ea_idx = next_input_idx
            extra_inputs += ['-stream_loop','-1','-i', extra_audio]
            fc_parts.append(
                f'[{ea_idx}:a]volume={global_volume}{norm_flt},aresample=48000[aout]'
            )
            next_input_idx += 1
            audio_label = '[aout]'
        elif extra_audio:
            # All source clips have audio → safe to mix with external.
            ea_idx = next_input_idx
            extra_inputs += ['-stream_loop','-1','-i', extra_audio]
            fc_parts.append(
                f'[0:a]volume={global_volume}{norm_flt}[a0];'
                f'[{ea_idx}:a]volume=1.0[a1];'
                f'[a0][a1]amix=inputs=2:normalize=0,aresample=48000[aout]'
            )
            next_input_idx += 1
            audio_label = '[aout]'
        elif src_all_audio:
            fc_parts.append(f'[0:a]volume={global_volume}{norm_flt},aresample=48000[aout]')
            audio_label = '[aout]'
        # else: mixed/silent source + no extra → audio_label=None → -an below

    filter_complex = ';'.join(fc_parts)

    # ── Outputs ──────────────────────────────────────────────────────────────
    # Hardware encoder selection (v3.2). libx264 = software (safe everywhere).
    # h264_v4l2m2m / h264_omx → Raspberry Pi HW encode (5-10x lower CPU).
    # h264_vaapi → Intel/AMD GPU on Linux. h264_nvenc → NVIDIA. h264_qsv → Intel QSV.
    # v3.9.17: fixed GOP for the RTMP-facing encode. Without an explicit
    # keyframe interval, x264's scenecut detector is free to place
    # keyframes at any spacing it likes (default keyint=250, i.e. ~8s at
    # 30fps) and to drop keyframes at silent scene-cuts. Several RTMP
    # ingest platforms are strict about needing a keyframe every ~2s;
    # when one is late, the platform's transcoder falls back to its last
    # good frame / a placeholder rendition (often a different implicit
    # crop/scale) until the next keyframe arrives, which reads exactly
    # like a sudden "zoom" that later snaps back. -sc_threshold 0 forces
    # ONLY the fixed-interval keyframes we ask for (no extra scenecut
    # keyframes with a different GOP position), which some ingest
    # servers also mishandle.
    _gop = max(1, int(round(2 * fps)))
    fixed_gop = ['-g', str(_gop), '-keyint_min', str(_gop), '-sc_threshold', '0']

    hw_encoder = params.get('hw_encoder', 'libx264')
    if hw_encoder == 'libx264':
        base_v_enc = ['-c:v', 'libx264', '-preset', preset, *fixed_gop, *buf_size_flags]
    elif hw_encoder in ('h264_v4l2m2m', 'h264_omx'):
        # Pi HW encoders ignore -preset; they tune via bitrate only.
        base_v_enc = ['-c:v', hw_encoder, '-g', str(_gop), *buf_size_flags]
    elif hw_encoder == 'h264_nvenc':
        base_v_enc = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-tune', 'll',
                      '-g', str(_gop), *buf_size_flags]
    elif hw_encoder == 'h264_qsv':
        base_v_enc = ['-c:v', 'h264_qsv', '-preset', 'veryfast', '-g', str(_gop), *buf_size_flags]
    elif hw_encoder == 'h264_vaapi':
        # VAAPI needs format=nv12 + hwupload; not added here to avoid breaking
        # the filter_complex chain. Operators wanting VAAPI should patch in
        # vaapi_device on the input side. For now we fall back to libx264.
        base_v_enc = ['-c:v', 'libx264', '-preset', preset, *fixed_gop, *buf_size_flags]
    else:
        base_v_enc = ['-c:v', 'libx264', '-preset', preset, *fixed_gop, *buf_size_flags]

    # (28) Quality auto-adjust — pick sane bitrate from output resolution
    if bool(params.get('auto_quality', False)):
        pixels = w * h
        if   pixels >= 1920*1080: br = 4500
        elif pixels >= 1280*720:  br = 2800
        elif pixels >=  854*480:  br = 1400
        else:                     br =  800
        base_v_enc += ['-b:v', f'{br}k', '-maxrate', f'{br}k',
                       '-bufsize', f'{br*2}k']

    # v3.9.5: -vsync cfr enforces constant frame rate at the output side so
    # transitions between clips with mismatched frame timing can't change
    # the cadence we hand to v4l2loopback / RTMP.
    cmd = ['ffmpeg','-y', *inputs, *extra_inputs,
           '-filter_complex', filter_complex,
           '-r', str(fps), '-vsync', 'cfr']

    if rtmp_url:
        # Pure RTMP path (single FLV w/ AAC). Optionally tee to second RTMP / file.
        outputs = [{'url': rtmp_url, 'fmt': 'flv'}]
        if rtmp_url_2:
            outputs.append({'url': rtmp_url_2, 'fmt': 'flv'})
        if record:
            os.makedirs(record_dir, exist_ok=True)
            ts = time.strftime('%Y%m%d_%H%M%S')
            outputs.append({'url': os.path.join(record_dir, f'rec_{ts}.mp4'),
                            'fmt': 'mp4'})

        # v3.9.5: pin -s WxH + -pix_fmt yuv420p on the encoded output too —
        # belt-and-suspenders against any container/encoder negotiating a
        # different frame size from what the filter graph emits.
        size_flags = ['-s', f'{w}x{h}', '-pix_fmt', 'yuv420p']
        if len(outputs) == 1:
            cmd += ['-map', last_video,
                    *(['-map', audio_label] if audio_label else ['-an']),
                    *base_v_enc, *size_flags,
                    *(['-c:a','aac','-b:a','128k'] if audio_label else []),
                    '-f', 'flv', rtmp_url]
        else:
            tee_str = '|'.join(
                f"[f={o['fmt']}]{o['url']}" for o in outputs
            )
            cmd += ['-map', last_video,
                    *(['-map', audio_label] if audio_label else ['-an']),
                    *base_v_enc, *size_flags,
                    *(['-c:a','aac','-b:a','128k'] if audio_label else []),
                    '-flags','+global_header','-f','tee', tee_str]
        return (cmd, f'{w}x{h}')

    # No RTMP → v4l2 (+ optional VirtualMic + optional record)
    # v3.9.5: pin -s/-r/-pix_fmt at the v4l2 output so v4l2loopback receives
    # fixed-size raw frames for the entire stream.
    cmd += ['-map', last_video, '-preset', preset, *buf_size_flags,
            '-s', f'{w}x{h}', '-r', str(fps), '-pix_fmt', 'yuv420p',
            '-an', '-f','v4l2', device]
    if audio_label:
        cmd += ['-map', audio_label,
                '-vn', '-c:a','pcm_f32le', '-ac','2',
                '-f','pulse','VirtualMic']
    if record:
        os.makedirs(record_dir, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        rec_path = os.path.join(record_dir, f'rec_{ts}.mp4')
        cmd += ['-map', last_video,
                *(['-map', audio_label] if audio_label else []),
                '-c:v','libx264','-preset','veryfast','-crf','23',
                *(['-c:a','aac','-b:a','128k'] if audio_label else ['-an']),
                '-movflags','+faststart', rec_path]
    return (cmd, f'{w}x{h}')


# ── Thumbnail generator ───────────────────────────────────────────────────────

def get_or_make_thumb(filepath):
    """Return base64 JPEG thumbnail, generating & caching if needed.
    Cache is invalidated if the source file's mtime has changed since the
    thumbnail was created — so replacing a file with the same name works.
    """
    safe  = base64.urlsafe_b64encode(filepath.encode()).decode()[:120]
    cache = os.path.join(THUMBS_DIR, safe+'.jpg')

    # Invalidate stale cache: if source is newer than cached thumb, regenerate
    if os.path.exists(cache):
        try:
            src_mtime   = os.path.getmtime(filepath)
            cache_mtime = os.path.getmtime(cache)
            if src_mtime > cache_mtime:
                os.remove(cache)
        except OSError:
            pass

    if not os.path.exists(cache):
        try:
            subprocess.run([
                'ffmpeg','-y','-ss','00:00:03','-i',filepath,
                '-vframes','1','-vf','scale=160:90','-q:v','5', cache
            ], capture_output=True, timeout=15)
        except Exception as e:
            print(f'Thumb error: {e}'); return None

    if os.path.exists(cache):
        with open(cache,'rb') as f:
            return 'data:image/jpeg;base64,' + base64.b64encode(f.read()).decode()
    return None


# ── Stream history ────────────────────────────────────────────────────────────
# v3.5: backed by SQLite (state.history_*). Legacy ~/mario_history.json is
# auto-migrated once at startup, then renamed to .migrated. _history_lock kept
# for backward compatibility (other code paths still acquire it).

def _load_history():
    try:
        return state.history_all()
    except Exception as e:
        log.warning('history read failed: %s', e)
        return []

def _save_history(h):
    # Compat shim: only the "clear" path now calls this with []. Append uses
    # _record_session → state.history_append directly. If a list is passed in,
    # we replace the table contents (used for /api/history/clear).
    if not h:
        try: state.history_clear()
        except Exception as e: log.warning('history clear failed: %s', e)
        return
    # Bulk replace (rarely used)
    with _history_lock:
        try:
            state.history_clear()
            for row in h[-state.HISTORY_MAX_ROWS:]:
                state.history_append(row)
        except Exception as e:
            log.warning('history bulk-save failed: %s', e)

def _record_session(started, ended, params, restarts, peak_fps, total_frames):
    row = {
        'id':       int(started*1000),
        'started':  time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
        'ended':    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended)),
        'duration': int(ended-started),
        'duration_str': fmt_dur(int(ended-started)),
        'resolution': params.get('resolution','auto'),
        'device':     params.get('device','?'),
        'rtmp':       bool(params.get('rtmp_url','')),
        'playlist_count': len(playlist),
        'restarts':   restarts,
        'peak_fps':   peak_fps,
        'total_frames': total_frames,
    }
    try:
        state.history_append(row)
    except Exception as e:
        log.warning('history append failed: %s', e)


# ── FFmpeg launcher ───────────────────────────────────────────────────────────

# Regex that FFmpeg prints when it opens each input segment:
#   Opening 'path/to/file.mp4' for reading
_OPENING_RE = re.compile(r"Opening '(.+?)' for reading")

# Phase 6A: redact sensitive tokens (RTMP stream keys, auth in URLs, etc.)
# from any command/log line that may surface to the UI or operator logs.
# The real cmd sent to FFmpeg is NEVER modified — only its string copy.
_RTMP_REDACT_RE = re.compile(
    r'(rtmps?://[^\s"\']*?/[^/\s"\']+/)([^/\s"\']+)',
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r'(\b[a-z][a-z0-9+\-.]*://)([^/@\s]+:)([^@\s]+)@', re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r'([?&](?:token|key|secret|password|auth|signature|sig)=)([^&\s"\']+)',
    re.IGNORECASE,
)

def _redact(s):
    """Return a copy of `s` with RTMP stream keys / URL passwords / query
    secrets masked. Idempotent and safe to call on any string."""
    if not s:
        return s
    s = _URL_USERINFO_RE.sub(r'\1\2***@', s)
    s = _QUERY_SECRET_RE.sub(r'\1***', s)
    s = _RTMP_REDACT_RE.sub(r'\1***', s)
    return s

def redact_sensitive_command(cmd):
    """Return a redacted *copy* of an argv list (list[str]). Original is
    untouched so the subprocess call still uses real values."""
    return [_redact(str(a)) for a in (cmd or [])]

def _append_log(line):
    """Thread-safe log appender — increments the SSE sequence counter so
    subscribers can tell exactly how many new lines arrived since their
    last tick (the deque's maxlen=200 used to silently drop the old
    last_log_idx trick when many lines arrived between SSE ticks).

    Phase 6A: every log line is redacted before storage so neither SSE
    subscribers nor the on-disk audit trail leak RTMP stream keys."""
    global ffmpeg_logs_seq
    safe = _redact(line)
    with _state_lock:
        ffmpeg_logs.append(safe)
        ffmpeg_logs_seq += 1

# v3.3: auto-restart backoff — clamps to MAX_AUTO_RESTARTS, exp delay 2→60s.
MAX_AUTO_RESTARTS = int(os.environ.get('MARIO_MAX_AUTO_RESTARTS', '10'))

# ── v3.9.15: Start-Stream progress tracker ──────────────────────────────────
# Lightweight, in-memory progress object the frontend polls while the
# /api/stream/start request is blocking through validation/normalization/
# launch. Safe: contains no secrets, no paths outside basename.
_start_progress = {
    'active': False, 'phase': 'idle', 'percent': 0,
    'current_clip': None, 'current_index': 0, 'total': 0,
    'message': '', 'error': None,
    'started_at': None, 'updated_at': None,
}
_start_progress_lock = threading.Lock()

def _set_start_progress(**kwargs):
    """Merge fields into the live start-progress object. Always stamps
    updated_at. `percent` is clamped to [0,100]."""
    with _start_progress_lock:
        for k, v in kwargs.items():
            if k == 'percent' and v is not None:
                try: v = max(0, min(100, int(v)))
                except Exception: v = _start_progress.get('percent', 0)
            _start_progress[k] = v
        _start_progress['updated_at'] = time.time()

def _reset_start_progress(active=True, total=0):
    with _start_progress_lock:
        _start_progress.update({
            'active': bool(active), 'phase': 'starting', 'percent': 0,
            'current_clip': None, 'current_index': 0, 'total': int(total),
            'message': 'Preparing stream…', 'error': None,
            'started_at': time.time(), 'updated_at': time.time(),
        })

def _snapshot_start_progress():
    with _start_progress_lock:
        return dict(_start_progress)

# ─── v3.9.9 Normalize-Before-Live ────────────────────────────────────────────
# Pre-encode each source into a fixed canvas/fps/SAR/DAR/pixel-format cached
# MP4 BEFORE the live FFmpeg sees it. This eliminates the mid-clip zoom-in
# artifact caused by SAR/DAR/rotation/crop metadata or filter reinitialization
# when a clip's intrinsic params change inside the same file.
NORMALIZATION_VERSION = 3  # v3.9.16: per-clip start_offset_seconds baked in


def _probe_has_audio(src):
    """Return True iff `src` contains at least one audio stream.
    Uses ffprobe; safe fallback to False on any error."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=codec_type',
             '-of', 'csv=p=0', src],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and 'audio' in (r.stdout or '')
    except Exception:
        return False

def get_normalized_cache_dir():
    d = (os.environ.get('MARIO_NORMALIZED_CACHE_DIR')
         or os.path.join(os.environ.get('MARIO_DATA_DIR')
                         or os.path.expanduser('~/mario_data'),
                         'normalized_cache'))
    os.makedirs(d, exist_ok=True)
    return d

def build_normalized_cache_key(src, settings):
    try:
        st = os.stat(src); size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        size, mtime = 0, 0
    # v3.9.16: include start_offset_seconds so cache regenerates when the
    # user changes the trim point.
    try:
        sofs = round(float(settings.get('start_offset_seconds', 0) or 0), 3)
    except (TypeError, ValueError):
        sofs = 0.0
    key = '|'.join(str(x) for x in (
        os.path.abspath(src), size, mtime,
        settings.get('w'), settings.get('h'),
        settings.get('fps'), settings.get('fit_mode'),
        sofs,
        NORMALIZATION_VERSION,
    ))
    return hashlib.sha1(key.encode('utf-8')).hexdigest()


def normalized_map_key(src, start_offset_seconds=0):
    """Stable composite key used to dedupe _normalized_paths when the same
    source appears with multiple start offsets. Matches the key used by
    build_ffmpeg_cmd to look up the cache for each playlist row."""
    try:
        sofs = round(float(start_offset_seconds or 0), 3)
    except (TypeError, ValueError):
        sofs = 0.0
    return f'{src}|{sofs}'

def normalize_video_for_live(src, settings):
    """Normalize ONE source. Returns (cache_path, reused_bool).
    Raises RuntimeError on failure (caller surfaces to UI).

    v3.9.16: when settings['start_offset_seconds'] > 0 the encoded cache
    starts at that offset (input-level `-ss` before `-i`)."""
    cache_dir  = get_normalized_cache_dir()
    key        = build_normalized_cache_key(src, settings)
    cache_path = os.path.join(cache_dir, f'{key}.mp4')

    try:
        sofs = max(0.0, float(settings.get('start_offset_seconds', 0) or 0))
    except (TypeError, ValueError):
        sofs = 0.0

    _append_log(f'[NORMALIZE] source={src}')
    if sofs > 0:
        _append_log(f'[NORMALIZE] start_offset_seconds={sofs:.3f}')
    _append_log(f'[NORMALIZE] cache={cache_path}')
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        _append_log('[NORMALIZE] reused=true')
        return cache_path, True

    w   = int(settings['w']);  h = int(settings['h'])
    fps = int(settings['fps'])
    fit = (settings.get('fit_mode') or 'fit').lower()
    if fit == 'fill':
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=increase,'
              f'crop={w}:{h},setsar=1,setdar={w}/{h},fps={fps},format=yuv420p')
    elif fit == 'stretch':
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=disable,'
              f'setsar=1,setdar={w}/{h},fps={fps},format=yuv420p')
    else:
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=decrease,'
              f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,'
              f'setsar=1,setdar={w}/{h},fps={fps},format=yuv420p')

    tmp = cache_path + '.tmp.mp4'
    # v3.9.10: detect audio FIRST, then build either a "source-audio" or a
    # "silent-audio" command. Avoids the unsafe `[0:a?]` reference inside
    # filter_complex (which fails on silent inputs). Every normalized cache
    # file ends up with a consistent AAC stereo 48k audio track so the
    # concat demuxer sees identical stream layouts across the playlist.
    has_audio = _probe_has_audio(src)
    _append_log(f'[NORMALIZE] source_has_audio={"true" if has_audio else "false"}')
    _append_log(f'[NORMALIZE] audio_mode={"source" if has_audio else "silent"}')

    common_v = [
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
        '-pix_fmt', 'yuv420p',
    ]
    common_a = ['-c:a', 'aac', '-ar', '48000', '-ac', '2']

    # v3.9.16: input-level seek before `-i` is fast and accurate enough for
    # our re-encode (we always re-decode). Goes BEFORE the input it applies to.
    seek_in = (['-ss', f'{sofs:.3f}'] if sofs > 0 else [])

    # v3.9.17 ZOOM FIX: '-noautorotate' used to be passed here with no
    # compensating transpose filter, so a phone clip with a 90°/270°
    # Display Matrix rotation tag was decoded in its RAW sensor
    # orientation (e.g. landscape pixels for visually-portrait content)
    # and then force-scaled/padded into the locked canvas — while
    # get_video_info() (used to pick that same canvas's W×H) already
    # swaps w/h for rotated clips. That mismatch between "canvas chosen
    # assuming rotation is applied" and "cache encoded assuming rotation
    # is NOT applied" is what produced a per-clip zoom/stretch artifact
    # for any portrait mobile clip in the playlist. Dropping
    # '-noautorotate' lets ffmpeg apply the Display Matrix rotation on
    # decode (the default), so every normalized cache is rotated the
    # same way get_video_info() assumed when the canvas was sized.
    if has_audio:
        cmd = [
            'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'warning',
            *seek_in,
            '-i', src,
            '-vf', vf,
            *common_v,
            *common_a,
            '-movflags', '+faststart',
            tmp,
        ]
    else:
        cmd = [
            'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'warning',
            *seek_in,
            '-i', src,
            '-f', 'lavfi', '-i',
            'anullsrc=channel_layout=stereo:sample_rate=48000',
            '-vf', vf,
            '-map', '0:v:0', '-map', '1:a:0',
            '-shortest',
            *common_v,
            *common_a,
            '-movflags', '+faststart',
            tmp,
        ]
    _append_log('[NORMALIZE] encoding…')
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        try: os.unlink(tmp)
        except OSError: pass
        raise RuntimeError(f'normalization timed out: {os.path.basename(src)}')
    if proc.returncode != 0 or not (os.path.isfile(tmp) and os.path.getsize(tmp) > 0):
        tail = (proc.stderr or '').strip().splitlines()[-3:]
        try: os.unlink(tmp)
        except OSError: pass
        raise RuntimeError(
            f'normalization failed for {os.path.basename(src)}: ' + ' | '.join(tail))
    os.replace(tmp, cache_path)
    _append_log(f'[NORMALIZE] reused=false bytes={os.path.getsize(cache_path)} '
                f'output={cache_path}')
    return cache_path, False

def _fmt_hms(secs):
    try: s = int(round(float(secs or 0)))
    except (TypeError, ValueError): s = 0
    if s < 0: s = 0
    h, rem = divmod(s, 3600); m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'

def prepare_normalized_playlist(items, w, h, fps, fit_mode):
    """Normalize EVERY playlist source. Returns a dict keyed by
    `normalized_map_key(src, start_offset_seconds)` so the same source at
    two different offsets gets two distinct cache files.
    Raises RuntimeError on the first failure — live MUST NOT start."""
    base_settings = {'w': int(w), 'h': int(h), 'fps': int(fps),
                     'fit_mode': fit_mode or 'fit'}
    mapping = {}
    total = len(items)
    _set_start_progress(phase='normalizing', total=total,
                        message=f'Normalizing 0/{total}', percent=10)
    completed = 0
    for i, v in enumerate(items, 1):
        src = v['path']
        try:
            sofs = max(0.0, float(v.get('start_offset_seconds', 0) or 0))
        except (TypeError, ValueError):
            sofs = 0.0
        base = os.path.basename(src)
        mkey = normalized_map_key(src, sofs)
        from_str = f' from {_fmt_hms(sofs)}' if sofs > 0 else ''
        _set_start_progress(
            phase='normalizing', current_index=i, total=total,
            current_clip=base,
            message=f'Normalizing {i}/{total} — {base}{from_str}',
            percent=int(10 + (completed / max(1, total)) * 75),
        )
        if mkey in mapping:
            completed += 1; continue
        _append_log(f'[NORMALIZE] {i}/{total} {base}{from_str}')
        per_settings = dict(base_settings, start_offset_seconds=sofs)
        cache_path, reused = normalize_video_for_live(src, per_settings)
        mapping[mkey] = cache_path
        completed += 1
        _set_start_progress(
            phase='normalizing', current_index=i, total=total,
            current_clip=base,
            message=('Using cached normalized clip ' if reused else
                     'Normalized clip ') + f'{i}/{total}{from_str}',
            percent=int(10 + (completed / max(1, total)) * 75),
        )
    _set_start_progress(phase='normalized', percent=85,
                        message=f'Normalized {total}/{total}')
    return mapping
# ─── /Normalize-Before-Live ──────────────────────────────────────────────────

def _launch_ffmpeg(params):
    global ffmpeg_process, streaming, stream_start_time, stream_stats, current_index, restart_attempts, last_stream_params

    # First build: needed only to determine the locked canvas (resolution
    # may be 'auto' and depend on the first clip). The concat tempfile this
    # produces is replaced by the second build below when normalization is on.
    cmd, res = build_ffmpeg_cmd(params)
    # v3.9.5: lock the canvas for the lifetime of this stream. Auto-restart
    # resume + skip rebuild from last_stream_params, so stamping _locked_wh
    # there guarantees identical W/H/fps/format on every rebuild.
    try:
        _lw, _lh = (int(x) for x in res.split('x'))
        params['_locked_wh'] = (_lw, _lh)
        if last_stream_params is not None:
            last_stream_params['_locked_wh'] = (_lw, _lh)
    except Exception:
        pass

    # ── v3.9.9: Normalize-Before-Live ────────────────────────────────────────
    # When ON, pre-encode every source into the normalized_cache and rebuild
    # the FFmpeg cmd so its concat file references the NORMALIZED paths, not
    # the originals. This is the actual fix for the mid-clip zoom artifact.
    if params.get('normalize_before_live', True):
        try:
            lw, lh = params.get('_locked_wh') or (1280, 720)
            fps_v  = int(params.get('fps', 30))
            fit_v  = params.get('fit_mode', 'fit')
            mapping = prepare_normalized_playlist(playlist, lw, lh, fps_v, fit_v)
            params['_normalized_paths'] = mapping
            if last_stream_params is not None:
                last_stream_params['_normalized_paths'] = mapping
            cmd, res = build_ffmpeg_cmd(params)        # rebuild with cache paths
            _append_log('[LIVE] using normalized playlist=true')
            _append_log('[NORMALIZE] complete')
            _set_start_progress(phase='building', percent=90,
                                message='Building FFmpeg command…')
        except Exception as e:
            _append_log(f'[NORMALIZE] FAILED: {e}')
            log.exception('normalization failed')
            raise RuntimeError(f'Normalization failed: {e}')
    else:
        _append_log('[LIVE] using normalized playlist=false (toggle OFF)')
        _set_start_progress(phase='building', percent=88,
                            message='Building FFmpeg command…')
    with _state_lock:
        ffmpeg_logs.clear()
        stream_stats.update({'fps':0,'bitrate':'0kbits/s','speed':'0x',
                             'frames':0,'dropped':0,'size_kb':0,'time':'00:00:00'})
        current_index = 0
    # Phase 6A: log a redacted copy of the command — never the raw stream key.
    _append_log(f'CMD: {" ".join(redact_sensitive_command(cmd))}')
    # v3.9.5: geometry debug log — confirms canvas/fit-mode lock at a glance.
    try:
        _fc_idx = cmd.index('-filter_complex') + 1
        _vf_str = cmd[_fc_idx]
    except Exception:
        _vf_str = '?'
    _append_log(
        f'[GEOMETRY] canvas={res} fps={params.get("fps",30)} '
        f'fit_mode={params.get("fit_mode","fit")} '
        f'crop={"crop=" in _vf_str} pad={"pad=" in _vf_str} '
        f'canvas_locked=True filter={_vf_str}'
    )

    # Inherit environment and ensure PULSE_SERVER is set so FFmpeg can
    # reach PipeWire's PulseAudio-compatible socket for virtual mic audio.
    ffmpeg_env = os.environ.copy()
    try:
        uid = os.getuid()
        pulse_socket = f'/run/user/{uid}/pulse/native'
        if os.path.exists(pulse_socket):
            ffmpeg_env['PULSE_SERVER'] = f'unix:{pulse_socket}'
            _append_log(f'[PULSE] Using socket: {pulse_socket}')
        else:
            _append_log(f'[PULSE] WARNING: socket not found at {pulse_socket}')
    except AttributeError:
        pass  # Windows / non-POSIX

    # Phase 5: capture the process in a local ref BEFORE assigning to the
    # global. Monitor uses the local ref so an old monitor cannot mutate
    # state belonging to a newer process spawned by skip/restart.
    _set_start_progress(phase='launching', percent=96,
                        message='Launching FFmpeg…')
    _append_log('[START] launching FFmpeg')
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True,
                            env=ffmpeg_env)
    with _ffmpeg_lock:
        ffmpeg_process = proc
    streaming         = True
    _start            = time.time()
    stream_start_time = _start
    peak_fps          = 0
    log.info('ffmpeg started pid=%s res=%s', proc.pid, res)
    _notify_webhooks('start',
        f'Stream started — res={res} fps={params.get("fps",30)} '
        f'rtmp={"yes" if params.get("rtmp_url") else "no"} '
        f'clips={len(playlist)}')

    _path_to_idx = {v['path']: i for i, v in enumerate(playlist)}
    # v3.9.9: normalized cache files have different paths — also map those.
    _norm_map = params.get('_normalized_paths') or {}
    for _src, _cache in _norm_map.items():
        if _src in _path_to_idx:
            _path_to_idx[_cache] = _path_to_idx[_src]
    _last_clip   = {'path': None, 'name': None, 'ts': time.time()}

    def monitor(process_ref):
        """Each monitor thread is bound to ITS OWN process_ref. Mutations to
        global state (streaming flag, ffmpeg_process pointer) only happen when
        the global pointer still equals process_ref — i.e. nobody started a
        newer stream in the meantime."""
        global streaming, restart_count, current_index, stream_start_time, restart_attempts
        nonlocal peak_fps

        for line in process_ref.stderr:
            line = line.rstrip()
            _append_log(line)

            mo = _OPENING_RE.search(line)
            if mo:
                path = mo.group(1)
                if path in _path_to_idx:
                    if _last_clip['path']:
                        elapsed = int(time.time() - _last_clip['ts'])
                        try:
                            state.bump_clip(_last_clip['path'], _last_clip['name'], elapsed)
                        except Exception as e:
                            log.warning('bump_clip failed: %s', e)
                    with _state_lock:
                        current_index = _path_to_idx[path]
                    _last_clip['path'] = path
                    _last_clip['name'] = playlist[current_index]['name']
                    _last_clip['ts']   = time.time()

            m = _STATS_RE.search(line)
            if m:
                fps = float(m.group('fps'))
                peak_fps = max(peak_fps, fps)
                with _state_lock:
                    stream_stats.update({
                        'fps':     fps,
                        'bitrate': m.group('bitrate'),
                        'speed':   m.group('speed'),
                        'frames':  int(m.group('frames')),
                        'size_kb': int(m.group('size')),
                        'time':    m.group('time'),
                    })
            d = _DROP_RE.search(line)
            if d:
                with _state_lock:
                    stream_stats['dropped'] = stream_stats.get('dropped', 0) + int(d.group(1))

        process_ref.wait()
        session_end = time.time()
        if _last_clip['path']:
            try:
                state.bump_clip(_last_clip['path'], _last_clip['name'],
                                int(session_end - _last_clip['ts']))
            except Exception as e:
                log.warning('final bump_clip failed: %s', e)

        # Phase 5: only flip global state if WE are still the current process.
        # Otherwise a newer stream owns the globals and we must not touch them.
        with _ffmpeg_lock:
            still_current = (ffmpeg_process is process_ref)
            if still_current:
                ffmpeg_process_local = None  # noqa: F841 — symmetry with old code
                globals()['ffmpeg_process'] = None
        if still_current:
            streaming         = False
            stream_start_time = None
        log.info('ffmpeg exited rc=%s duration=%.1fs frames=%s still_current=%s',
                 process_ref.returncode, session_end - _start,
                 stream_stats.get('frames', 0), still_current)
        _notify_webhooks('stop',
            f'Stream ended rc={process_ref.returncode} '
            f'duration={int(session_end - _start)}s '
            f'frames={stream_stats.get("frames", 0)}')

        if session_end - _start >= 2:
            _record_session(_start, session_end, params, restart_count,
                            round(peak_fps, 1), stream_stats.get('frames', 0))
            # Stream ran long enough → reset crash counter
            restart_attempts = 0
        else:
            restart_attempts += 1

        # Auto-restart only when this monitor is the still-current one.
        # Prevents an old monitor from spawning a duplicate stream after
        # skip/stop has already replaced ffmpeg_process.
        if still_current and auto_restart and last_stream_params:
            if restart_attempts > MAX_AUTO_RESTARTS:
                _append_log(f'[mario] Auto-restart abandoned after '
                            f'{restart_attempts} consecutive crashes')
                log.error('auto-restart abandoned: %s consecutive crashes', restart_attempts)
                _notify_webhooks('error',
                    f'Auto-restart abandoned after {restart_attempts} crashes')
                return
            delay = min(60, 2 * (2 ** min(restart_attempts, 5)))
            restart_count += 1
            _append_log(
                f'[mario] Auto-restarting from clip #{current_index+1} '
                f'(attempt {restart_count}, delay {delay}s, '
                f'consecutive crashes {restart_attempts})'
            )
            time.sleep(delay)
            if not streaming:
                resume_params = dict(last_stream_params)
                resume_params['_start_index'] = current_index
                _launch_ffmpeg(resume_params)

    threading.Thread(target=monitor, args=(proc,), daemon=True).start()
    return res



# ── Smart shuffle ─────────────────────────────────────────────────────────────
def _smart_shuffle(items):
    """Shuffle so no clip plays twice in a row, weighted toward least-played
    clips (per analytics). Uses weighted random selection (Walker-style),
    NOT a deterministic sort, so two consecutive calls give different orders."""
    if len(items) < 3:
        out = list(items); random.shuffle(out); return out
    stats = {s['path']: s for s in state.get_clip_stats()}
    pool  = list(items)
    out   = []
    while pool:
        # Weight = 1 / (1 + play_count). Lower play_count → higher weight.
        weights = []
        for v in pool:
            pc = stats.get(v['path'], {}).get('play_count', 0)
            w  = 1.0 / (1.0 + pc)
            # Avoid immediate repeat: zero-weight the previous clip when others exist
            if out and v['path'] == out[-1]['path'] and len(pool) > 1:
                w = 0.0
            weights.append(w)
        if sum(weights) == 0:
            weights = [1.0] * len(pool)  # fallback
        pick = random.choices(range(len(pool)), weights=weights, k=1)[0]
        out.append(pool.pop(pick))
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return render_template('index.html')

# v3.7: serve manifest as a plain JSON dict (no Jinja templating, no dead
# `if False` branch). Avoids Jinja-injection surface on a public route.
_PWA_MANIFEST = {
    'name': 'Mario Camera Streamer',
    'short_name': 'Mario',
    'description': 'Loop video playlists into a virtual camera + RTMP',
    'start_url': '/',
    'display': 'standalone',
    'orientation': 'any',
    'background_color': '#0a0a0f',
    'theme_color': '#00d4ff',
    'icons': [
        {'src': "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='32' fill='%2300d4ff'/><text x='50%25' y='58%25' font-size='110' text-anchor='middle' fill='%230a0a0f' font-family='sans-serif' font-weight='700'>M</text></svg>",
         'sizes': '192x192', 'type': 'image/svg+xml', 'purpose': 'any maskable'},
        {'src': "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='80' fill='%2300d4ff'/><text x='50%25' y='58%25' font-size='300' text-anchor='middle' fill='%230a0a0f' font-family='sans-serif' font-weight='700'>M</text></svg>",
         'sizes': '512x512', 'type': 'image/svg+xml', 'purpose': 'any maskable'},
    ],
}

@app.route('/manifest.webmanifest')
def pwa_manifest():
    """v3.7: PWA manifest (installable on mobile / Chromebook)."""
    resp = jsonify(_PWA_MANIFEST)
    resp.headers['Content-Type'] = 'application/manifest+json'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/health')
def health():
    info = auth.system_health()
    info.update({'streaming': streaming,
                 'uptime': int(time.time()-stream_start_time) if stream_start_time else 0,
                 'auth_enabled': auth.auth_enabled()})
    # v3.4: disk space for recordings dir — fail-soft warning if < 500MB free
    try:
        rec_dir = os.path.expanduser('~/mario_recordings')
        check_dir = rec_dir if os.path.isdir(rec_dir) else os.path.expanduser('~')
        st = os.statvfs(check_dir)
        free_mb = (st.f_bavail * st.f_frsize) // (1024*1024)
        info['disk_free_mb'] = free_mb
        info['disk_warning'] = free_mb < 500
        if free_mb < 100:
            info['ok'] = False
    except Exception as e:
        info['disk_error'] = str(e)
    return jsonify(info), (200 if info.get('ok') else 503)


# ── Auth + CSRF gate ───────────────────────────────────────────────
# When MARIO_PASSWORD is set, every endpoint requires HTTP Basic Auth
# EXCEPT /health and /metrics (so monitoring tools can ping without creds).
# CSRF: when MARIO_STRICT_ORIGIN=1 (default 0), mutating requests must come
# from a same-origin page or an allow-listed origin — protects against
# browser-based cross-site forgery while Basic-Auth is cached.
STRICT_ORIGIN = os.environ.get('MARIO_STRICT_ORIGIN', '0') == '1'
ALLOWED_ORIGINS = {o.strip().lower() for o in
                   os.environ.get('MARIO_ALLOWED_ORIGINS','').split(',') if o.strip()}
_PUBLIC_ENDPOINTS = {'health', 'prometheus_metrics', 'csrf_token', 'pwa_manifest',
                     'login_get', 'login_post', 'logout', 'static'}

# v3.3: double-submit CSRF cookie.
CSRF_COOKIE = 'mario_csrf'
CSRF_HEADER = 'X-CSRF-Token'
CSRF_ENABLED = os.environ.get('MARIO_CSRF', '1') == '1'

def _origin_ok():
    if not STRICT_ORIGIN:
        return True
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    origin = request.headers.get('Origin') or request.headers.get('Referer', '')
    if not origin:
        return bool(request.authorization)
    try:
        netloc = urlparse(origin).netloc.lower()
    except Exception:
        return False
    if netloc == request.host.lower():
        return True
    return netloc in ALLOWED_ORIGINS

def _csrf_ok():
    """For mutating requests: header X-CSRF-Token (or form 'csrf_token') must
    match the cookie. CLI clients can opt out via X-Mario-Cli: 1 + Basic-Auth."""
    if not CSRF_ENABLED:
        return True
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    if request.headers.get('X-Mario-Cli') == '1' and request.authorization:
        return True
    cookie = request.cookies.get(CSRF_COOKIE, '')
    header = request.headers.get(CSRF_HEADER, '') or (request.form.get('csrf_token', '') if request.form else '')
    if not cookie or not header:
        return False
    return hmac.compare_digest(cookie, header)

@app.route('/api/csrf')
def csrf_token():
    tok = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    resp = jsonify({'token': tok})
    resp.set_cookie(CSRF_COOKIE, tok, samesite='Lax',
                    secure=request.is_secure, httponly=False, max_age=86400)
    return resp


# ── Login / logout (replaces browser Basic-Auth popup) ────────────────────────
# In-memory rate limiter for failed login attempts (per IP).
_LOGIN_ATTEMPTS = defaultdict(deque)
_LOGIN_MAX_ATTEMPTS = int(os.environ.get('MARIO_LOGIN_MAX_ATTEMPTS', '8'))
_LOGIN_WINDOW_SEC  = int(os.environ.get('MARIO_LOGIN_WINDOW_SEC', '900'))  # 15 min

def _client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()

def _login_blocked(ip):
    now = time.time()
    q = _LOGIN_ATTEMPTS[ip]
    while q and now - q[0] > _LOGIN_WINDOW_SEC:
        q.popleft()
    return len(q) >= _LOGIN_MAX_ATTEMPTS

def _login_record_failure(ip):
    _LOGIN_ATTEMPTS[ip].append(time.time())

def _login_clear(ip):
    _LOGIN_ATTEMPTS.pop(ip, None)

def _is_logged_in():
    if session.get('mario_user'):
        return True
    # CLI / scripted clients can still use Basic-Auth
    a = request.authorization
    if a and a.username and a.password:
        user = os.environ.get('MARIO_USER', 'mario')
        pw   = (os.environ.get('MARIO_PASSWORD', '') or '').strip()
        if pw and hmac.compare_digest(str(a.username), user) \
              and hmac.compare_digest(str(a.password), pw):
            return True
    return False

def _wants_json():
    if request.path.startswith('/api/') or request.path in ('/metrics', '/health'):
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept

@app.route('/login', methods=['GET'])
def login_get():
    # Ensure a CSRF cookie exists so the form can post safely.
    tok = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    next_url = request.args.get('next', '/')
    # only allow relative next URLs
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = '/'
    if not auth.auth_enabled():
        # Auth is disabled — no point in showing the page
        return redirect(next_url)
    if session.get('mario_user'):
        return redirect(next_url)
    resp = make_response(render_template('login.html',
                                         csrf_token=tok,
                                         next_url=next_url,
                                         error=None,
                                         version=MARIO_VERSION))
    resp.set_cookie(CSRF_COOKIE, tok, samesite='Lax',
                    secure=request.is_secure, httponly=False, max_age=86400)
    return resp

@app.route('/login', methods=['POST'])
def login_post():
    ip = _client_ip()
    next_url = request.form.get('next', '/') or '/'
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = '/'
    tok = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)

    def _render_err(msg, status=401):
        resp = make_response(render_template('login.html',
                                             csrf_token=tok,
                                             next_url=next_url,
                                             error=msg,
                                             version=MARIO_VERSION), status)
        resp.set_cookie(CSRF_COOKIE, tok, samesite='Lax',
                        secure=request.is_secure, httponly=False, max_age=86400)
        return resp

    if _login_blocked(ip):
        log.warning('login rate-limited ip=%s', ip)
        return _render_err('Too many failed attempts. Please wait and try again.', 429)

    username = (request.form.get('username') or '').strip()
    password = (request.form.get('password') or '').strip()
    expected_user = os.environ.get('MARIO_USER', 'mario')
    expected_pw   = (os.environ.get('MARIO_PASSWORD', '') or '').strip()

    if not expected_pw:
        # Auth disabled at server level — just send them through.
        return redirect(next_url)

    ok = (hmac.compare_digest(username, expected_user) and
          hmac.compare_digest(password, expected_pw))
    if not ok:
        _login_record_failure(ip)
        log.info('login failed user=%r ip=%s', username, ip)
        return _render_err('Invalid username or password.', 401)

    _login_clear(ip)
    session.clear()
    session['mario_user'] = expected_user
    session['login_ts']   = int(time.time())
    session.permanent = True
    log.info('login ok user=%s ip=%s', expected_user, ip)
    return redirect(next_url)

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect('/login')


@app.before_request
def _auth_gate():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if not _origin_ok():
        return jsonify({'error': 'Cross-origin request blocked'}), 403
    if not _csrf_ok():
        return jsonify({'error': 'CSRF token missing or invalid',
                        'hint': 'GET /api/csrf then send X-CSRF-Token header'}), 403
    if not auth.auth_enabled():
        return None
    if _is_logged_in():
        return None
    # Not authenticated: HTML → /login redirect; API → JSON 401 (NO Basic popup).
    if _wants_json():
        return jsonify({'error': 'Login required',
                        'login_url': '/login'}), 401
    nxt = request.full_path if request.query_string else request.path
    return redirect('/login?next=' + (nxt if nxt.startswith('/') else '/'))


# v3.7: minimal security headers on every response.
@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy',
        'camera=(), microphone=(), geolocation=(), interest-cohort=()')
    ctype = resp.headers.get('Content-Type', '')
    if ctype.startswith('text/html'):
        resp.headers.setdefault('Content-Security-Policy',
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'")
    return resp


# ── Scan ──────────────────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
@auth.rate_limit(per_minute=30)
def scan_folder():
    data=request.json or {}; folder=str(data.get('folder','')).strip()
    scan_type=data.get('type','video'); recursive=bool(data.get('recursive',False))
    if not folder or not os.path.isdir(folder):
        return jsonify({'error':'Folder not found'}),400
    # SECURITY: confine scanning to MARIO_SCAN_ROOTS (default: common media dirs).
    if not _path_in_allowlist(folder):
        return jsonify({'error': 'Folder not in MARIO_SCAN_ROOTS allowlist',
                        'allowed_roots': SCAN_ROOTS}), 403
    exts=AUDIO_EXTENSIONS if scan_type=='audio' else VIDEO_EXTENSIONS
    files=[]
    if recursive:
        for ext in exts:
            # Phase 4A fix: VIDEO_EXTENSIONS items are like '*.mp4'. rglob expects
            # the full glob pattern. Previously ext[2:] stripped '*.' → 'mp4',
            # which only matched a file literally named "mp4". Use ext as-is.
            files+=[str(p) for p in Path(folder).rglob(ext)]
            files+=[str(p) for p in Path(folder).rglob(ext.upper())]
    else:
        for ext in exts:
            files+=glob.glob(os.path.join(folder,ext))
            files+=glob.glob(os.path.join(folder,ext.upper()))
    files=sorted(set(files))
    # Belt-and-suspenders: drop anything that escaped via symlinks
    files=[f for f in files if _path_in_allowlist(f)]
    result=[]
    for f in files:
        if scan_type=='audio':
            result.append({'path':f,'name':os.path.basename(f),
                           'info':{'size_str':fmt_size(os.path.getsize(f)),'duration_str':'—'}})
        else:
            result.append({'path':f,'name':os.path.basename(f),'info':get_video_info(f)})
    return jsonify({'files':result,'count':len(result)})


# ── Thumbnail ─────────────────────────────────────────────────────────────────

@app.route('/api/thumb', methods=['POST'])
def make_thumb():
    path=request.json.get('path','')
    if not isinstance(path, str) or not path:
        return jsonify({'error':'path required'}),400
    if not _path_in_allowlist(path):
        return jsonify({'error':'Path not in MARIO_SCAN_ROOTS allowlist'}),403
    if os.path.splitext(path)[1].lower() not in _VIDEO_EXT_SET:
        return jsonify({'error':'unsupported extension'}),400
    if not os.path.exists(path): return jsonify({'error':'File not found'}),404
    data=get_or_make_thumb(path)
    return jsonify({'thumb':data})


# ── Stream ────────────────────────────────────────────────────────────────────

@app.route('/api/stream/start', methods=['POST'])
@auth.rate_limit(per_minute=20)
def start_stream():
    """Start the stream.

    Phase 2: the frontend now sends `playlist` inside the request body so
    backend state never lags behind the UI. We accept it, validate every
    item, replace global playlist + persist, then continue with the
    existing logic. Falls back to the existing backend playlist if no
    payload list is provided (backwards-compatible with old clients)."""
    global last_stream_params, auto_restart, restart_count, shuffle_mode, playlist, restart_attempts, ffmpeg_process, streaming

    body = request.json or {}

    # ── Phase 1: idempotent guard (BEFORE validation/normalization/FFmpeg) ──
    # While a stream is actually running, repeated POSTs return 200
    # already_running=true and do nothing — no glitch, no re-normalize.
    with stream_lock:
        proc = ffmpeg_process
        if streaming and proc is not None and proc.poll() is None:
            log.info('[START] already running pid=%s', proc.pid)
            try: _append_log(f'[START] already running pid={proc.pid} — no-op')
            except Exception: pass
            return jsonify({
                'success': True, 'already_running': True,
                'message': 'Stream already running',
                'pid': proc.pid, 'status': 'live',
            }), 200
        # Stale state: streaming flag set but process is dead → clean & continue.
        if streaming and (proc is None or proc.poll() is not None):
            log.info('[START] clearing stale streaming state')
            ffmpeg_process = None
            streaming = False

    log.info('[START] request received')
    _reset_start_progress(active=True, total=len(playlist or []))
    _set_start_progress(phase='validating', percent=3,
                        message='Validating request…')
    _append_log('[START] validating playlist')

    # ── Optional: accept frontend-owned playlist ─────────────────────────────
    incoming = body.get('playlist')
    if isinstance(incoming, list) and incoming:
        try:
            cleaned = clean_playlist_items(incoming, require_exists=True)
        except ValidationError as e:
            _set_start_progress(active=False, phase='error', error=str(e),
                                message=f'Invalid playlist: {e}')
            return jsonify({'success': False, 'error': str(e)}), 400
        playlist[:] = cleaned
        try: state.save_playlist(playlist)
        except Exception as e: log.warning('save_playlist failed: %s', e)

    _set_start_progress(phase='syncing', percent=6,
                        total=len(playlist or []),
                        message='Syncing playlist…')

    if not playlist:
        _set_start_progress(active=False, phase='error',
                            error='Playlist is empty',
                            message='Playlist is empty')
        return jsonify({'success': False, 'error': 'Playlist is empty'}), 400
    missing=[v['name'] for v in playlist if not os.path.exists(v['path'])]
    if missing:
        msg = f"Missing files: {', '.join(missing)}"
        _set_start_progress(active=False, phase='error',
                            error=msg, message=msg)
        return jsonify({'success': False, 'error': msg}), 400

    try:
        params = validate_stream_params(body)
    except ValidationError as e:
        msg = f'Invalid params: {e}'
        _set_start_progress(active=False, phase='error', error=msg, message=msg)
        return jsonify({'success': False, 'error': msg}), 400

    dev = params.get('device', '/dev/video10')
    if not params.get('rtmp_url') and not os.path.exists(dev):
        msg = f'device not found: {dev}'
        _set_start_progress(active=False, phase='error', error=msg, message=msg)
        return jsonify({'success': False, 'error': msg}), 400

    auto_restart    = params['auto_restart']
    shuffle_mode    = params['shuffle']
    restart_count   = 0
    restart_attempts = 0

    if shuffle_mode:
        if params.get('smart_shuffle'):
            playlist[:] = _smart_shuffle(playlist)
        else:
            random.shuffle(playlist)
        state.save_playlist(playlist)

    _set_start_progress(phase='preparing', percent=8,
                        total=len(playlist or []),
                        message='Checking normalized cache…')

    with stream_lock:
        # Re-check inside the lock — a parallel request may have started
        # the stream while we were validating.
        proc = ffmpeg_process
        if streaming and proc is not None and proc.poll() is None:
            log.info('[START] already running (race) pid=%s', proc.pid)
            _set_start_progress(active=False, phase='live', percent=100,
                                message='Stream already running')
            return jsonify({
                'success': True, 'already_running': True,
                'message': 'Stream already running',
                'pid': proc.pid, 'status': 'live',
            }), 200
        try:
            last_stream_params = params
            log.info('[START] starting normalization / launch')
            res = _launch_ffmpeg(params)
            log.info('[START] launched ffmpeg pid=%s res=%s',
                     ffmpeg_process.pid if ffmpeg_process else None, res)
            _append_log(f'[START] stream started pid={ffmpeg_process.pid if ffmpeg_process else "?"}')
            _set_start_progress(active=False, phase='live', percent=100,
                                message='Stream started',
                                current_clip=None, current_index=0)
            return jsonify({'success':True,'pid':ffmpeg_process.pid,'resolution':res,
                            'shuffled':shuffle_mode,'status':'live'})
        except Exception as e:
            log.exception('stream start failed')
            _set_start_progress(active=False, phase='error',
                                error=str(e), message=f'Start failed: {e}')
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/stream/start-progress', methods=['GET'])
@auth.auth_required
@auth.rate_limit(per_minute=240)
def stream_start_progress():
    """Return the live progress object for the in-flight /api/stream/start
    call. Frontend polls this every ~500ms while preparing. Contains no
    secrets — only basenames and phase/percent."""
    return jsonify({'success': True, 'progress': _snapshot_start_progress()})


@app.route('/api/stream/stop', methods=['POST'])
def stop_stream():
    """Phase 5: terminate ONLY the currently-tracked process, then null the
    global pointer under _ffmpeg_lock so a stale monitor cannot race us."""
    global ffmpeg_process, streaming, auto_restart, stream_start_time
    auto_restart = False
    with _ffmpeg_lock:
        proc = ffmpeg_process
        ffmpeg_process = None
    if proc:
        try: proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
    streaming = False
    stream_start_time = None
    return jsonify({'success': True})


@app.route('/api/stream/skip', methods=['POST'])
@auth.rate_limit(per_minute=30)
def skip_stream():
    """Phase 5: terminate current process under _ffmpeg_lock, clear the
    global pointer so the old monitor sees 'not still current' and exits
    cleanly, then launch the next process. No more zombie monitors flipping
    `streaming = False` on the new stream."""
    global ffmpeg_process, last_stream_params
    if not streaming or not last_stream_params:
        return jsonify({'success': False, 'error': 'Not streaming'}), 400
    target = (current_index + 1) % max(1, len(playlist))
    with stream_lock:
        with _ffmpeg_lock:
            proc = ffmpeg_process
            ffmpeg_process = None
        if proc:
            try: proc.terminate(); proc.wait(timeout=3)
            except Exception:
                try: proc.kill()
                except Exception: pass
        resume = dict(last_stream_params)
        resume['_start_index'] = target
        last_stream_params = resume
        _launch_ffmpeg(resume)
    return jsonify({'success': True, 'index': target})


@app.route('/api/stream/events')
def stream_events():
    """Server-Sent Events: pushes status, stats and new log lines to the
    browser. Bounded by SSE_MAX_CLIENTS to prevent tab-bomb DoS. Uses
    a global monotonic log sequence counter so subscribers never miss or
    duplicate lines (the previous index-into-deque trick broke when the
    deque rolled over its maxlen=200 cap between ticks)."""
    global _sse_clients
    with _sse_lock:
        if _sse_clients >= SSE_MAX_CLIENTS:
            return jsonify({'error': 'SSE client limit reached',
                            'max': SSE_MAX_CLIENTS}), 429
        _sse_clients += 1

    def gen():
        global _sse_clients
        last_seen_seq = 0
        try:
            # Initial backlog: send last 60 lines as the starting offset
            with _state_lock:
                last_seen_seq = ffmpeg_logs_seq
                backlog = list(ffmpeg_logs)[-60:]
            if backlog:
                yield f'event: logs\ndata: {json.dumps(backlog)}\n\n'
            while True:
                with _state_lock:
                    uptime = int(time.time()-stream_start_time) if stream_start_time else 0
                    # v3.4: clamp current_index — if playlist shrank between
                    # ticks we'd otherwise emit a stale index pointing past the end.
                    _ci = current_index if (playlist and 0 <= current_index < len(playlist)) else 0
                    payload = {
                        'streaming':     streaming,
                        'uptime':        uptime,
                        'current_index': _ci,
                        'current_name':  playlist[_ci]['name'] if playlist else '',
                        'stats':         dict(stream_stats),
                        'restart_count': restart_count,
                    }
                    seq_now  = ffmpeg_logs_seq
                    new_count = max(0, seq_now - last_seen_seq)
                    # Cap at deque maxlen — if we missed more we just send tail
                    new_logs  = list(ffmpeg_logs)[-min(new_count, len(ffmpeg_logs)):] if new_count else []
                    last_seen_seq = seq_now
                yield f'event: tick\ndata: {json.dumps(payload)}\n\n'
                if new_logs:
                    yield f'event: logs\ndata: {json.dumps(new_logs)}\n\n'
                time.sleep(1)
        except (GeneratorExit, BrokenPipeError):
            pass
        finally:
            with _sse_lock:
                _sse_clients = max(0, _sse_clients - 1)

    return Response(stream_with_context(gen()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/api/stream/logs',  methods=['GET'])
def stream_logs():
    return jsonify({'logs':list(ffmpeg_logs)[-int(request.args.get('n',60)):]})

@app.route('/api/stream/stats', methods=['GET'])
def get_stream_stats():
    uptime=int(time.time()-stream_start_time) if stream_start_time else 0
    return jsonify({**stream_stats,'uptime':uptime,'streaming':streaming,
                    'current_index':current_index,
                    'current_name':playlist[current_index]['name'] if playlist and 0<=current_index<len(playlist) else ''})

@app.route('/api/status', methods=['GET'])
def get_status():
    sys = get_system_status()
    uptime = int(time.time()-stream_start_time) if stream_start_time else 0
    return jsonify({'streaming':streaming,'v4l2_available':sys['v4l2'],
                    'virtual_devices':sys['devices'],
                    'ffmpeg_available':sys['ffmpeg'],'playlist_count':len(playlist),
                    'current_index':current_index,'uptime':uptime,
                    'auto_restart':auto_restart,'restart_count':restart_count,
                    'shuffle_mode':shuffle_mode})


# ── Playlist ──────────────────────────────────────────────────────────────────

@app.route('/api/playlist',         methods=['GET'])
def get_playlist():
    return jsonify({'playlist':playlist,'current_index':current_index,'streaming':streaming})

@app.route('/api/playlist/add',     methods=['POST'])
def add_to_playlist():
    v = (request.json or {}).get('video') or {}
    path = str(v.get('path',''))
    if not path or not os.path.isfile(path):
        return jsonify({'error': 'File not found'}), 400
    # SECURITY: enforce path allowlist + media extension
    if not _path_in_allowlist(path):
        return jsonify({'error': 'Path not in MARIO_SCAN_ROOTS allowlist'}), 403
    if os.path.splitext(path)[1].lower() not in _MEDIA_EXT_SET:
        return jsonify({'error': 'Unsupported file extension'}), 400
    # Refuse symlinks pointing outside the allowlist (already covered above
    # by realpath check, but reject obvious traversal in submitted path).
    if '..' in path.split(os.sep):
        return jsonify({'error': 'Invalid path'}), 400
    playlist.append(v); state.save_playlist(playlist)
    return jsonify({'success': True})

@app.route('/api/playlist/remove',  methods=['POST'])
def remove_from_playlist():
    idx=request.json.get('index')
    if idx is not None and 0<=idx<len(playlist): playlist.pop(idx)
    state.save_playlist(playlist)
    return jsonify({'success':True})

@app.route('/api/playlist/reorder', methods=['POST'])
def reorder_playlist():
    """Reorder/replace playlist. v3.3: validates every submitted path is
    either already in the current playlist OR inside the SCAN_ROOTS
    allowlist — closes a bypass where reorder could inject arbitrary paths."""
    global playlist
    new_list = (request.json or {}).get('playlist', [])
    if not isinstance(new_list, list):
        return jsonify({'error': 'playlist must be a list'}), 400
    if len(new_list) > 2000:
        return jsonify({'error': 'playlist too large (max 2000)'}), 400
    existing = {v.get('path') for v in playlist}
    cleaned = []
    for v in new_list:
        if not isinstance(v, dict): continue
        p = str(v.get('path', ''))
        if not p: continue
        if p not in existing and not _path_in_allowlist(p):
            return jsonify({'error': f'Path not allowed: {p}'}), 403
        cleaned.append(v)
    playlist = cleaned
    try:
        state.save_playlist(playlist)
    except sqlite3.OperationalError as e:
        log.error('save_playlist DB error: %s (DB_PATH=%s)', e, state.DB_PATH)
        return jsonify({
            'error': 'Database is not writable',
            'detail': str(e),
            'db_path': state.DB_PATH,
        }), 500
    except Exception as e:
        log.exception('save_playlist unexpected error')
        return jsonify({'error': 'Failed to save playlist', 'detail': str(e)}), 500
    return jsonify({'success': True, 'count': len(playlist)})

@app.route('/api/playlist/clear',   methods=['POST'])
def clear_playlist():
    global playlist, current_index
    with _state_lock:
        playlist = []
        current_index = 0
    state.save_playlist(playlist)
    return jsonify({'success':True})


# ── Live Preview (MJPEG from virtual camera) ─────────────────────────────────

# ── MJPEG singleton broadcaster ──────────────────────────────────────────────
# v3.3: ONE ffmpeg reads /dev/video10 and N browser subscribers receive the
# frames. Previously each /api/preview/mjpeg request spawned a new ffmpeg —
# 5 tabs = 5 readers contending on the device. Now we ref-count and tear
# the reader down when the last subscriber leaves.
_mjpeg_lock        = threading.Lock()
_mjpeg_subscribers = 0
_mjpeg_thread      = None
_mjpeg_frame       = {'jpg': None, 'cond': threading.Condition()}
_mjpeg_stop        = threading.Event()
_mjpeg_device      = None
MJPEG_MAX_CLIENTS  = int(os.environ.get('MARIO_MJPEG_MAX', '10'))

def _mjpeg_reader_loop(device):
    cmd = ['ffmpeg','-loglevel','quiet',
           '-f','v4l2','-i', device,
           '-vf','scale=480:270,fps=8',
           '-f','mjpeg','-q:v','7','-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        buf = b''
        while not _mjpeg_stop.is_set():
            chunk = p.stdout.read(8192)
            if not chunk: break
            buf += chunk
            while True:
                s = buf.find(b'\xff\xd8')
                e = buf.find(b'\xff\xd9', s+2) if s >= 0 else -1
                if s < 0 or e < 0: break
                jpg = buf[s:e+2]
                buf = buf[e+2:]
                with _mjpeg_frame['cond']:
                    _mjpeg_frame['jpg'] = jpg
                    _mjpeg_frame['cond'].notify_all()
    finally:
        try: p.terminate()
        except Exception: pass
        log.info('mjpeg reader stopped (device=%s)', device)

@app.route('/api/preview/mjpeg')
def preview_mjpeg():
    """Multi-subscriber MJPEG preview. Single shared ffmpeg per device."""
    global _mjpeg_subscribers, _mjpeg_thread, _mjpeg_device
    device = request.args.get('device', '/dev/video10')
    if not _DEVICE_RE.match(device) or not os.path.exists(device):
        return jsonify({'error': 'invalid or missing device'}), 400

    with _mjpeg_lock:
        if _mjpeg_subscribers >= MJPEG_MAX_CLIENTS:
            return jsonify({'error': 'MJPEG client limit reached',
                            'max': MJPEG_MAX_CLIENTS}), 429
        if _mjpeg_thread is None or not _mjpeg_thread.is_alive() or _mjpeg_device != device:
            _mjpeg_stop.set()
            if _mjpeg_thread and _mjpeg_thread.is_alive():
                _mjpeg_thread.join(timeout=2)
            _mjpeg_stop.clear()
            _mjpeg_device = device
            _mjpeg_thread = threading.Thread(
                target=_mjpeg_reader_loop, args=(device,), daemon=True)
            _mjpeg_thread.start()
            log.info('mjpeg reader started (device=%s)', device)
        _mjpeg_subscribers += 1

    def gen():
        global _mjpeg_subscribers
        try:
            last_frame_id = id(None)
            while True:
                with _mjpeg_frame['cond']:
                    if not _mjpeg_frame['cond'].wait(timeout=5):
                        continue
                    jpg = _mjpeg_frame['jpg']
                if not jpg or id(jpg) == last_frame_id:
                    continue
                last_frame_id = id(jpg)
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                       + jpg + b'\r\n')
        except (GeneratorExit, BrokenPipeError):
            pass
        finally:
            with _mjpeg_lock:
                _mjpeg_subscribers = max(0, _mjpeg_subscribers - 1)
                if _mjpeg_subscribers == 0:
                    _mjpeg_stop.set()

    return Response(stream_with_context(gen()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# ── Saved playlists ───────────────────────────────────────────────────────────

@app.route('/api/playlists',        methods=['GET'])
def list_playlists():
    return jsonify({'playlists':sorted(f[:-5] for f in os.listdir(PLAYLISTS_DIR) if f.endswith('.json'))})

@app.route('/api/playlists/save',   methods=['POST'])
def save_playlist_route():
    """Save a named playlist to disk. Sanitizes the name to prevent traversal."""
    data=request.json or {}; name=str(data.get('name','')).strip()
    if not name: return jsonify({'error':'Name required'}),400
    # Block path traversal / shell-unfriendly chars
    if not re.match(r'^[A-Za-z0-9 _.\-]{1,80}$', name):
        return jsonify({'error':'Invalid name (A-Z, 0-9, _.- only, ≤80 chars)'}),400
    items = data.get('items', playlist)
    path=os.path.join(PLAYLISTS_DIR,f'{name}.json')
    with open(path,'w') as f: json.dump({'name':name,'items':items},f,indent=2)
    return jsonify({'success':True,'count':len(items)})

@app.route('/api/playlists/load',   methods=['POST'])
def load_playlist_route():
    global playlist
    name=str((request.json or {}).get('name','')).strip()
    if not re.match(r'^[A-Za-z0-9 _.\-]{1,80}$', name):
        return jsonify({'error':'Invalid name'}),400
    path=os.path.join(PLAYLISTS_DIR,f'{name}.json')
    if not os.path.exists(path): return jsonify({'error':'Not found'}),404
    try:
        with open(path) as f: data=json.load(f)
    except (OSError, ValueError) as e:
        return jsonify({'error': f'Failed to read playlist: {e}'}), 400
    if not isinstance(data, dict):
        return jsonify({'error': 'Malformed playlist file'}), 400
    # Phase 8: never trust on-disk playlist JSON — re-validate every item
    # through the same allowlist/extension/type rules used by /stream/start.
    try:
        cleaned = clean_playlist_items(data.get('items', []), require_exists=True)
    except ValidationError as e:
        return jsonify({'error': f'Invalid playlist: {e}'}), 400
    playlist[:] = cleaned
    state.save_playlist(playlist)
    return jsonify({'success':True,'playlist':playlist})


@app.route('/api/playlists/delete', methods=['POST'])
def delete_playlist():
    name=str((request.json or {}).get('name','')).strip()
    if not re.match(r'^[A-Za-z0-9 _.\-]{1,80}$', name):
        return jsonify({'error':'Invalid name'}),400
    path=os.path.join(PLAYLISTS_DIR,f'{name}.json')
    if os.path.exists(path): os.remove(path)
    return jsonify({'success':True})


# ── Preset Profiles ───────────────────────────────────────────────────────────

@app.route('/api/profiles',         methods=['GET'])
def list_profiles():
    profiles=[]
    for f in sorted(os.listdir(PROFILES_DIR)):
        if f.endswith('.json'):
            try:
                with open(os.path.join(PROFILES_DIR,f)) as fh:
                    profiles.append(json.load(fh))
            except: pass
    return jsonify({'profiles':profiles})

_PROFILE_NAME_RE = re.compile(r'^[A-Za-z0-9 _.\-]{1,80}$')

@app.route('/api/profiles/save',    methods=['POST'])
def save_profile():
    d = request.json or {}
    name = str(d.get('name','')).strip()
    if not name: return jsonify({'error':'Name required'}),400
    if not _PROFILE_NAME_RE.match(name):
        return jsonify({'error':'Invalid name (A-Z, 0-9, _.- only, ≤80 chars)'}),400
    profile = {'name':name,'created':time.strftime('%Y-%m-%d %H:%M'),
               'settings':d.get('settings',{})}
    path = os.path.join(PROFILES_DIR, f'{name}.json')
    with open(path,'w') as f: json.dump(profile,f,indent=2)
    return jsonify({'success':True,'profile':profile})

@app.route('/api/profiles/delete',  methods=['POST'])
def delete_profile():
    name = str((request.json or {}).get('name','')).strip()
    if not _PROFILE_NAME_RE.match(name):
        return jsonify({'error':'Invalid name'}),400
    path = os.path.join(PROFILES_DIR, f'{name}.json')
    if os.path.exists(path): os.remove(path)
    return jsonify({'success':True})


# ── Stream History ────────────────────────────────────────────────────────────

@app.route('/api/history',          methods=['GET'])
def get_history():
    h=_load_history(); return jsonify({'history':list(reversed(h))})

@app.route('/api/history/clear',    methods=['POST'])
def clear_history():
    _save_history([]); return jsonify({'success':True})


# ── Per-clip analytics ────────────────────────────────────────────────────────

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    rows = state.get_clip_stats()
    for r in rows:
        r['total_str'] = fmt_dur(r.get('total_secs', 0))
    return jsonify({'clips': rows})

@app.route('/api/analytics/reset', methods=['POST'])
def reset_analytics():
    state.reset_clip_stats(); return jsonify({'success': True})
# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduled_jobs=[]; _sched_lock=threading.Lock(); _sched_thread=None

def _scheduler_loop():
    """v3.3: precise minute-boundary wake-up. The previous loop slept 30s
    which could miss the matching minute entirely if it landed mid-minute."""
    last_fired_key = None  # (hhmm, day) — guards against double-fire if
                           # multiple wake-ups occur in the same minute
    while True:
        now       = time.localtime()
        now_hhmm  = f'{now.tm_hour:02d}:{now.tm_min:02d}'
        now_day   = now.tm_wday

        to_fire = []
        key = (now_hhmm, now_day)
        if key != last_fired_key:
            with _sched_lock:
                for job in scheduled_jobs:
                    if not job.get('active'):           continue
                    if job['time'] != now_hhmm:         continue
                    if job.get('days') and now_day not in job['days']: continue
                    if streaming:                       continue
                    to_fire.append(job)
            if to_fire:
                last_fired_key = key

        for job in to_fire:
            _append_log(f'[scheduler] Firing "{job["name"]}" at {now_hhmm}')
            log.info('scheduler firing job=%s', job['name'])
            try:
                with stream_lock:
                    global last_stream_params, auto_restart, restart_count, restart_attempts
                    last_stream_params = job['params']
                    auto_restart       = job['params'].get('auto_restart', False)
                    restart_count      = 0
                    restart_attempts   = 0
                    _launch_ffmpeg(job['params'])
            except Exception as e:
                _append_log(f'[scheduler] Failed: {e}')
                log.exception('scheduler job failed')

        # Sleep precisely until the start of the next minute (+1s margin).
        delay = 60 - now.tm_sec + 1
        time.sleep(max(2, delay))

def _ensure_scheduler():
    global _sched_thread
    if _sched_thread is None or not _sched_thread.is_alive():
        _sched_thread=threading.Thread(target=_scheduler_loop,daemon=True); _sched_thread.start()

@app.route('/api/scheduler',          methods=['GET'])
def list_jobs(): return jsonify({'jobs':scheduled_jobs})

@app.route('/api/scheduler/add',      methods=['POST'])
def add_job():
    try:
        v = validate_job(request.json or {})
    except ValidationError as e:
        return jsonify({'error': str(e)}), 400
    job = {'id': int(time.time()*1000), 'active': True, **v}
    with _sched_lock:
        scheduled_jobs.append(job)
        state.save_jobs(scheduled_jobs)
    _ensure_scheduler()
    return jsonify({'success':True,'job':job})

@app.route('/api/scheduler/toggle',   methods=['POST'])
def toggle_job():
    jid=request.json.get('id')
    with _sched_lock:
        for job in scheduled_jobs:
            if job['id']==jid:
                job['active']=not job['active']
                state.save_jobs(scheduled_jobs)
                return jsonify({'success':True,'active':job['active']})
    return jsonify({'error':'Not found'}),404

@app.route('/api/scheduler/delete',   methods=['POST'])
def delete_job():
    jid=request.json.get('id')
    with _sched_lock:
        idx=next((i for i,j in enumerate(scheduled_jobs) if j['id']==jid),None)
        if idx is not None: scheduled_jobs.pop(idx)
        state.save_jobs(scheduled_jobs)
    return jsonify({'success':True})


# ── (19) Version / auto-update checker ───────────────────────────────────────
MARIO_VERSION = "3.9.17"
GITHUB_REPO   = os.environ.get('MARIO_GITHUB_REPO', '')   # e.g. "user/mario-stream"
_ver_cache    = {'t': 0, 'data': None}

@app.route('/api/version')
def version_info():
    return jsonify({'version': MARIO_VERSION, 'repo': GITHUB_REPO})

@app.route('/api/version/check')
def version_check():
    """Compares MARIO_VERSION to the latest GitHub release tag (cached 1h)."""
    import urllib.request
    if not GITHUB_REPO:
        return jsonify({'error': 'MARIO_GITHUB_REPO not set'}), 400
    now = time.time()
    if _ver_cache['data'] and now - _ver_cache['t'] < 3600:
        return jsonify(_ver_cache['data'])
    try:
        url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
        req = urllib.request.Request(url, headers={'User-Agent':'mario-stream'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        latest = (data.get('tag_name','') or '').lstrip('v')
        result = {'current': MARIO_VERSION, 'latest': latest,
                  'update_available': bool(latest and latest != MARIO_VERSION),
                  'url': data.get('html_url','')}
        _ver_cache.update({'t': now, 'data': result})
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ── (19b) Safe self-update (git fast-forward only, opt-in) ──────────────────
ALLOW_AUTO_UPDATE = os.environ.get('MARIO_ALLOW_AUTO_UPDATE', '0') == '1'
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def _git(*args, timeout=30):
    # GIT_TERMINAL_PROMPT=0 → git fails fast instead of hanging on a
    # credential prompt; GIT_ASKPASS=true ensures no helper kicks in either.
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_ASKPASS']         = 'true'
    return subprocess.run(['git', '-C', PROJECT_DIR, *args],
                          capture_output=True, text=True, timeout=timeout, env=env)

@app.route('/api/version/apply', methods=['POST'])
@auth.rate_limit(per_minute=2)
def version_apply():
    """Safely pulls the latest release via git fast-forward.

    Safety guards:
      - Opt-in only:    MARIO_ALLOW_AUTO_UPDATE=1
      - Repo required:  MARIO_GITHUB_REPO must be set
      - Must be inside a clean git checkout (no uncommitted changes)
      - Remote must match MARIO_GITHUB_REPO (prevents wrong-origin attacks)
      - Fast-forward pull only (no merge commits, no force, no rebase)
      - Auth-gated (global Basic-Auth) + rate-limited (2/min)
      - Does NOT auto-restart — operator restarts the service manually.
    """
    if not ALLOW_AUTO_UPDATE:
        return jsonify({'error': 'Auto-update disabled. Set MARIO_ALLOW_AUTO_UPDATE=1 to enable.'}), 403
    if not GITHUB_REPO:
        return jsonify({'error': 'MARIO_GITHUB_REPO not set'}), 400

    # 1. Verify we're in a git repo
    r = _git('rev-parse', '--is-inside-work-tree')
    if r.returncode != 0 or r.stdout.strip() != 'true':
        return jsonify({'error': 'Project is not a git checkout'}), 400

    # 2. Verify remote matches the configured repo (defense in depth)
    r = _git('remote', 'get-url', 'origin')
    if r.returncode != 0:
        return jsonify({'error': 'No git remote "origin" configured'}), 400
    remote_url = r.stdout.strip().lower()
    expected = GITHUB_REPO.lower()
    if expected not in remote_url:
        return jsonify({'error': f'Remote origin ({remote_url}) does not match MARIO_GITHUB_REPO ({GITHUB_REPO})'}), 400

    # 3. Refuse if working tree is dirty
    r = _git('status', '--porcelain')
    if r.stdout.strip():
        return jsonify({'error': 'Working tree has uncommitted changes. Refusing to update.',
                        'dirty': r.stdout.splitlines()[:20]}), 409

    # 4. Fetch + fast-forward only
    before = _git('rev-parse', 'HEAD').stdout.strip()
    f = _git('fetch', '--tags', '--prune', 'origin', timeout=60)
    if f.returncode != 0:
        return jsonify({'error': 'git fetch failed', 'detail': f.stderr[:500]}), 502

    p = _git('pull', '--ff-only', timeout=60)
    if p.returncode != 0:
        return jsonify({'error': 'Fast-forward pull failed (diverged history?)',
                        'detail': p.stderr[:500]}), 409
    after = _git('rev-parse', 'HEAD').stdout.strip()

    return jsonify({
        'success': True,
        'updated': before != after,
        'before': before[:12],
        'after':  after[:12],
        'message': p.stdout.strip()[:500],
        'restart_required': before != after,
        'restart_hint': 'sudo systemctl restart mario@$USER  # or re-run python app.py',
    })


# ── (16) Prometheus metrics ──────────────────────────────────────────────────
@app.route('/metrics')
def prometheus_metrics():
    uptime = int(time.time()-stream_start_time) if stream_start_time else 0
    lines = [
        '# HELP mario_streaming 1 if ffmpeg is currently streaming',
        '# TYPE mario_streaming gauge',
        f'mario_streaming {1 if streaming else 0}',
        '# HELP mario_uptime_seconds Current stream uptime',
        '# TYPE mario_uptime_seconds gauge',
        f'mario_uptime_seconds {uptime}',
        '# HELP mario_fps Current encode fps',
        '# TYPE mario_fps gauge',
        f'mario_fps {float(stream_stats.get("fps",0) or 0)}',
        '# HELP mario_frames_total Frames encoded in current session',
        '# TYPE mario_frames_total counter',
        f'mario_frames_total {int(stream_stats.get("frames",0) or 0)}',
        '# HELP mario_frames_dropped_total Dropped frames in current session',
        '# TYPE mario_frames_dropped_total counter',
        f'mario_frames_dropped_total {int(stream_stats.get("dropped",0) or 0)}',
        '# HELP mario_restart_count Auto-restart count since last start',
        '# TYPE mario_restart_count counter',
        f'mario_restart_count {restart_count}',
        '# HELP mario_playlist_size Number of clips in current playlist',
        '# TYPE mario_playlist_size gauge',
        f'mario_playlist_size {len(playlist)}',
        '# HELP mario_current_index Currently playing clip index',
        '# TYPE mario_current_index gauge',
        f'mario_current_index {current_index}',
    ]
    return Response('\n'.join(lines)+'\n', mimetype='text/plain; version=0.0.4')


# ── (17) Backup / Restore ────────────────────────────────────────────────────
@app.route('/api/backup/export')
def backup_export():
    """Returns a ZIP with state.db + saved playlists + profiles + history."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # v3.8.3: honour MARIO_DB_PATH (Docker uses /home/mario/mario_data/...)
        db = state.DB_PATH
        if os.path.exists(db):
            z.write(db, 'mario_state.db')
        for d, label in [(PLAYLISTS_DIR,'playlists'), (PROFILES_DIR,'profiles')]:
            for fn in os.listdir(d):
                if fn.endswith('.json'):
                    z.write(os.path.join(d,fn), f'{label}/{fn}')
        if os.path.exists(HISTORY_FILE):
            z.write(HISTORY_FILE, 'history.json')
        z.writestr('version.txt', MARIO_VERSION)
    buf.seek(0)
    ts = time.strftime('%Y%m%d_%H%M%S')
    return Response(buf.getvalue(), mimetype='application/zip',
                    headers={'Content-Disposition':
                             f'attachment; filename="mario_backup_{ts}.zip"'})

# Zip-bomb / restore limits
BACKUP_MAX_UNCOMPRESSED = int(os.environ.get('MARIO_BACKUP_MAX_MB', '50')) * 1024 * 1024
BACKUP_MAX_FILES        = int(os.environ.get('MARIO_BACKUP_MAX_FILES', '500'))

@app.route('/api/backup/import', methods=['POST'])
@auth.rate_limit(per_minute=5)
def backup_import():
    """Restore from a ZIP backup. v3.3 hardening:
      * stops any running stream before overwriting mario_state.db
        (otherwise the live SQLite connection sees corruption);
      * caps total uncompressed size + file count (zip-bomb DoS);
      * rejects abs paths, traversal, drive letters, and per-file size > limit;
      * writes via tempfile + atomic rename so a partial decode never leaves
        a half-overwritten DB on disk.
    """
    import io, zipfile, tempfile
    f = request.files.get('file')
    if not f: return jsonify({'error':'no file uploaded'}), 400
    try:
        raw = f.read()
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return jsonify({'error':'not a valid zip'}), 400

    # Pre-flight: zip-bomb checks before any write
    infos = z.infolist()
    if len(infos) > BACKUP_MAX_FILES:
        return jsonify({'error': f'too many files in backup (>{BACKUP_MAX_FILES})'}), 413
    total = sum(i.file_size for i in infos)
    if total > BACKUP_MAX_UNCOMPRESSED:
        return jsonify({'error': f'backup too large uncompressed (>{BACKUP_MAX_UNCOMPRESSED//1024//1024} MB)'}), 413

    # Stop the stream so the live DB connection is released before overwrite
    was_streaming = streaming
    if was_streaming:
        try:
            with stream_lock:
                if ffmpeg_process:
                    try: ffmpeg_process.terminate(); ffmpeg_process.wait(timeout=3)
                    except Exception:
                        try: ffmpeg_process.kill()
                        except Exception: pass
        except Exception as e:
            log.warning('pre-restore stop failed: %s', e)

    def _safe_write(dst, data):
        d = os.path.dirname(dst) or '.'
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.restore_', dir=d)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.replace(tmp, dst)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    restored = []
    for info in infos:
        name = info.filename
        # Reject: abs, traversal, Windows drive, control chars
        if not name or name.startswith('/') or name.startswith('\\'):
            continue
        if '..' in name.replace('\\','/').split('/'):
            continue
        if re.match(r'^[A-Za-z]:', name):
            continue
        if info.file_size > BACKUP_MAX_UNCOMPRESSED:
            continue  # per-file cap
        data = z.read(info)
        if name == 'mario_state.db':
            # v3.8.3: restore into the real configured DB location
            _safe_write(state.DB_PATH, data)
            restored.append(name)
        elif name == 'history.json':
            with _history_lock:
                _safe_write(HISTORY_FILE, data)
            restored.append(name)
        elif name.startswith('playlists/') and name.endswith('.json'):
            base = os.path.basename(name)
            if _PROFILE_NAME_RE.match(base[:-5]):  # reuse regex (same charset)
                _safe_write(os.path.join(PLAYLISTS_DIR, base), data)
                restored.append(name)
        elif name.startswith('profiles/') and name.endswith('.json'):
            base = os.path.basename(name)
            if _PROFILE_NAME_RE.match(base[:-5]):
                _safe_write(os.path.join(PROFILES_DIR, base), data)
                restored.append(name)

    _bootstrap_state()
    log.info('backup restored count=%s files=%s', len(restored), restored)
    return jsonify({'success': True, 'restored': restored,
                    'count': len(restored), 'was_streaming': was_streaming})


# ── (26) Stream snapshot (auto-thumbnail) ────────────────────────────────────
_snapshot_cache = {'t': 0, 'data': None}
_snapshot_lock  = threading.Lock()

@app.route('/api/stream/snapshot.jpg')
def stream_snapshot():
    """Single JPEG grabbed from the virtual camera (cached 5s)."""
    device = request.args.get('device', '/dev/video10')
    if not _DEVICE_RE.match(device) or not os.path.exists(device):
        return jsonify({'error':'invalid or missing device'}), 400
    now = time.time()
    with _snapshot_lock:
        if _snapshot_cache['data'] and now - _snapshot_cache['t'] < 5:
            return Response(_snapshot_cache['data'], mimetype='image/jpeg')
        try:
            r = subprocess.run(
                ['ffmpeg','-loglevel','quiet','-y','-f','v4l2','-i',device,
                 '-frames:v','1','-vf','scale=480:-1','-q:v','5','-f','image2','-'],
                capture_output=True, timeout=6)
            if r.returncode == 0 and r.stdout:
                _snapshot_cache.update({'t': now, 'data': r.stdout})
                return Response(r.stdout, mimetype='image/jpeg')
            return jsonify({'error':'snapshot failed',
                            'stderr': r.stderr[:200].decode('utf-8','ignore')}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500


# ── (v3.3) Recordings rotation ───────────────────────────────────────────────
@app.route('/api/recordings', methods=['GET'])
def list_recordings():
    rec_dir = os.path.expanduser('~/mario_recordings')
    if not os.path.isdir(rec_dir): return jsonify({'recordings': []})
    out = []
    for fn in sorted(os.listdir(rec_dir), reverse=True):
        if not fn.endswith('.mp4'): continue
        p = os.path.join(rec_dir, fn)
        try:
            st = os.stat(p)
            out.append({'name': fn, 'size': st.st_size,
                        'size_str': fmt_size(st.st_size),
                        'mtime': int(st.st_mtime)})
        except OSError: pass
    return jsonify({'recordings': out, 'count': len(out),
                    'total_bytes': sum(r['size'] for r in out)})

@app.route('/api/recordings/cleanup', methods=['POST'])
@auth.rate_limit(per_minute=5)
def cleanup_recordings():
    """Delete old recordings: keep last N (max_keep) AND drop anything
    older than max_age_days. Operator-controlled, never automatic."""
    d = request.json or {}
    max_keep    = max(0, min(1000, int(d.get('max_keep', 20))))
    max_age_days= max(0, min(3650, int(d.get('max_age_days', 30))))
    rec_dir = os.path.expanduser('~/mario_recordings')
    if not os.path.isdir(rec_dir): return jsonify({'deleted': []})
    files = []
    for fn in os.listdir(rec_dir):
        if not fn.endswith('.mp4'): continue
        p = os.path.join(rec_dir, fn)
        try: files.append((os.stat(p).st_mtime, p, fn))
        except OSError: pass
    files.sort(reverse=True)  # newest first
    cutoff  = time.time() - max_age_days * 86400 if max_age_days else 0
    deleted = []
    for i, (mt, p, fn) in enumerate(files):
        too_old = max_age_days and mt < cutoff
        over_keep = i >= max_keep
        if too_old or over_keep:
            try: os.remove(p); deleted.append(fn)
            except OSError as e: log.warning('cleanup failed %s: %s', fn, e)
    log.info('recordings cleanup deleted=%d', len(deleted))
    return jsonify({'success': True, 'deleted': deleted, 'count': len(deleted)})


# ── (27) Silent-gap detection per clip ───────────────────────────────────────
@app.route('/api/clip/silent-check', methods=['POST'])
@auth.rate_limit(per_minute=20)
def silent_check():
    """Runs ffmpeg silencedetect on a clip and returns silent segments.
    Body: {path, noise: '-30dB' (def), min: 1.0 (sec)}"""
    d = request.json or {}
    path = str(d.get('path',''))
    if not path:
        return jsonify({'error':'path required'}), 400
    if not _path_in_allowlist(path):
        return jsonify({'error':'Path not in MARIO_SCAN_ROOTS allowlist'}), 403
    if os.path.splitext(path)[1].lower() not in (_VIDEO_EXT_SET | _AUDIO_EXT_SET):
        return jsonify({'error':'unsupported extension'}), 400
    if not os.path.isfile(path):
        return jsonify({'error':'file not found'}), 404
    noise = str(d.get('noise','-30dB'))
    if not re.match(r'^-?\d{1,3}(\.\d+)?dB$', noise):
        return jsonify({'error':'invalid noise'}), 400
    mindur = max(0.1, min(60.0, float(d.get('min', 1.0))))
    try:
        r = subprocess.run(
            ['ffmpeg','-hide_banner','-nostats','-i', path,
             '-af', f'silencedetect=noise={noise}:d={mindur}',
             '-f','null','-'],
            capture_output=True, text=True, timeout=120)
        out = r.stderr
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', out)]
    ends   = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', out)]
    durs   = [float(x) for x in re.findall(r'silence_duration: ([\d.]+)', out)]
    gaps = []
    for i, st in enumerate(starts):
        gaps.append({'start': st,
                     'end':   ends[i]   if i < len(ends)   else None,
                     'dur':   durs[i]   if i < len(durs)   else None})
    total_silent = sum(g.get('dur') or 0 for g in gaps)
    info = get_video_info(path)
    return jsonify({
        'path': path, 'duration': info.get('duration', 0),
        'silent_total': round(total_silent, 2),
        'silent_ratio': round(total_silent / info.get('duration',1), 3)
                        if info.get('duration',0) > 0 else 0,
        'gaps': gaps,
        'mostly_silent': bool(info.get('duration',0) > 0 and
                              total_silent / info['duration'] > 0.5),
    })




def _bootstrap_state():
    """Load persisted playlist + scheduled jobs from SQLite on startup."""
    global playlist, scheduled_jobs
    state.init_db()
    try:
        playlist[:] = state.load_playlist()
    except Exception as e:
        print(f'[mario] failed to load playlist: {e}')
    try:
        scheduled_jobs[:] = state.load_jobs()
    except Exception as e:
        print(f'[mario] failed to load scheduled jobs: {e}')
    # v3.5: one-shot migration of legacy history.json → SQLite
    try:
        n = state.history_migrate_from_json(HISTORY_FILE)
        if n: print(f'[mario] migrated {n} history rows from history.json')
    except Exception as e:
        print(f'[mario] history migration failed: {e}')


# Run once at import time so it works under both `python app.py` and
# `flask run` / gunicorn.
_bootstrap_state()


# ── Graceful shutdown ───────────────────────────────────────────────────────
# Make sure ffmpeg children are terminated when the parent process exits
# (SIGTERM from systemd, Ctrl-C, container stop). Otherwise ffmpeg becomes
# a zombie holding /dev/video10 + the RTMP socket.
_shutdown_done = threading.Event()

def _graceful_shutdown(*_args):
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    global ffmpeg_process, streaming, auto_restart
    auto_restart = False
    streaming    = False
    p = ffmpeg_process
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass
    # v3.7: clean leftover concat tempfiles
    while _concat_tempfiles:
        try: os.unlink(_concat_tempfiles.popleft())
        except Exception: pass

atexit.register(_graceful_shutdown)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, lambda *_: (_graceful_shutdown(), os._exit(0)))
    except (ValueError, OSError):
        # Not main thread (e.g. under gunicorn worker) — atexit still runs.
        pass

# v3.7: bootstrap the scheduler at module import time so scheduled jobs
# run under gunicorn (was inside `if __name__ == '__main__'` → never ran
# in production). Idempotent — _ensure_scheduler() no-ops if alive.
try:
    _ensure_scheduler()
except Exception:
    log.exception('scheduler bootstrap failed')

# ── Phase 6C: refuse to bind to a public interface without a password ────────
# Defined BEFORE the import-time call below so gunicorn / WSGI workers
# (which import this module at startup) trigger the check correctly.
def _enforce_public_auth(host: str):
    """If the app is binding to a non-loopback interface, MARIO_PASSWORD
    MUST be set. Otherwise refuse to start. An operator who genuinely wants
    an unauthenticated public bind (development LAN demo, etc.) can pass
    MARIO_ALLOW_UNSAFE=1 to override — never set this in production."""
    public = host not in ('127.0.0.1', 'localhost', '::1', '')
    has_pw = bool((os.environ.get('MARIO_PASSWORD', '') or '').strip())
    if public and not has_pw and os.environ.get('MARIO_ALLOW_UNSAFE', '0') != '1':
        sys.stderr.write(
            '\n[mario] REFUSING TO START: binding to public interface '
            f'({host}) without MARIO_PASSWORD.\n'
            '         Set MARIO_PASSWORD=... or, for trusted local testing,\n'
            '         set MARIO_ALLOW_UNSAFE=1 (NEVER in production).\n\n'
        )
        sys.exit(2)

# Phase 6C: enforce on module-import (covers gunicorn / WSGI workers).
# Skipped under pytest so unit tests still import the module freely.
if 'pytest' not in sys.modules:
    try:
        _enforce_public_auth(os.environ.get('MARIO_HOST', '127.0.0.1'))
    except SystemExit:
        raise
    except Exception:
        log.exception('public-auth enforcement check failed')

# ── Phase 9: consistent JSON error shape for any uncaught exception ──────────
@app.errorhandler(Exception)
def _json_error(e):
    """Return errors as {"success": false, "error": "<safe msg>"} for JSON
    clients. Never leak tracebacks or full command lines to the UI."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        msg  = e.description or e.name
        code = e.code or 500
    else:
        log.exception('unhandled exception')
        msg  = 'Internal server error'
        code = 500
    # Heuristic: respond JSON when the client wants JSON or hits /api/*.
    wants_json = (request.path.startswith('/api/')
                  or 'application/json' in (request.headers.get('Accept') or ''))
    if wants_json:
        return jsonify({'success': False, 'error': _redact(str(msg))}), code
    return (str(msg), code)

# ── Phase 6D: safe media preview endpoint (replaces frontend file://) ────────
@app.route('/api/media/preview')
def media_preview():
    """Stream a video/audio file by absolute path, gated by the same
    allowlist used for /api/scan. Supports basic HTTP Range so the browser
    <video> element can seek. No path traversal — _path_in_allowlist
    normalizes against MARIO_SCAN_ROOTS."""
    path = request.args.get('path', '')
    if not path or not isinstance(path, str):
        return jsonify({'success': False, 'error': 'path required'}), 400
    if not _path_in_allowlist(path):
        return jsonify({'success': False,
                        'error': 'Path not in MARIO_SCAN_ROOTS allowlist'}), 403
    if not os.path.isfile(path):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    # v3.8.3: strict extension allowlist — never serve arbitrary octet-stream
    ext = os.path.splitext(path)[1].lower()
    if ext not in (_VIDEO_EXT_SET | _AUDIO_EXT_SET):
        return jsonify({'success': False,
                        'error': 'Unsupported preview file type'}), 400
    # Flask's send_file handles If-Modified-Since + Range requests transparently.
    from flask import send_file
    mime = {
        '.mp4':'video/mp4', '.mov':'video/quicktime', '.mkv':'video/x-matroska',
        '.webm':'video/webm','.m4v':'video/x-m4v','.avi':'video/x-msvideo',
        '.wmv':'video/x-ms-wmv','.flv':'video/x-flv',
        '.mp3':'audio/mpeg','.wav':'audio/wav','.ogg':'audio/ogg','.flac':'audio/flac',
        '.aac':'audio/aac','.m4a':'audio/mp4','.opus':'audio/ogg',
    }.get(ext, 'application/octet-stream')
    return send_file(path, mimetype=mime, conditional=True)


# ── v3.9.0: Dashboard support endpoints ──────────────────────────────────────
@app.route('/api/health')
def health_json():
    """Extended health snapshot — consumed by the dashboard checklist."""
    info = auth.system_health()
    info.update({
        'streaming': streaming,
        'uptime': int(time.time()-stream_start_time) if stream_start_time else 0,
        'auth_enabled': auth.auth_enabled(),
        'csrf_enabled': CSRF_ENABLED,
        'v4l2_ok': bool(check_v4l2()),
        'playlist_size': len(playlist),
    })
    try:
        rec_dir = os.environ.get('MARIO_RECORDINGS_DIR') or os.path.expanduser('~/mario_recordings')
        check_dir = rec_dir if os.path.isdir(rec_dir) else os.path.expanduser('~')
        st = os.statvfs(check_dir)
        free_mb = (st.f_bavail * st.f_frsize) // (1024*1024)
        info['disk_free_mb'] = free_mb
        info['disk_warning'] = free_mb < 500
    except Exception as e:
        info['disk_error'] = str(e)
    try: info['deployment'] = _deployment_info()
    except Exception as e: info['deployment'] = {'error': str(e)}
    return jsonify(info)

@app.route('/api/app/info')
def app_info():
    """Runtime + paths summary for the System Health page. Secrets redacted."""
    import sys, platform, getpass, shutil, subprocess
    ffmpeg_ver = 'not found'
    try:
        ff = shutil.which('ffmpeg')
        if ff:
            out = subprocess.check_output([ff,'-version'], stderr=subprocess.STDOUT, timeout=2).decode('utf-8','replace')
            ffmpeg_ver = out.splitlines()[0] if out else 'unknown'
    except Exception as e:
        ffmpeg_ver = f'error: {e}'
    try: user = getpass.getuser()
    except Exception: user = '?'
    try:
        rec_dir = os.environ.get('MARIO_RECORDINGS_DIR') or os.path.expanduser('~/mario_recordings')
        st = os.statvfs(rec_dir if os.path.isdir(rec_dir) else os.path.expanduser('~'))
        disk_free = (st.f_bavail * st.f_frsize) // (1024*1024)
    except Exception: disk_free = None
    return jsonify({
        'version': MARIO_VERSION,
        'python': f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
        'platform': platform.platform(),
        'ffmpeg': ffmpeg_ver,
        'user': user,
        'db_path': getattr(state, 'DB_PATH', '?'),
        'recordings_path': os.environ.get('MARIO_RECORDINGS_DIR') or os.path.expanduser('~/mario_recordings'),
        'scan_roots': list(SCAN_ROOTS or []),
        'auth_enabled': auth.auth_enabled(),
        'csrf_enabled': CSRF_ENABLED,
        'strict_origin': STRICT_ORIGIN,
        'disk_free_mb': disk_free,
        'deployment': _deployment_info(),
    })

@app.route('/api/logs/recent')
def logs_recent():
    """Last N redacted ffmpeg log lines. Stream keys never returned in clear."""
    try: n = max(1, min(500, int(request.args.get('n', 100))))
    except Exception: n = 100
    try:
        # ffmpeg_logs is a deque maintained by _append_log()
        lines = list(ffmpeg_logs)[-n:]
    except Exception:
        lines = []
    return jsonify({'success': True, 'count': len(lines), 'lines': lines})


# ── Deployment / autostart introspection (used by /api/health and /api/app/info)
def _deployment_info():
    """Best-effort deployment detection. Never raises."""
    import shutil, subprocess, getpass
    info = {
        'deployment_mode': 'manual',
        'systemd_unit': None,
        'autostart_enabled': None,        # True / False / None (=unknown)
        'restart_policy': None,
        'in_docker': False,
        'reboot_allowed': os.environ.get('MARIO_ALLOW_SERVER_REBOOT') == '1',
        'restart_app_supported': False,
        'restart_app_reason': '',
    }
    # Docker detection
    try:
        if os.path.exists('/.dockerenv') or os.environ.get('MARIO_IN_DOCKER') == '1':
            info['in_docker'] = True
            info['deployment_mode'] = 'docker'
            info['restart_policy'] = 'unless-stopped (compose default)'
            info['restart_app_reason'] = 'Use `docker compose restart` from the host.'
            return info
    except Exception:
        pass
    # systemd detection
    try:
        user = os.environ.get('MARIO_SYSTEMD_USER') or getpass.getuser()
    except Exception:
        user = None
    unit = os.environ.get('MARIO_SYSTEMD_UNIT') or (f'mario@{user}.service' if user else None)
    info['systemd_unit'] = unit
    sysctl = shutil.which('systemctl')
    if sysctl and unit:
        try:
            r = subprocess.run([sysctl, 'is-enabled', unit],
                               capture_output=True, text=True, timeout=3)
            out = (r.stdout or r.stderr or '').strip()
            if r.returncode == 0 and out in ('enabled', 'enabled-runtime', 'alias', 'static'):
                info['autostart_enabled'] = True
                info['deployment_mode'] = 'systemd'
            elif out in ('disabled', 'masked', 'linked'):
                info['autostart_enabled'] = False
                info['deployment_mode'] = 'systemd'
            elif out == 'not-found' or 'not-found' in out:
                info['autostart_enabled'] = False
            else:
                info['autostart_enabled'] = None
        except Exception:
            info['autostart_enabled'] = None
        # Probe whether passwordless `sudo systemctl restart <unit>` is available.
        try:
            r = subprocess.run(['sudo', '-n', sysctl, 'is-active', unit],
                               capture_output=True, text=True, timeout=3)
            info['restart_app_supported'] = (r.returncode in (0, 3))  # 3 = inactive but allowed
            if not info['restart_app_supported']:
                info['restart_app_reason'] = 'Add a NOPASSWD sudoers rule for systemctl restart <unit>.'
        except Exception as e:
            info['restart_app_reason'] = f'sudo probe failed: {e}'
    else:
        info['restart_app_reason'] = 'systemctl not available on PATH.'
    return info


@app.route('/api/system/restart-app', methods=['POST'])
@auth.rate_limit(per_minute=3)
def system_restart_app():
    """Restart the systemd unit running this app. CSRF + auth enforced by _auth_gate."""
    import shutil, subprocess, threading, shlex
    dep = _deployment_info()
    if dep['in_docker']:
        return jsonify({'success': False,
                        'error': 'App restart from inside Docker is not available. '
                                 'Use `docker compose restart` on the host.'}), 400
    unit = dep.get('systemd_unit')
    sysctl = shutil.which('systemctl')
    if not sysctl or not unit:
        return jsonify({'success': False,
                        'error': 'systemctl/unit not detected. Set MARIO_SYSTEMD_UNIT.'}), 400
    # Fixed command — no user input concatenated.
    cmd = ['sudo', '-n', sysctl, 'restart', unit]
    def _run():
        time.sleep(1.0)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
    print(f'[system] restart-app scheduled: {shlex.join(cmd)}', flush=True)
    return jsonify({'success': True, 'message': 'Restarting app service…', 'unit': unit})


@app.route('/api/system/reboot-server', methods=['POST'])
@auth.rate_limit(per_minute=1)
def system_reboot_server():
    """Reboot the host. DISABLED unless MARIO_ALLOW_SERVER_REBOOT=1 + sudoers rule.

    The frontend additionally requires the user to type 'REBOOT' to confirm,
    but server-side we ALSO require a 'confirm':'REBOOT' field in the JSON
    body so a stolen CSRF token cannot trigger this from a stale tab.
    """
    import shutil, subprocess, threading
    if os.environ.get('MARIO_ALLOW_SERVER_REBOOT') != '1':
        return jsonify({'success': False,
                        'error': 'Server reboot disabled. Set MARIO_ALLOW_SERVER_REBOOT=1.'}), 403
    body = request.get_json(silent=True) or {}
    if (body.get('confirm') or '').strip() != 'REBOOT':
        return jsonify({'success': False,
                        'error': "Confirmation missing. POST {'confirm':'REBOOT'}."}), 400
    reboot = '/sbin/reboot' if os.path.exists('/sbin/reboot') else shutil.which('reboot')
    if not reboot:
        return jsonify({'success': False, 'error': 'reboot binary not found.'}), 500
    cmd = ['sudo', '-n', reboot]
    def _run():
        time.sleep(2.0)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
    print('[system] reboot-server scheduled', flush=True)
    return jsonify({'success': True, 'message': 'Rebooting server in ~2 seconds…'})





if __name__ == '__main__':

    os.makedirs('templates', exist_ok=True)
    _ensure_scheduler()
    # SECURITY: debug=False in production. Bind to 127.0.0.1 unless
    # MARIO_HOST is explicitly set (e.g. MARIO_HOST=0.0.0.0 for LAN access).
    host  = os.environ.get('MARIO_HOST', '127.0.0.1')
    port  = int(os.environ.get('MARIO_PORT', '5000'))
    debug = os.environ.get('MARIO_DEBUG', '0') == '1'
    _enforce_public_auth(host)
    app.run(debug=debug, host=host, port=port, threaded=True)
