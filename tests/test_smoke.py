"""Smoke tests: app boots, public endpoints answer, version is set."""

def test_health(client):
    r = client.get('/health')
    # 200 (ok) OR 503 (e.g. ffmpeg missing in CI) — never 500
    assert r.status_code in (200, 503)
    j = r.get_json()
    assert 'streaming' in j
    assert 'disk_free_mb' in j   # v3.4 addition

def test_version(client):
    r = client.get('/api/version')
    assert r.status_code == 200
    j = r.get_json()
    assert j['version'].startswith('3.')

def test_playlist_empty_by_default(client):
    r = client.get('/api/playlist')
    assert r.status_code == 200
    j = r.get_json()
    assert isinstance(j['playlist'], list)
    assert j['current_index'] == 0
    assert j['streaming'] is False

def test_history_empty(client):
    r = client.get('/api/history')
    assert r.status_code == 200
    assert isinstance(r.get_json()['history'], list)

def test_metrics_prometheus(client):
    r = client.get('/metrics')
    assert r.status_code == 200
    assert b'mario_' in r.data
