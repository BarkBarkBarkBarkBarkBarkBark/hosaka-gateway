"""Disposable real-app browser/HTTP fixture. Stop with Ctrl-C to remove its data."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

import uvicorn

from hosaka.extensions.manager import Manager
from hosaka_gateway.auth import atomic_private, configure
from hosaka_gateway.server import create_app

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "hosaka_citations/extension"))
sys.path.insert(0, str(WORKSPACE / "hosaka_citations/scripts"))
from benchmark import fixture
from citations.cli import OperatorContext


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18986)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hosaka-web-fixture-") as directory:
        os.environ["HOSAKA_DATA_DIR"] = str(Path(directory) / "managed")
        manager = Manager()
        manager.init()
        for repository, identifier in (("hosaka_citations", "citations"), ("virgo_zone", "virgo-zone")):
            source = WORKSPACE / repository
            manager.link(source / "extension")
            manager.trust(
                identifier, allow_storage=True, allow_managed_data=identifier == "citations",
                allow_network=identifier == "virgo-zone", allow_web=True,
                web_source=source / "web/local", web_python=source / ".web-venv/bin/python",
            )
            manager.enable(identifier, True)
        config = manager.root / "extension-data/virgo-zone/web-config.json"
        atomic_private(config, {
            "DATA_DIR": str(Path(directory) / "virgo-data"),
            "CITATIONS_SOURCE": str(WORKSPACE / "hosaka_citations/extension"),
        })
        fixture(OperatorContext(manager.root / "extension-data/citations"), 1000)
        configure(manager.root / "gateway/auth.json", "synthetic-browser-password")
        print(f"DISPOSABLE_DATA={manager.root}", flush=True)
        uvicorn.run(create_app(manager), host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
