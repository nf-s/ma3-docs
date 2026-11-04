#!/usr/bin/env python3
"""Build the local search database from generated JSONL records."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import build_index
from scripts.package_assets import unpack_assets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    unpack_assets(args.root.resolve())
    print(json.dumps(build_index(args.root.resolve(), args.database), indent=2))


if __name__ == "__main__":
    main()
