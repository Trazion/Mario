"""Pytest configuration — provides an isolated $HOME so the app's
SQLite + dir creation doesn't touch the developer's real $HOME."""
import os, sys, tempfile, pathlib
import pytest

@pytest.fixture(scope='session', autouse=True)
def isolated_home():
    tmp = tempfile.mkdtemp(prefix='mario-test-')
    os.environ['HOME'] = tmp
    os.environ['MARIO_PASSWORD'] = ''        # disable auth in tests
    os.environ['MARIO_STRICT_ORIGIN'] = '0'
    os.environ['MARIO_CSRF'] = '0'
    # Make project-files importable as a module root (tests/ → project-files/)
    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    yield tmp


@pytest.fixture
def client(isolated_home):
    import importlib
    # Force a fresh import so each test gets a clean module state
    for mod in ('app', 'state', 'auth'):
        if mod in sys.modules: del sys.modules[mod]
    import app as app_module
    app_module.app.testing = True
    with app_module.app.test_client() as c:
        yield c
