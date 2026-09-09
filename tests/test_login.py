"""v3.9.6 — internal /login page replaces browser Basic-Auth popup."""
import os, importlib, sys

def _client_with_auth(monkeypatch):
    monkeypatch.setenv('MARIO_PASSWORD', 'secret123')
    monkeypatch.setenv('MARIO_USER', 'mario')
    monkeypatch.setenv('MARIO_CSRF', '0')
    for m in ('app', 'state', 'auth'):
        if m in sys.modules: del sys.modules[m]
    import app as app_module
    app_module.app.testing = True
    return app_module.app.test_client()

def test_dashboard_redirects_to_login_when_unauth(monkeypatch):
    c = _client_with_auth(monkeypatch)
    r = c.get('/')
    assert r.status_code in (301, 302)
    assert '/login' in r.headers.get('Location', '')
    # No browser Basic-Auth popup header
    assert 'WWW-Authenticate' not in r.headers

def test_api_returns_401_json_no_basic(monkeypatch):
    c = _client_with_auth(monkeypatch)
    r = c.get('/api/status')
    assert r.status_code == 401
    assert 'WWW-Authenticate' not in r.headers
    j = r.get_json()
    assert j.get('login_url') == '/login'

def test_login_page_renders(monkeypatch):
    c = _client_with_auth(monkeypatch)
    r = c.get('/login')
    assert r.status_code == 200
    assert b'Sign in' in r.data
    assert b'name="csrf_token"' in r.data

def test_login_success_sets_session(monkeypatch):
    c = _client_with_auth(monkeypatch)
    c.get('/login')  # prime CSRF cookie
    tok = next((v for k, v in c.get_cookie('mario_csrf').__dict__.items()
                if k == 'value'), None) if False else None
    # simpler: read cookie via test client
    cookie = c.get_cookie('mario_csrf')
    tok = cookie.value if cookie else ''
    r = c.post('/login', data={'username':'mario','password':'secret123',
                               'csrf_token': tok, 'next': '/'})
    assert r.status_code in (301, 302)
    assert r.headers.get('Location', '').endswith('/')
    # Now /api/status should pass
    r2 = c.get('/api/status')
    assert r2.status_code == 200

def test_login_bad_password(monkeypatch):
    c = _client_with_auth(monkeypatch)
    c.get('/login')
    cookie = c.get_cookie('mario_csrf')
    tok = cookie.value if cookie else ''
    r = c.post('/login', data={'username':'mario','password':'wrong',
                               'csrf_token': tok, 'next': '/'})
    assert r.status_code == 401
    assert b'Invalid' in r.data

def test_logout_clears_session(monkeypatch):
    c = _client_with_auth(monkeypatch)
    c.get('/login')
    cookie = c.get_cookie('mario_csrf')
    tok = cookie.value if cookie else ''
    c.post('/login', data={'username':'mario','password':'secret123',
                           'csrf_token': tok, 'next': '/'})
    assert c.get('/api/status').status_code == 200
    r = c.get('/logout')
    assert r.status_code in (301, 302)
    assert c.get('/api/status').status_code == 401
