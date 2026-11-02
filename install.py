"""Optional native application installation; never approves or enables code unless --dev is requested."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import venv

MARKER = "# Hosaka managed web command"
RESERVED_ENV = {"HOME", "PATH", "TMPDIR", "SYSTEMROOT"}


def launcher(prefix: Path, name: str, python: Path, module: str, core: Path):
    directory = prefix / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.is_symlink() or (target.exists() and MARKER not in target.read_text()):
        raise ValueError(f"Refusing to overwrite an unrelated command: {target}")
    fd, temporary = tempfile.mkstemp(prefix=".web-command-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            data = (
                f"export HOSAKA_DATA_DIR={shlex.quote(os.environ['HOSAKA_DATA_DIR'])}\n"
                if "HOSAKA_DATA_DIR" in os.environ else ""
            )
            stream.write(
                "#!/bin/sh\n" + MARKER + "\n"
                + f"export PYTHONPATH={shlex.quote(str(core))}\n"
                + f"export PATH={shlex.quote(str(directory))}:\"$PATH\"\n"
                + data
                + f"exec {shlex.quote(str(python))} -m {shlex.quote(module)} \"$@\"\n"
            )
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(source: Path, extra: str = "") -> Path:
    source = source.expanduser().resolve()
    if not (source / "pyproject.toml").is_file():
        raise ValueError(f"Expected an application checkout with pyproject.toml: {source}")
    environment = source / ".web-venv"
    python = environment / "bin/python"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    args = [str(python), "-m", "pip", "install"]
    if (source / "requirements.txt").is_file():
        args += ["-r", str(source / "requirements.txt")]
    args += ["-e", str(source) + extra]
    subprocess.run(args, check=True)
    return python


def seed_virgo_config(data_root: Path, virgozone: Path, citations: Path | None = None) -> Path | None:
    virgozone = virgozone.expanduser().resolve()
    virgo_data = data_root / "extension-data/virgo-zone"
    virgo_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    canonical_db = virgo_data / "virgo-zone.db"
    legacy_data = virgozone / "data"
    for old_db in (virgo_data / "app.db", legacy_data / "virgo-zone.db", legacy_data / "app.db"):
        if old_db.is_file():
            if not canonical_db.exists():
                with sqlite3.connect(old_db) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                shutil.move(str(old_db), str(canonical_db))
                os.chmod(canonical_db, 0o600)
            else:
                old_db.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(old_db) + suffix).unlink(missing_ok=True)
    for folder in ("uploads", "thumbnails"):
        src_folder = legacy_data / folder
        dst_folder = virgo_data / folder
        if src_folder.is_dir():
            dst_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for item in src_folder.iterdir():
                target = dst_folder / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))
            shutil.rmtree(src_folder, ignore_errors=True)
    if (legacy_data / "citations").is_dir():
        shutil.rmtree(legacy_data / "citations", ignore_errors=True)
    if legacy_data.is_dir() and not any(legacy_data.iterdir()):
        legacy_data.rmdir()
    sys.path.insert(0, str(virgozone / "web/local"))
    from app.db import Database as VirgoDatabase
    VirgoDatabase(canonical_db).initialize()
    os.chmod(canonical_db, 0o600)
    config_path = virgo_data / "web-config.json"
    values: dict[str, str] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                values.update({str(k): str(v) for k, v in loaded.items()})
        except ValueError:
            pass
    dotenv = virgozone / ".env"
    if dotenv.is_file():
        for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if (
                key.isupper() and key.replace("_", "").isalnum()
                and not key.startswith(("HOSAKA_", "PYTHON", "LD_", "DYLD_"))
                and key not in RESERVED_ENV
            ):
                values.setdefault(key, value)
    values["DATA_DIR"] = str(virgo_data)
    values["CITATIONS_DATA_DIR"] = str(data_root / "extension-data/citations")
    if citations is not None:
        ext_dir = citations.expanduser().resolve() / "extension"
        if ext_dir.is_dir():
            values["CITATIONS_SOURCE"] = str(ext_dir)
    elif "CITATIONS_SOURCE" in values:
        candidate = Path(values["CITATIONS_SOURCE"]).expanduser().resolve()
        if (candidate / "extension/extension.yaml").is_file():
            values["CITATIONS_SOURCE"] = str(candidate / "extension")
    fd, temporary = tempfile.mkstemp(prefix=".web-config-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return config_path


def migrate_citations_data(manager, citations: Path) -> Path:
    old_data = manager.root / "extension-data/halloween-citations"
    new_data = manager.root / "extension-data/citations"
    if old_data.is_dir() and not new_data.exists():
        new_data.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(old_data, new_data)
    new_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with manager.transaction() as registry:
        if "halloween-citations" in registry.extensions:
            del registry.extensions["halloween-citations"]
    sys.path.insert(0, str(citations.expanduser().resolve() / "extension"))
    from citations.api import run
    from citations.cli import OperatorContext

    result = run("initialize", {}, OperatorContext(new_data))
    if not result.get("success"):
        raise ValueError(result.get("error") or "Failed to initialize citations.db")
    return new_data / "citations.db"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--citations", type=Path)
    parser.add_argument("--virgozone", type=Path)
    parser.add_argument("--link", action="store_true", help="Link extension packages into Hosaka registry")
    parser.add_argument("--dev", action="store_true", help="Link, approve and enable local dev web apps")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Install as the owning user, not root.")
    core = args.core.resolve()
    if not (core / "hosaka/paths.py").is_file():
        parser.error("--core must point at the Hosaka checkout.")
    sys.path.insert(0, str(core))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        python = install(Path(__file__).parent)
        launcher(args.prefix, "hosaka-gateway", python, "hosaka_gateway.cli", core)
        from hosaka.extensions.manager import Manager
        from hosaka_gateway.auth import configure

        manager = Manager()
        manager.init()
        if args.citations is not None:
            migrate_citations_data(manager, args.citations)
        if args.virgozone is not None:
            seed_virgo_config(manager.root, args.virgozone, args.citations)
        for source, extra, command, module, identifier in (
            (args.citations, "[web]", "cite", "citations.terminal", "citations"),
            (args.virgozone, "", "virgozone-start", "app.launcher", "virgo-zone"),
        ):
            if source is not None:
                resolved = source.expanduser().resolve()
                interpreter = install(resolved, extra)
                launcher(args.prefix, command, interpreter, module, core)
                if command == "cite":
                    launcher(args.prefix, "citations-setup", interpreter, "citations.cli", core)
                if args.link or args.dev:
                    manager.link(resolved / "extension")
                if args.dev:
                    manager.trust(
                        identifier,
                        allow_storage=True,
                        allow_managed_data=identifier == "citations",
                        allow_network=True,
                        allow_background=identifier == "citations",
                        allow_web=True,
                        web_source=resolved / "web/local",
                        web_python=interpreter,
                    )
                    manager.enable(identifier, True)
                    print(f"Installed, linked and enabled {identifier} ({command}).")
                else:
                    print(f"Installed {command}. Link {resolved / 'extension'} and inspect before granting web access.")
        if args.dev:
            auth_path = manager.root / "gateway/auth.json"
            if not auth_path.exists():
                configure(auth_path, secrets.token_urlsafe(24))
            print("Installed and configured gateway for local dev mode.")
        else:
            print("Installed gateway. Run hosaka-gateway configure, then explicitly approve/enable each app.")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Web installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
