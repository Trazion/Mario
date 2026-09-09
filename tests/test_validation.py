"""Validation tests — ensures clamping + injection guards work."""
import pytest

def test_validate_clamps_fps(client):
    from app import validate_stream_params
    out = validate_stream_params({'fps': 9999})
    assert out['fps'] == 120
    out = validate_stream_params({'fps': -5})
    assert out['fps'] == 1

def test_validate_rejects_bad_device(client):
    from app import validate_stream_params, ValidationError
    with pytest.raises(ValidationError):
        validate_stream_params({'device': '/etc/passwd'})

def test_validate_rejects_bad_rtmp(client):
    from app import validate_stream_params, ValidationError
    with pytest.raises(ValidationError):
        validate_stream_params({'rtmp_url': 'javascript:alert(1)'})

def test_validate_strips_control_chars_in_overlay(client):
    from app import validate_stream_params
    out = validate_stream_params({'text_overlay': 'hi\x00\x07world'})
    assert '\x00' not in out['text_overlay']
    assert out['text_overlay'] == 'hiworld'

def test_validate_audio_norm_defaults(client):
    from app import validate_stream_params
    out = validate_stream_params({})
    assert out['audio_normalize'] is False
    assert -70.0 <= out['audio_norm_i'] <= -5.0

def test_validate_bumper_missing_file(client):
    from app import validate_stream_params, ValidationError
    with pytest.raises(ValidationError):
        validate_stream_params({'bumper_path': '/no/such/file.mp4'})

def test_history_roundtrip(client):
    import state, os
    state.init_db()
    state.history_clear()
    state.history_append({
        'id': 1, 'started': 'a', 'ended': 'b', 'duration': 5,
        'duration_str': '5s', 'resolution': '1280x720',
        'device': '/dev/video10', 'rtmp': False, 'playlist_count': 3,
        'restarts': 0, 'peak_fps': 30.0, 'total_frames': 150,
    })
    rows = state.history_all()
    assert len(rows) == 1
    assert rows[0]['playlist_count'] == 3
    assert rows[0]['rtmp'] is False
