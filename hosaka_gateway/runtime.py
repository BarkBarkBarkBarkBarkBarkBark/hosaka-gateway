from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import json
import os
from pathlib import Path
import secrets
import socket
import signal
import stat
import subprocess
import time
import threading

import httpx

from hosaka.extensions.manager import Manager, digest
from hosaka.extensions.manifest import ExtensionError, package_file, validate_package
from hosaka.extensions.runner import approval_reason
from hosaka.paths import CORE_ROOT
from hosaka_gateway.auth import atomic_private, private_directory

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    identifier: str
    process: subprocess.Popen
    port: int
    token: str
    approval: tuple[str, ...]
    log_thread: threading.Thread
    state_file: Path

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.log_thread.join(timeout=5)
        if self.state_file.is_file() and not self.state_file.is_symlink():
            try:
                saved = json.loads(self.state_file.read_text())
            except json.JSONDecodeError:
                log.warning("Invalid private runtime record: %s", self.identifier)
            else:
                if isinstance(saved, dict) and saved.get("token") == self.token:
                    self.state_file.unlink()


class Runtimes:
    def __init__(self, manager: Manager, *, dev_mode: bool = False):
        self.manager = manager
        self.dev_mode = dev_mode
        self.active: dict[str, Runtime] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.failures: dict[str, tuple[tuple[str, ...], str]] = {}

    @staticmethod
    def _approval_tuple(entry) -> tuple[str, ...]:
        return (entry.approved_digest or "", entry.web_digest or "", entry.web_python or "", ",".join(sorted(entry.grants)))

    def approved(self, identifier: str):
        registry = self.manager.registry()
        entry = registry.extensions.get(identifier)
        if entry is None:
            raise ExtensionError("App is not registered.")
        package = validate_package(Path(entry.path))
        if package.manifest.id != identifier:
            raise ExtensionError("App identity changed.")
        reason = approval_reason(package, entry, safe_mode=registry.safe_mode)
        if reason:
            raise ExtensionError(reason)
        if (
            package.manifest.web is None or "web:self" not in entry.grants
            or not entry.web_source or not entry.web_python or not entry.web_digest
        ):
            raise ExtensionError("Web runtime requires explicit --allow-web approval.")
        source = Path(entry.web_source)
        package_file(source, package.manifest.web.file)
        if not self.dev_mode and digest(source) != entry.web_digest:
            raise ExtensionError("Web source changed; inspect it and approve it again.")
        return entry, package

    def catalog(self) -> list[dict]:
        registry = self.manager.registry()
        items = []
        for identifier, entry in registry.extensions.items():
            if entry.removed or not entry.enabled:
                continue
            try:
                package = validate_package(Path(entry.path))
                if package.manifest.web is None:
                    continue
                self.approved(identifier)
                failure = self.failures.get(identifier)
                error = failure[1] if failure and failure[0] == self._approval_tuple(entry) else None
                items.append({
                    "id": identifier, "name": package.manifest.name,
                    "description": package.manifest.description,
                    "url": f"/{identifier}/", "available": error is None, "error": error,
                })
            except (OSError, ValueError) as exc:
                items.append({
                    "id": identifier, "name": identifier, "description": "",
                    "url": f"/{identifier}/",
                    "available": False, "error": str(exc),
                })
        return items

    async def get(self, identifier: str) -> Runtime:
        async with self.locks.setdefault(identifier, asyncio.Lock()):
            entry, package = await asyncio.to_thread(self.approved, identifier)
            approval = self._approval_tuple(entry)
            old = self.active.get(identifier)
            if old is not None:
                if old.approval == approval and old.process.poll() is None:
                    return old
                await asyncio.to_thread(old.stop)
                del self.active[identifier]
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            token = secrets.token_urlsafe(32)
            source = Path(entry.web_source)
            data = private_directory(self.manager.root, self.manager.root / "extension-data" / identifier)
            logs = private_directory(self.manager.root, self.manager.root / "logs" / "gateway")
            logfile = logs / f"{identifier}.log"
            if logfile.is_symlink():
                raise ExtensionError("Web runtime log must not be a symlink.")
            if logfile.exists() and logfile.stat().st_size > 2 * 1024 * 1024:
                logfile.replace(logs / f"{identifier}.previous.log")
            environment = {
                key: value for key, value in os.environ.items()
                if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
            }
            environment.update({
                "PYTHONPATH": os.pathsep.join((str(source), entry.path)),
                "HOSAKA_DATA_DIR": str(self.manager.root),
                "HOSAKA_EXTENSION_DATA": str(data),
                "HOSAKA_EXTENSION_SOURCE": entry.path,
                "HOSAKA_GATEWAY_TOKEN": token,
                "HOSAKA_WEB_NETWORK": "1" if "network:self" in entry.grants else "0",
                "HOSAKA_WEB_BACKGROUND": "1" if "background:self" in entry.grants else "0",
                "HOSAKA_APP_PREFIX": f"/{identifier}",
                "HOSAKA_IDENTITY_DIR": str(CORE_ROOT / "identity"),
            })
            config = data / "web-config.json"
            if config.exists():
                if config.is_symlink() or not stat.S_ISREG(config.stat().st_mode) or config.stat().st_mode & 0o077 or config.stat().st_size > 65536:
                    raise ExtensionError("App web-config.json must be private, regular and at most 64 KiB.")
                values = json.loads(config.read_text(encoding="utf-8"))
                if not isinstance(values, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    or not key.isupper() or not key.replace("_", "").isalnum()
                    or key.startswith(("HOSAKA_", "PYTHON", "LD_", "DYLD_"))
                    or key in {"HOME", "PATH", "TMPDIR", "SYSTEMROOT"}
                    for key, value in values.items()
                ):
                    raise ExtensionError("App web-config.json must contain only application environment strings, not runtime overrides.")
                environment.update(values)
            web = package.manifest.web
            command = [
                entry.web_python, "-m", "uvicorn", f"{Path(web.file).stem}:{web.callable}",
                "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
                "--no-proxy-headers", "--root-path", f"/{identifier}",
            ]
            stream = os.fdopen(os.open(logfile, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600), "ab", buffering=0)
            try:
                process = subprocess.Popen(
                    command, cwd=source, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                )
            except OSError:
                stream.close()
                raise
            def capture():
                try:
                    with process.stdout, stream:
                        while chunk := process.stdout.read1(16384):
                            if os.fstat(stream.fileno()).st_size + len(chunk) > 2 * 1024 * 1024:
                                stream.seek(0)
                                stream.truncate()
                            stream.write(chunk)
                except OSError:
                    log.exception("Web app log capture failed: %s", identifier)
                    if process.poll() is None:
                        process.terminate()

            log_thread = threading.Thread(target=capture, name=f"web-log-{identifier}", daemon=True)
            log_thread.start()
            state_file = data / "web-runtime.json"
            runtime = Runtime(identifier, process, port, token, approval, log_thread, state_file)
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise ExtensionError("App exited during startup; inspect its gateway log.")
                        try:
                            response = await client.get(
                                runtime.origin + web.health,
                                headers={"X-Hosaka-Gateway-Token": token},
                            )
                            if response.is_success:
                                atomic_private(state_file, {"origin": runtime.origin, "token": token})
                                self.active[identifier] = runtime
                                self.failures.pop(identifier, None)
                                return runtime
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(.1)
                raise ExtensionError("App readiness timed out; inspect its gateway log.")
            except BaseException:
                await asyncio.to_thread(runtime.stop)
                self.failures[identifier] = (approval, "App failed readiness; inspect gateway logs.")
                raise

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(2)
            for identifier, runtime in list(self.active.items()):
                crashed = False
                try:
                    entry, _ = await asyncio.to_thread(self.approved, identifier)
                    if self._approval_tuple(entry) != runtime.approval:
                        raise ExtensionError("Web approval changed.")
                    if runtime.process.poll() is not None:
                        crashed = True
                        raise ExtensionError("App stopped unexpectedly; inspect gateway logs.")
                except (OSError, ValueError) as exc:
                    async with self.locks.setdefault(identifier, asyncio.Lock()):
                        if self.active.get(identifier) is runtime:
                            await asyncio.to_thread(runtime.stop)
                            self.active.pop(identifier, None)
                            if crashed:
                                self.failures[identifier] = (runtime.approval, str(exc))
                            log.warning("Stopped web app %s: %s", identifier, exc)

    async def close(self) -> None:
        for runtime in list(self.active.values()):
            await asyncio.to_thread(runtime.stop)
        self.active.clear()
