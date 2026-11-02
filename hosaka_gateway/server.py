from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from hosaka.extensions.manager import Manager
from hosaka.extensions.manifest import validate_id
from hosaka_gateway.auth import Auth, COOKIE
from hosaka_gateway.runtime import Runtimes

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
HOP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "authorization", "cookie",
}


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


def create_app(manager: Manager | None = None, auth: Auth | None = None, *, dev_mode: bool = False) -> FastAPI:
    manager = manager or Manager()
    auth = auth or Auth(manager.root / "gateway" / "auth.json")
    runtimes = Runtimes(manager, dev_mode=dev_mode)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60, connect=3)) as client:
            app.state.client = client
            monitor = asyncio.create_task(runtimes.reap())
            try:
                yield
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
                await runtimes.close()

    app = FastAPI(title="Hosaka Gateway", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtimes = runtimes

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        target = urlsplit(str(request.url))
        client = request.client.host if request.client else ""
        if client in LOOPBACK and target.hostname not in LOOPBACK:
            return JSONResponse({"detail": "Use a loopback hostname locally."}, 403)
        if client not in LOOPBACK and target.scheme != "https":
            return JSONResponse({"detail": "LAN sign-in requires HTTPS."}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            if origin != f"{target.scheme}://{target.netloc}" or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Same-origin requests required."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def role(request: Request) -> str:
        result = auth.role(request.cookies.get(COOKIE))
        if result is None and dev_mode and (request.client and request.client.host in LOOPBACK):
            return "organizer"
        if result is None:
            if request.method == "GET" and request.url.path != "/apps" and "text/html" in request.headers.get("accept", ""):
                target = request.url.path + ("?" + request.url.query if request.url.query else "")
                raise HTTPException(303, "Sign in to Hosaka Gateway.", headers={"Location": "/?next=" + quote(target, safe="")})
            raise HTTPException(401, "Sign in to Hosaka Gateway.")
        return result

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/gateway.js")
    async def script():
        return FileResponse(STATIC / "gateway.js", media_type="text/javascript")

    @app.get("/gateway.css")
    async def styles():
        return FileResponse(STATIC / "gateway.css", media_type="text/css")

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "hosaka-gateway"}

    @app.post("/session")
    async def login(body: Login, request: Request):
        client = request.client.host if request.client else "unknown"
        if not auth.allow_login(client):
            raise HTTPException(429, "Too many sign-in attempts. Try again later.")
        if not await asyncio.to_thread(auth.password_ok, body.password):
            raise HTTPException(401, "Incorrect gateway password.")
        response = JSONResponse({"role": "organizer"})
        response.set_cookie(
            COOKIE, auth.session(), httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=8 * 3600, path="/",
        )
        return response

    @app.delete("/session")
    async def logout():
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/apps")
    async def catalog(request: Request):
        role(request)
        return {
            "apps": await asyncio.to_thread(runtimes.catalog),
            "dev_mode": dev_mode and auth.role(request.cookies.get(COOKIE)) is None,
        }

    async def runtime(identifier):
        try:
            validate_id(identifier)
            return await runtimes.get(identifier)
        except (ValueError, OSError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.api_route("/{identifier}", methods=["GET", "HEAD"])
    async def app_redirect(identifier: str, request: Request):
        role(request)
        try:
            validate_id(identifier)
        except ValueError as exc:
            raise HTTPException(400, "Invalid app identifier.") from exc
        if identifier == "halloween-citations":
            return RedirectResponse("/citations/", 307)
        return RedirectResponse(f"/{identifier}/", 307)

    @app.api_route("/{identifier}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(identifier: str, path: str, request: Request):
        caller_role = role(request)
        if identifier == "halloween-citations":
            target = f"/citations/{path}" + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(target, 307)
        if any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
            raise HTTPException(400, "Invalid app path.")
        worker = await runtime(identifier)
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS and not key.lower().startswith(("x-hosaka-", "x-forwarded-"))
        }
        headers.update({
            "X-Hosaka-Gateway-Token": worker.token,
            "X-Hosaka-Role": caller_role,
        })
        target = worker.origin + "/" + path
        if request.url.query:
            target += "?" + request.url.query
        try:
            upstream = await app.state.client.send(app.state.client.build_request(
                request.method, target, headers=headers, content=request.stream(),
            ), stream=True)
        except httpx.TransportError as exc:
            raise HTTPException(502, "App is unavailable. Check gateway status and logs.") from exc
        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() not in HOP_HEADERS | {"set-cookie", "content-encoding", "content-length"}
        }
        location = upstream.headers.get("location")
        if location:
            if location.startswith(worker.origin + "/"):
                location = location[len(worker.origin):]
            if location.startswith("/") and not location.startswith(f"/{identifier}/"):
                response_headers["location"] = f"/{identifier}" + location
            elif urlsplit(location).netloc:
                await upstream.aclose()
                raise HTTPException(502, "App returned an external redirect.")
        return StreamingResponse(
            upstream.aiter_bytes(), status_code=upstream.status_code, headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    @app.websocket("/{identifier}/{path:path}")
    async def websocket(websocket: WebSocket, identifier: str, path: str):
        target = urlsplit(str(websocket.url))
        expected = ("https" if target.scheme == "wss" else "http") + "://" + target.netloc
        session_cookie = websocket.cookies.get(COOKIE)
        caller_role = auth.role(session_cookie)
        loopback_dev = dev_mode and (websocket.client is not None and websocket.client.host in LOOPBACK)
        if caller_role is None and loopback_dev:
            caller_role = "organizer"
        if (
            caller_role is None or websocket.headers.get("origin") != expected
            or (websocket.client.host not in LOOPBACK and target.scheme != "wss")
            or any(part in {".", ".."} for part in path.split("/")) or "\\" in path
        ):
            await websocket.close(code=1008)
            return
        try:
            worker = await runtimes.get(identifier)
            url = worker.origin.replace("http:", "ws:") + "/" + path
            if websocket.url.query:
                url += "?" + websocket.url.query
            async with connect(url, additional_headers={
                "X-Hosaka-Gateway-Token": worker.token, "X-Hosaka-Role": caller_role,
            }, max_size=1048576) as upstream:
                await websocket.accept()

                async def inbound():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message.get("text") if message.get("text") is not None else message["bytes"])

                async def outbound():
                    async for message in upstream:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                async def session_guard():
                    while loopback_dev or auth.role(session_cookie) is not None:
                        await asyncio.sleep(15)
                    await websocket.close(code=1008)

                tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound()), asyncio.create_task(session_guard())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, ValueError, ConnectionClosed, WebSocketDisconnect):
            with suppress(RuntimeError):
                await websocket.close(code=1011)

    return app
