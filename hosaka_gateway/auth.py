from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import re
import stat
import tempfile
import time


COOKIE = "hosaka_gateway"


def private_directory(root: Path, path: Path) -> Path:
    current = root.resolve()
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Gateway data directories must not be symlinks.")
        current.mkdir(exist_ok=True, mode=0o700)
    return current


def atomic_private(path: Path, value: dict) -> None:
    if path.parent.is_symlink():
        raise ValueError("Gateway configuration directory must not be a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Gateway configuration must not be a symlink.")
    fd, temporary = tempfile.mkstemp(prefix=".gateway-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(path: Path, password: str) -> None:
    if len(password) < 12:
        raise ValueError("Use a gateway password of at least 12 characters.")
    salt = secrets.token_hex(16)
    atomic_private(path, {
        "version": 1, "salt": salt,
        "password_hash": hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex(),
        "signing_key": secrets.token_hex(32),
    })


class Auth:
    def __init__(self, path: Path):
        self.path = path
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Gateway auth configuration must be private (mode 0600).")
        self.settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(self.settings, dict) or type(self.settings.get("version")) is not int or self.settings["version"] != 1:
            raise ValueError("Unsupported gateway auth configuration.")
        for field, size in (("salt", 32), ("password_hash", 128), ("signing_key", 64)):
            if not isinstance(self.settings.get(field), str) or not re.fullmatch(rf"[0-9a-f]{{{size}}}", self.settings[field]):
                raise ValueError("Invalid gateway auth configuration.")
        self.key = bytes.fromhex(self.settings["signing_key"])
        if len(self.key) != 32:
            raise ValueError("Invalid gateway signing key.")
        self.failures: dict[str, list[float]] = {}
        self.modified = path.stat().st_mtime_ns

    def reload(self) -> None:
        if self.path.stat().st_mtime_ns != self.modified:
            replacement = Auth(self.path)
            self.settings, self.key, self.modified = replacement.settings, replacement.key, replacement.modified

    def password_ok(self, password: str) -> bool:
        self.reload()
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(self.settings["salt"]), n=16384, r=8, p=1,
        ).hex()
        return hmac.compare_digest(actual, self.settings["password_hash"])

    def session(self) -> str:
        self.reload()
        payload = base64.urlsafe_b64encode(json.dumps({
            "role": "organizer", "exp": int(time.time()) + 8 * 3600,
        }).encode()).decode().rstrip("=")
        signature = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def role(self, value: str | None) -> str | None:
        self.reload()
        if not value or len(value) > 1024:
            return None
        try:
            payload, signature = value.split(".")
            expected = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if data["role"] == "organizer" and type(data["exp"]) is int and data["exp"] > time.time():
                return "organizer"
        except (ValueError, KeyError, TypeError):
            return None
        return None

    def allow_login(self, client: str) -> bool:
        now = time.monotonic()
        self.failures = {
            key: [at for at in values if now - at < 300]
            for key, values in self.failures.items() if values and now - values[-1] < 300
        }
        if client not in self.failures and len(self.failures) >= 4096:
            return False
        bucket = self.failures.setdefault(client, [])
        if len(bucket) >= 5:
            return False
        bucket.append(now)
        return True
