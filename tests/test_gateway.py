from pathlib import Path
import shutil
import sys
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import yaml

from hosaka.extensions.manager import Manager
from hosaka_gateway.auth import Auth, configure
from hosaka_gateway.server import create_app

CORE = Path(__file__).resolve().parents[2] / "Hosaka"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key in ("SUDO_USER", "HOSAKA_OPERATOR", "HOSAKA_DATA_DIR", "HOSAKA_PUBLIC_MODE", "HOSAKA_EXTENSIONS_SAFE_MODE"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manager = Manager()
    manager.init()
    package = manager.packages / "reference-extension"
    manifest_path = package / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["web"] = {"schema_version": 1, "file": "server.py", "callable": "app", "health": "/health"}
    manifest_path.write_text(yaml.safe_dump(manifest))
    source = tmp_path / "web source"
    source.mkdir()
    (source / "server.py").write_text("""
import hmac
import os
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
app = FastAPI()
@app.middleware("http")
async def auth(request, call_next):
    if not hmac.compare_digest(request.headers.get("x-hosaka-gateway-token", ""), os.environ["HOSAKA_GATEWAY_TOKEN"]):
        return JSONResponse({"detail": "gateway only"}, 403)
    return await call_next(request)
@app.get("/health")
def health(): return {"ok": True}
@app.get("/")
def root(request: Request):
    return {"role": request.headers.get("x-hosaka-role"), "root_path": request.scope["root_path"]}
@app.post("/echo")
async def echo(request: Request):
    return {"text": (await request.body()).decode(), "role": request.headers.get("x-hosaka-role")}
@app.get("/redirect")
def redirect(): return RedirectResponse("/target")
@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    await socket.send_text(await socket.receive_text())
""")
    manager.trust("reference-extension", allow_storage=True, allow_web=True, web_source=source, web_python=Path(sys.executable))
    manager.enable("reference-extension", True)
    config = manager.root / "gateway/auth.json"
    configure(config, "synthetic-password-only")
    auth = Auth(config)
    app = create_app(manager, auth)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        yield manager, app, client, source, auth


def sign_in(client):
    assert client.post("/session", json={"password": "synthetic-password-only"}, headers={"origin": "http://127.0.0.1"}).status_code == 200


def test_login_origin_session_and_catalog(runtime):
    _, _, client, _, _ = runtime
    assert client.get("/apps").status_code == 401
    assert client.post("/session", json={"password": "synthetic-password-only"}).status_code == 403
    sign_in(client)
    assert client.get("/apps").json()["apps"][0]["id"] == "reference-extension"
    assert client.delete("/session", headers={"origin": "http://127.0.0.1"}).status_code == 200
    assert client.get("/apps").status_code == 401


def test_html_deep_links_return_to_sign_in_without_changing_api_errors(runtime):
    _, _, client, _, _ = runtime
    response = client.get("/reference-extension/?item=example", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?next=%2Freference-extension%2F%3Fitem%3Dexample"
    assert client.get("/reference-extension/").status_code == 401


def test_http_prefix_stream_role_and_cleanup(runtime):
    _, app, client, _, _ = runtime
    sign_in(client)
    response = client.get("/reference-extension/")
    assert response.status_code == 200, response.text
    assert response.json() == {"role": "organizer", "root_path": "/reference-extension"}
    assert client.post("/reference-extension/echo", content=b"streamed", headers={
        "origin": "http://127.0.0.1", "x-hosaka-role": "forged",
    }).json() == {"role": "organizer", "text": "streamed"}
    redirect = client.get("/reference-extension/redirect", follow_redirects=False)
    assert redirect.headers["location"] == "/reference-extension/target"
    worker = app.state.runtimes.active["reference-extension"]
    assert httpx.get(worker.origin + "/health", trust_env=False).status_code == 403
    assert client.get("/settings/secrets").status_code == 503


def test_websocket_prefix_requires_session_and_origin(runtime):
    _, _, client, _, _ = runtime
    sign_in(client)
    with client.websocket_connect("ws://127.0.0.1/reference-extension/ws", headers={"origin": "http://127.0.0.1"}) as websocket:
        websocket.send_text("hello")
        assert websocket.receive_text() == "hello"


def test_source_change_and_revocation_stop_execution(runtime):
    manager, app, client, source, _ = runtime
    sign_in(client)
    assert client.get("/reference-extension/").status_code == 200
    worker = app.state.runtimes.active["reference-extension"]
    (source / "changed.txt").write_text("new code")
    assert client.get("/reference-extension/").status_code == 503
    deadline = time.monotonic() + 5
    while worker.process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert worker.process.poll() is not None
    manager.revoke("reference-extension")
    assert client.get("/reference-extension/").status_code == 503


def test_auth_rejects_bad_tokens_and_config_permissions(runtime):
    manager, _, _, _, auth = runtime
    assert auth.role("garbage") is None
    assert auth.role(auth.session()) == "organizer"
    config = manager.root / "gateway/auth.json"
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        Auth(config)


def test_password_rotation_invalidates_existing_sessions(runtime):
    manager, _, client, _, _ = runtime
    sign_in(client)
    configure(manager.root / "gateway/auth.json", "replacement-synthetic-password")
    assert client.get("/apps").status_code == 401


def test_private_runtime_record_is_published_and_removed(runtime):
    _, app, client, _, _ = runtime
    sign_in(client)
    assert client.get("/reference-extension/").status_code == 200
    worker = app.state.runtimes.active["reference-extension"]
    assert worker.state_file.is_file()
    assert worker.state_file.stat().st_mode & 0o077 == 0
    worker.stop()
    assert not worker.state_file.exists()


def test_symlink_app_data_is_refused(runtime, tmp_path):
    manager, _, client, _, _ = runtime
    data = manager.root / "extension-data/reference-extension"
    data.parent.mkdir(exist_ok=True)
    data.symlink_to(tmp_path, target_is_directory=True)
    sign_in(client)
    assert client.get("/reference-extension/").status_code == 503
