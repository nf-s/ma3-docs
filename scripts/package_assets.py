#!/usr/bin/env python3
"""Pack or restore deterministic, verified illustration bundles for transport."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOTS = ("assets/manual", "assets/releases/images")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".tif", ".tiff", ".jp2"}
MAX_SHARD_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
FORMAT = "ma3-docs-assets-v1"
BUNDLE_NAME = re.compile(r"assets-[0-9]{4,}-[0-9a-f]{64}\.tar\.gz\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def asset_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid asset path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe asset path: {value}")
    path = PurePosixPath(value)
    if not any(value.startswith(prefix + "/") for prefix in ASSET_ROOTS) or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"Asset is outside the illustration roots: {value}")
    return value


def local_path(root: Path, relative: str) -> Path:
    """Refuse links and non-directory parents, including links within the root."""
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            status = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(status.st_mode):
            raise ValueError(f"Symbolic link refused: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"Asset parent is not a directory: {relative}")
        if index == len(parts) - 1 and not stat.S_ISREG(status.st_mode):
            raise ValueError(f"Asset is not a regular file: {relative}")
    return current


def file_fingerprint(path: Path) -> tuple[int, int, int]:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    return status.st_size, status.st_mtime_ns, status.st_ino


def tar_info(path: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.size = size
    info.mode = 0o644
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    return info


def tar_size(payload_size: int) -> int:
    return ((payload_size + 1024 + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


def pack_assets(root: Path, max_shard_bytes: int = MAX_SHARD_BYTES) -> dict:
    root = root.resolve()
    if type(max_shard_bytes) is not int or not tarfile.RECORDSIZE <= max_shard_bytes <= MAX_SHARD_BYTES:
        raise ValueError(f"Shard limit must be between {tarfile.RECORDSIZE} and {MAX_SHARD_BYTES} bytes")
    sources = []
    for prefix in ASSET_ROOTS:
        source_root = root / prefix
        if source_root.is_symlink():
            raise ValueError(f"Symbolic link refused: {prefix}")
        if not source_root.exists():
            continue
        for directory, directories, names in os.walk(source_root, followlinks=False):
            for name in directories + names:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError(f"Symbolic link refused: {path.relative_to(root)}")
            for name in names:
                path = Path(directory) / name
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                relative = asset_path(path.relative_to(root).as_posix())
                path = local_path(root, relative)
                fingerprint = file_fingerprint(path)
                sources.append((relative, fingerprint))
    sources.sort()

    groups = []
    group = []
    payload_size = 0
    for relative, fingerprint in sources:
        size = fingerprint[0]
        info = tar_info(relative, size)
        entry_size = len(info.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8")) + ((size + 511) // 512) * 512
        if tar_size(entry_size) > max_shard_bytes:
            raise ValueError(f"Single asset exceeds the shard limit: {relative}")
        if group and tar_size(payload_size + entry_size) > max_shard_bytes:
            groups.append(group)
            group = []
            payload_size = 0
        group.append((relative, fingerprint))
        payload_size += entry_size
    if group:
        groups.append(group)

    destination = root / "asset-bundles"
    if destination.is_symlink():
        raise ValueError("Symbolic link refused: asset-bundles")
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".asset-pack-", dir=root) as temporary:
        staging = Path(temporary)
        bundles = []
        for index, group in enumerate(groups, 1):
            raw = staging / "payload.tar"
            inventory = []
            with tarfile.open(raw, "w", format=tarfile.PAX_FORMAT) as archive:
                for relative, fingerprint in group:
                    source = local_path(root, relative)
                    data = source.read_bytes()
                    if file_fingerprint(source) != fingerprint or len(data) != fingerprint[0]:
                        raise ValueError(f"Asset changed while packaging: {relative}")
                    archive.addfile(tar_info(relative, len(data)), io.BytesIO(data))
                    inventory.append({"path": relative, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            uncompressed_bytes = raw.stat().st_size
            if uncompressed_bytes > max_shard_bytes:
                raise ValueError("Tar size exceeded its calculated shard limit")
            compressed = staging / "payload.tar.gz"
            with raw.open("rb") as source, compressed.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as archive:
                    shutil.copyfileobj(source, archive)
            digest = sha256_file(compressed)
            filename = f"assets-{index:04d}-{digest}.tar.gz"
            compressed_bytes = compressed.stat().st_size
            if compressed_bytes > MAX_SHARD_BYTES:
                raise ValueError("Compressed shard exceeds the transport limit; use a smaller shard size")
            compressed.rename(staging / filename)
            raw.unlink()
            bundles.append({"path": f"asset-bundles/{filename}", "sha256": digest, "size_bytes": compressed_bytes, "uncompressed_bytes": uncompressed_bytes, "files": inventory})
        for relative, fingerprint in sources:
            if file_fingerprint(local_path(root, relative)) != fingerprint:
                raise ValueError(f"Asset changed while packaging: {relative}")
        manifest = {"format": FORMAT, "roots": list(ASSET_ROOTS), "max_uncompressed_bytes": max_shard_bytes, "file_count": len(sources), "total_bytes": sum(fingerprint[0] for _, fingerprint in sources), "bundles": bundles}
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for bundle in bundles:
            name = PurePosixPath(bundle["path"]).name
            os.replace(staging / name, destination / name)
        os.replace(manifest_path, destination / "manifest.json")
        current = {PurePosixPath(bundle["path"]).name for bundle in bundles}
        for old in destination.iterdir():
            if BUNDLE_NAME.fullmatch(old.name) and old.name not in current and old.is_file() and not old.is_symlink():
                old.unlink()
    return {"status": "packed", "files": len(sources), "bundles": len(bundles), "bytes": manifest["total_bytes"]}


def positive_size(value: object, maximum: int, allow_zero: bool = False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= maximum:
        raise ValueError("Invalid bundle size or count")
    return value


def valid_digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA256 digest")
    return value


def read_manifest(root: Path) -> dict | None:
    manifest_path = local_path(root, "asset-bundles/manifest.json")
    if not manifest_path.exists():
        if list((root / "asset-bundles").glob("*.tar.gz")):
            raise ValueError("Asset archives exist without their manifest")
        return None
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Asset manifest exceeds the size limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("roots") != list(ASSET_ROOTS):
        raise ValueError("Unsupported asset manifest")
    limit = positive_size(manifest.get("max_uncompressed_bytes"), MAX_SHARD_BYTES)
    if limit < tarfile.RECORDSIZE:
        raise ValueError("Invalid uncompressed shard limit")
    bundles = manifest.get("bundles")
    if not isinstance(bundles, list) or len(bundles) > 10000:
        raise ValueError("Invalid bundle list")
    seen_bundles = set()
    seen_files = set()
    total = 0
    for bundle in bundles:
        if not isinstance(bundle, dict):
            raise ValueError("Invalid bundle record")
        path = bundle.get("path")
        if not isinstance(path, str) or not path.startswith("asset-bundles/") or not BUNDLE_NAME.fullmatch(path.removeprefix("asset-bundles/")) or path in seen_bundles:
            raise ValueError("Invalid or duplicate archive path")
        seen_bundles.add(path)
        digest = valid_digest(bundle.get("sha256"))
        if not path.endswith(f"-{digest}.tar.gz"):
            raise ValueError("Content-addressed archive name does not match its digest")
        positive_size(bundle.get("size_bytes"), MAX_SHARD_BYTES)
        positive_size(bundle.get("uncompressed_bytes"), limit)
        files = bundle.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Empty or invalid archive inventory")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Invalid file inventory")
            relative = asset_path(item.get("path"))
            if relative in seen_files:
                raise ValueError(f"Duplicate asset path: {relative}")
            seen_files.add(relative)
            total += positive_size(item.get("size_bytes"), limit, allow_zero=True)
            valid_digest(item.get("sha256"))
    if type(manifest.get("file_count")) is not int or manifest["file_count"] != len(seen_files) or type(manifest.get("total_bytes")) is not int or manifest["total_bytes"] != total:
        raise ValueError("Asset inventory totals do not match")
    return manifest


def unpack_assets(root: Path) -> dict:
    """Verify every byte in staging, then restore missing illustrations only."""
    root = root.resolve()
    manifest = read_manifest(root)
    if manifest is None:
        return {"status": "not-packaged", "files": 0, "restored": 0, "bundles": 0}
    for bundle in manifest["bundles"]:
        archive = local_path(root, bundle["path"])
        if archive.stat().st_size != bundle["size_bytes"] or sha256_file(archive) != bundle["sha256"]:
            raise ValueError(f"Archive SHA256 or size mismatch: {bundle['path']}")
        for item in bundle["files"]:
            destination = local_path(root, item["path"])
            if destination.exists() and (destination.stat().st_size != item["size_bytes"] or sha256_file(destination) != item["sha256"]):
                raise ValueError(f"Existing asset differs from the bundle; repack after ingestion: {item['path']}")
    restored = 0
    with tempfile.TemporaryDirectory(prefix=".asset-unpack-", dir=root) as temporary:
        staging = Path(temporary)
        for bundle in manifest["bundles"]:
            raw = staging / "payload.tar"
            with gzip.open(local_path(root, bundle["path"]), "rb") as source, raw.open("wb") as output:
                remaining = bundle["uncompressed_bytes"]
                while True:
                    data = source.read(min(1024 * 1024, remaining + 1))
                    if not data:
                        break
                    remaining -= len(data)
                    if remaining < 0:
                        raise ValueError("Archive exceeds its declared uncompressed size")
                    output.write(data)
                if remaining:
                    raise ValueError("Archive is shorter than its declared uncompressed size")
            expected = {item["path"]: item for item in bundle["files"]}
            seen = set()
            with tarfile.open(raw, "r:") as archive:
                for member in archive:
                    relative = asset_path(member.name)
                    if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.sparse is not None or set(member.pax_headers) - {"path"}:
                        raise ValueError(f"Non-regular or extended archive member refused: {relative}")
                    if relative not in expected or relative in seen or member.size != expected[relative]["size_bytes"]:
                        raise ValueError(f"Archive member differs from its inventory: {relative}")
                    seen.add(relative)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"Missing archive file data: {relative}")
                    target = staging / relative
                    if target.exists():
                        raise ValueError(f"Asset paths collide on this filesystem: {relative}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with source, target.open("wb") as output:
                        for data in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(data)
                            output.write(data)
                    if digest.hexdigest() != expected[relative]["sha256"]:
                        raise ValueError(f"Asset SHA256 mismatch: {relative}")
                if seen != set(expected):
                    raise ValueError("Archive is missing inventoried assets")
            raw.unlink()
        for bundle in manifest["bundles"]:
            for item in bundle["files"]:
                destination = local_path(root, item["path"])
                if destination.exists():
                    if sha256_file(destination) != item["sha256"]:
                        raise ValueError(f"Asset changed during restoration: {item['path']}")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = staging / item["path"]
                source.chmod(0o644)
                os.replace(source, destination)
                restored += 1
    return {"status": "verified", "files": manifest["file_count"], "restored": restored, "bundles": len(manifest["bundles"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "unpack"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--max-shard-bytes", type=int, default=MAX_SHARD_BYTES)
    args = parser.parse_args()
    try:
        result = pack_assets(args.root, args.max_shard_bytes) if args.action == "pack" else unpack_assets(args.root)
    except (OSError, ValueError, tarfile.TarError, EOFError) as error:
        parser.exit(1, f"Asset packaging failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
