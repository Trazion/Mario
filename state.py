"""SQLite-backed persistent state for Mario Camera Streamer.

Stores the active playlist, scheduled jobs, and arbitrary key/value
state so they survive process restarts. History remains in JSON
(legacy) but could be migrated here later.
"""
import os, json, sqlite3, threading

DB_PATH = os.environ.get('MARIO_DB_PATH') or os.path.expanduser('~/mario_state.db')
# Ensure parent dir exists (matters when MARIO_DB_PATH points inside a
# dedicated data directory / docker volume that may be freshly mounted).
try:
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
except OSError:
    pass
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS playlist (
            idx     INTEGER PRIMARY KEY,
            path    TEXT NOT NULL,
            name    TEXT NOT NULL,
            info    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            time    TEXT NOT NULL,
            days    TEXT NOT NULL,
            active  INTEGER NOT NULL,
            params  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clip_stats (
            path        TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            play_count  INTEGER NOT NULL DEFAULT 0,
            total_secs  INTEGER NOT NULL DEFAULT 0,
            last_played INTEGER
        );
        -- v3.5: stream session history (migrated from history.json)
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY,
            started     TEXT NOT NULL,
            ended       TEXT NOT NULL,
            duration    INTEGER NOT NULL,
            duration_str TEXT NOT NULL,
            resolution  TEXT,
            device      TEXT,
            rtmp        INTEGER NOT NULL DEFAULT 0,
            playlist_count INTEGER NOT NULL DEFAULT 0,
            restarts    INTEGER NOT NULL DEFAULT 0,
            peak_fps    REAL NOT NULL DEFAULT 0,
            total_frames INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_history_started ON history(started);
        """)


# ── History (v3.5: SQLite-backed) ───────────────────────────────────
HISTORY_MAX_ROWS = 500

def history_append(row):
    """Insert a session row. Auto-trims to HISTORY_MAX_ROWS."""
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO history
            (id,started,ended,duration,duration_str,resolution,device,
             rtmp,playlist_count,restarts,peak_fps,total_frames)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row['id'], row['started'], row['ended'], row['duration'],
             row['duration_str'], row.get('resolution',''), row.get('device',''),
             1 if row.get('rtmp') else 0, row.get('playlist_count',0),
             row.get('restarts',0), row.get('peak_fps',0),
             row.get('total_frames',0)))
        c.execute("""DELETE FROM history WHERE id NOT IN
            (SELECT id FROM history ORDER BY id DESC LIMIT ?)""",
            (HISTORY_MAX_ROWS,))

def history_all():
    with _lock, _conn() as c:
        rows = c.execute('SELECT * FROM history ORDER BY id ASC').fetchall()
    out = []
    for r in rows:
        d = dict(r); d['rtmp'] = bool(d['rtmp']); out.append(d)
    return out

def history_clear():
    with _lock, _conn() as c:
        c.execute('DELETE FROM history')

def history_migrate_from_json(path):
    """One-shot import from legacy ~/mario_history.json. Idempotent: skips
    rows whose id already exists. Returns count imported."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return 0
    except Exception:
        return 0
    imported = 0
    with _lock, _conn() as c:
        for row in rows:
            try:
                c.execute("""INSERT OR IGNORE INTO history
                    (id,started,ended,duration,duration_str,resolution,device,
                     rtmp,playlist_count,restarts,peak_fps,total_frames)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(row.get('id',0)), row.get('started',''), row.get('ended',''),
                     int(row.get('duration',0)), row.get('duration_str',''),
                     row.get('resolution',''), row.get('device',''),
                     1 if row.get('rtmp') else 0, int(row.get('playlist_count',0)),
                     int(row.get('restarts',0)), float(row.get('peak_fps',0)),
                     int(row.get('total_frames',0))))
                if c.total_changes > imported:
                    imported = c.total_changes
            except Exception:
                continue
    # Rename old file as backup so we don't keep re-importing
    try:
        os.rename(path, path + '.migrated')
    except OSError:
        pass
    return imported


# ── Analytics ───────────────────────────────────────────────────────
def bump_clip(path, name, secs=0):
    with _lock, _conn() as c:
        c.execute("""
            INSERT INTO clip_stats(path,name,play_count,total_secs,last_played)
            VALUES(?,?,1,?,?)
            ON CONFLICT(path) DO UPDATE SET
              play_count  = play_count + 1,
              total_secs  = total_secs + excluded.total_secs,
              last_played = excluded.last_played,
              name        = excluded.name
        """, (path, name, int(secs), int(__import__('time').time())))


def get_clip_stats():
    with _lock, _conn() as c:
        rows = c.execute(
            'SELECT path,name,play_count,total_secs,last_played '
            'FROM clip_stats ORDER BY play_count DESC, last_played DESC'
        ).fetchall()
    return [dict(r) for r in rows]


def reset_clip_stats():
    with _lock, _conn() as c:
        c.execute('DELETE FROM clip_stats')


# ── Playlist ────────────────────────────────────────────────────────
def _ensure_playlist_columns():
    """Add muted/volume/start_offset_seconds columns to old playlist
    tables (no-op if present)."""
    with _lock, _conn() as c:
        cols = {r['name'] for r in c.execute("PRAGMA table_info(playlist)")}
        if 'muted' not in cols:
            c.execute("ALTER TABLE playlist ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if 'volume' not in cols:
            c.execute("ALTER TABLE playlist ADD COLUMN volume REAL NOT NULL DEFAULT 1.0")
        if 'start_offset_seconds' not in cols:
            c.execute("ALTER TABLE playlist ADD COLUMN start_offset_seconds REAL NOT NULL DEFAULT 0")


def save_playlist(items):
    _ensure_playlist_columns()
    with _lock, _conn() as c:
        c.execute('DELETE FROM playlist')
        c.executemany(
            'INSERT INTO playlist(idx,path,name,info,muted,volume,start_offset_seconds) '
            'VALUES(?,?,?,?,?,?,?)',
            [(i, v.get('path',''), v.get('name',''), json.dumps(v.get('info',{})),
              1 if v.get('muted') else 0,
              float(v.get('volume', 1.0)),
              float(v.get('start_offset_seconds', 0) or 0))
             for i, v in enumerate(items)]
        )


def load_playlist():
    _ensure_playlist_columns()
    with _lock, _conn() as c:
        rows = c.execute(
            'SELECT path,name,info,muted,volume,start_offset_seconds '
            'FROM playlist ORDER BY idx'
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            'path': d['path'], 'name': d['name'],
            'info': json.loads(d['info']),
            'muted': bool(d.get('muted', 0)),
            'volume': float(d.get('volume', 1.0) or 1.0),
            'start_offset_seconds': float(d.get('start_offset_seconds', 0) or 0),
        })
    return out


# ── Scheduled jobs ──────────────────────────────────────────────────
def save_jobs(jobs):
    with _lock, _conn() as c:
        c.execute('DELETE FROM scheduled_jobs')
        c.executemany(
            'INSERT INTO scheduled_jobs(id,name,time,days,active,params) VALUES(?,?,?,?,?,?)',
            [(j['id'], j['name'], j['time'], json.dumps(j.get('days', [])),
              1 if j.get('active') else 0, json.dumps(j.get('params', {})))
             for j in jobs]
        )


def load_jobs():
    with _lock, _conn() as c:
        rows = c.execute('SELECT * FROM scheduled_jobs ORDER BY id').fetchall()
    return [{'id': r['id'], 'name': r['name'], 'time': r['time'],
             'days': json.loads(r['days']), 'active': bool(r['active']),
             'params': json.loads(r['params'])} for r in rows]


# ── Generic key/value ───────────────────────────────────────────────
def kv_set(key, value):
    with _lock, _conn() as c:
        c.execute('INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)',
                  (key, json.dumps(value)))


def kv_get(key, default=None):
    with _lock, _conn() as c:
        r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(r['value']) if r else default
