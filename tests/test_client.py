"""Blender 不在场时也可测的纯 stdlib HTTP 客户端回归。"""
import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest


CLIENT_PATH = (Path(__file__).resolve().parent.parent / "addon" /
               "blender_modal_bridge" / "client.py")
SPEC = importlib.util.spec_from_file_location("farm_client_test_module", CLIENT_PATH)
client_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_mod)


def _client():
    return client_mod.FarmClient("https://test--blender-bridge", "key")


def test_run_retries_with_same_idempotency_key(monkeypatch):
    client = _client()
    bodies = []

    def post(_label, body):
        bodies.append(dict(body))
        if len(bodies) == 1:
            raise client_mod.FarmError("response lost", retryable=True)
        return {"id": "job-1"}

    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _seconds: None)
    assert client.run({"task_type": "render"}, "scenes/a.blend")["id"] == "job-1"
    assert len(bodies) == 2
    assert bodies[0]["request_id"] == bodies[1]["request_id"]
    assert len(bodies[0]["request_id"]) == 32


def test_run_recovery_reuses_persisted_idempotency_key(monkeypatch):
    client = _client()
    bodies = []
    monkeypatch.setattr(client, "_post", lambda _label, body: (
        bodies.append(dict(body)) or {"id": "original-job"}))
    request_id = "a" * 32
    assert client.run({"task_type": "render"}, "scenes/a.blend",
                      request_id=request_id)["id"] == "original-job"
    assert bodies[0]["request_id"] == request_id


def test_single_upload_retries_network_failure(monkeypatch, tmp_path):
    source = tmp_path / "scene.blend"
    source.write_bytes(b"BLENDER" + b"x" * 200)
    client = _client()
    attempts = []

    def upload(_qs, body, length, what):
        attempts.append((body.read(), length, what))
        if len(attempts) == 1:
            raise client_mod.FarmError("reset", retryable=True)
        return {"blend_path": "scenes/a.blend"}

    monkeypatch.setattr(client, "_upload_request", upload)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _seconds: None)
    result = client._upload_once(source, "scene.blend", source.stat().st_size, None)
    assert result["blend_path"] == "scenes/a.blend"
    assert attempts[0][0] == attempts[1][0] == source.read_bytes()


def test_http_5xx_is_retryable(monkeypatch):
    client = _client()
    error = urllib.error.HTTPError(
        "https://test", 503, "unavailable", {}, io.BytesIO(b'{"error":"cold start"}'))
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    with pytest.raises(client_mod.FarmError) as exc:
        client._req("https://test", {"x": 1}, 1)
    assert exc.value.retryable is True
    assert "cold start" in str(exc.value)


def test_get_auth_uses_header_not_url_query(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"healthy":true}'

    def open_request(request, **_kwargs):
        assert "key=" not in request.full_url
        assert request.get_header("X-farm-key") == "key"
        return Response()

    client = _client()
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", open_request)
    assert client.health()["healthy"] is True
