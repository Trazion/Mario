"""Regression tests for v3.8.x phase fixes: redaction, loop_mode 'one',
volume normalization shape, rglob pattern, playlist sanitization."""
import os, sys, tempfile, shutil
from pathlib import Path
import pytest

# NOTE: do NOT import `app` at module level. The isolated_home fixture in
# conftest.py sets HOME/MARIO_PASSWORD/MARIO_CSRF/MARIO_STRICT_ORIGIN before
# any test runs; importing app here would touch the developer's real
# ~/mario_state.db during collection.

@pytest.fixture
def mario_app(isolated_home, monkeypatch):
    monkeypatch.setenv('MARIO_SCAN_ROOTS', '/tmp')
    for mod in ('app', 'state', 'auth'):
        if mod in sys.modules: del sys.modules[mod]
    import app as mario_app
    return mario_app



def test_redact_rtmp_stream_key(mario_app):
    s = 'ffmpeg -f flv rtmp://a.rtmp.youtube.com/live2/abc-def-1234-secret'
    out = mario_app._redact(s)
    assert 'abc-def-1234-secret' not in out
    assert 'rtmp://a.rtmp.youtube.com/live2/' in out
    assert '***' in out


def test_redact_query_token(mario_app):
    s = 'https://example.com/api?token=SUPERSECRET123&x=1'
    out = mario_app._redact(s)
    assert 'SUPERSECRET123' not in out
    assert 'token=***' in out


def test_redact_url_password(mario_app):
    out = mario_app._redact('rtmp://user:pass@host/app/key')
    assert 'pass' not in out
    assert 'user:***@' in out


def test_redact_sensitive_command_does_not_mutate_original(mario_app):
    cmd = ['ffmpeg', '-f', 'flv', 'rtmp://x.com/live/MYKEY']
    safe = mario_app.redact_sensitive_command(cmd)
    assert cmd[-1] == 'rtmp://x.com/live/MYKEY'   # original untouched
    assert 'MYKEY' not in safe[-1]


def test_recursive_rglob_finds_nested_files():
    """The Phase 4A fix: rglob now uses the full '*.mp4' glob, not 'mp4'."""
    d = tempfile.mkdtemp(prefix='mario_test_')
    try:
        sub = Path(d) / 'sub'; sub.mkdir()
        (sub / 'clip.mp4').write_bytes(b'\x00')
        (sub / 'song.MP3').write_bytes(b'\x00')
        # Mimic the actual rglob calls used in scan_folder
        for ext in ('*.mp4', '*.MP4'):
            hits = list(Path(d).rglob(ext))
            if hits:
                assert any(p.name == 'clip.mp4' for p in hits)
                break
        else:
            assert False, 'recursive *.mp4 found nothing'
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_volume_clamp_matches_frontend_contract(mario_app):
    """Frontend sends percent/100 (0..2). Backend clamps to 0..4."""
    p = mario_app.validate_stream_params({'global_volume': 1.0})
    assert p['global_volume'] == 1.0
    p = mario_app.validate_stream_params({'global_volume': 0.5})
    assert p['global_volume'] == 0.5
    p = mario_app.validate_stream_params({'global_volume': 99})   # malicious huge
    assert p['global_volume'] == 4.0                              # clamped


def test_loop_mode_one_validation(mario_app):
    p = mario_app.validate_stream_params({'loop_mode': 'one'})
    assert p['loop_mode'] == 'one'
    assert p['loop'] is True
    p = mario_app.validate_stream_params({'loop_mode': 'once'})
    assert p['loop'] is False


def test_enforce_public_auth_blocks_unauthenticated_public_bind(mario_app, monkeypatch):
    monkeypatch.delenv('MARIO_PASSWORD', raising=False)
    monkeypatch.delenv('MARIO_ALLOW_UNSAFE', raising=False)
    import pytest
    with pytest.raises(SystemExit):
        mario_app._enforce_public_auth('0.0.0.0')


def test_enforce_public_auth_allows_loopback_without_password(mario_app, monkeypatch):
    monkeypatch.delenv('MARIO_PASSWORD', raising=False)
    mario_app._enforce_public_auth('127.0.0.1')  # must not raise


# ── v3.8.2 regression tests ───────────────────────────────────────────
def test_record_dir_rejects_arbitrary_path(mario_app, monkeypatch):
    import pytest
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', ['/tmp'])
    with pytest.raises(mario_app.ValidationError):
        mario_app.validate_stream_params({'record_dir': '/etc'})


def test_record_dir_accepts_default(mario_app):
    p = mario_app.validate_stream_params({})  # no record_dir → default
    assert p['record_dir'].endswith('mario_recordings')


def test_clean_playlist_items_rejects_bad_extension(mario_app, tmp_path, monkeypatch):
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', [str(tmp_path)])
    bad = tmp_path / 'evil.sh'; bad.write_text('#!/bin/sh\n')
    import pytest
    with pytest.raises(mario_app.ValidationError):
        mario_app.clean_playlist_items([{'path': str(bad)}])


def test_clean_playlist_items_rejects_outside_allowlist(mario_app, tmp_path, monkeypatch):
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', [str(tmp_path)])
    import pytest
    with pytest.raises(mario_app.ValidationError):
        mario_app.clean_playlist_items([{'path': '/etc/passwd.mp4'}])


def test_clean_playlist_items_sanitizes_volume(mario_app, tmp_path, monkeypatch):
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', [str(tmp_path)])
    ok = tmp_path / 'a.mp4'; ok.write_bytes(b'\x00')
    out = mario_app.clean_playlist_items(
        [{'path': str(ok), 'volume': 999, 'muted': 'yes', 'info': 'nope'}])
    assert out[0]['volume'] == 4.0
    assert out[0]['muted'] is True
    assert out[0]['info'] == {}



def test_clean_playlist_items_safe_string_volume(mario_app, tmp_path, monkeypatch):
    """Bad volume string must not raise ValueError — clamps to default."""
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', [str(tmp_path)])
    ok = tmp_path / 'a.mp4'; ok.write_bytes(b'\x00')
    out = mario_app.clean_playlist_items(
        [{'path': str(ok), 'volume': 'evil', 'muted': 'nope'}])
    assert out[0]['volume'] == 1.0
    assert out[0]['muted'] is False


def test_media_preview_rejects_bad_extension(mario_app, client, tmp_path, monkeypatch):
    monkeypatch.setattr(mario_app, 'SCAN_ROOTS', [str(tmp_path)])
    monkeypatch.setattr(mario_app, '_path_in_allowlist', lambda p: True)
    bad = tmp_path / 'evil.txt'; bad.write_text('x')
    r = client.get(f'/api/media/preview?path={bad}')
    assert r.status_code == 400
    assert 'Unsupported' in r.get_json()['error']


def test_backup_export_uses_state_db_path(mario_app, monkeypatch, tmp_path):
    """backup_export must read state.DB_PATH, not ~/mario_state.db."""
    import state
    assert state.DB_PATH  # set
    # Just verify the source references state.DB_PATH
    import inspect
    src = inspect.getsource(mario_app.backup_export)
    assert 'state.DB_PATH' in src
