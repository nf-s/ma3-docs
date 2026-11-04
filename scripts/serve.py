#!/usr/bin/env python3
"""Serve the MA3 reference UI and read-only retrieval API."""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import serve
from app.auth import AuthConfigurationError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        serve(root, args.database or root / ".cache" / "catalog.sqlite", args.host, args.port)
    except AuthConfigurationError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
