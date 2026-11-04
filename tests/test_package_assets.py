"""Transport round trips, deterministic shards and hostile archive boundaries."""

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.package_assets import pack_assets, unpack_assets


class AssetBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = {
            "assets/manual/Storage/" + "long source folder " * 7 + "/fixture lumière.png": bytes(range(256)) * 34,
            "assets/manual/second.png": b"second image" * 800,
            "assets/releases/images/diagram.jp2": b"third image" * 800,
        }
        for relative, data in self.originals.items():
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (self.root / "assets/manual/ordinary.html").write_text("Keep ordinary text outside bundles")
        (self.root / "release-notes-pdf").mkdir()
        (self.root / "release-notes-pdf/source.pdf").write_bytes(b"original PDF")

    def tearDown(self):
        self.temporary.cleanup()

    def pack(self):
        return pack_assets(self.root, max_shard_bytes=20480)

    def manifest(self):
        return json.loads((self.root / "asset-bundles/manifest.json").read_text())

    def save_manifest(self, manifest):
        (self.root / "asset-bundles/manifest.json").write_text(json.dumps(manifest))

    def remove_images(self):
        for relative in self.originals:
            (self.root / relative).unlink()

    def replace_archive(self, manifest, members):
        """Keep transport hashes valid so tests exercise the tar/member boundary."""
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for info, data in members:
                archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
        compressed = gzip.compress(raw.getvalue(), mtime=0)
        digest = hashlib.sha256(compressed).hexdigest()
        bundle = manifest["bundles"][0]
        old = self.root / bundle["path"]
        bundle["path"] = f"asset-bundles/assets-0001-{digest}.tar.gz"
        bundle["sha256"] = digest
        bundle["size_bytes"] = len(compressed)
        bundle["uncompressed_bytes"] = len(raw.getvalue())
        (self.root / bundle["path"]).write_bytes(compressed)
        if old != self.root / bundle["path"]:
            old.unlink()
        self.save_manifest(manifest)

    def ordinary_members(self, bundle):
        result = []
        for item in bundle["files"]:
            info = tarfile.TarInfo(item["path"])
            data = self.originals[item["path"]]
            info.size = len(data)
            result.append((info, data))
        return result

    def test_exact_round_trip_multiple_shards_preserves_text_and_pdfs(self):
        packed = self.pack()
        self.assertGreater(packed["bundles"], 1)
        for bundle in self.manifest()["bundles"]:
            self.assertLessEqual(bundle["uncompressed_bytes"], 20480)
            self.assertEqual(len(gzip.decompress((self.root / bundle["path"]).read_bytes())), bundle["uncompressed_bytes"])
        self.remove_images()
        restored = unpack_assets(self.root)
        self.assertEqual(restored["restored"], len(self.originals))
        for relative, original in self.originals.items():
            self.assertEqual((self.root / relative).read_bytes(), original)
        self.assertEqual((self.root / "release-notes-pdf/source.pdf").read_bytes(), b"original PDF")
        self.assertIn("ordinary text", (self.root / "assets/manual/ordinary.html").read_text())
        self.assertEqual(unpack_assets(self.root)["restored"], 0)

    def test_pack_bytes_are_deterministic_despite_source_timestamps(self):
        self.pack()
        before = {path.name: path.read_bytes() for path in (self.root / "asset-bundles").iterdir()}
        for relative in self.originals:
            os.utime(self.root / relative, (123456789, 123456789))
        self.pack()
        after = {path.name: path.read_bytes() for path in (self.root / "asset-bundles").iterdir()}
        self.assertEqual(before, after)

    def test_no_bundles_keeps_original_local_workflow(self):
        self.assertEqual(unpack_assets(self.root)["status"], "not-packaged")

    def test_index_build_restores_a_fresh_checkout_before_indexing(self):
        self.pack()
        self.remove_images()
        data = self.root / "data/manual"
        data.mkdir(parents=True)
        document = {"id": "manual/example", "title": "Example", "kind": "manual", "category": "topic", "version": "2.5", "authority": "manual", "text": "A source image."}
        (data / "documents.jsonl").write_text(json.dumps(document) + "\n")
        script = Path(__file__).resolve().parents[1] / "scripts/build_index.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(self.root)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["documents"], 1)
        self.assertTrue((self.root / ".cache/catalog.sqlite").is_file())
        for relative, original in self.originals.items():
            self.assertEqual((self.root / relative).read_bytes(), original)

    def test_single_oversized_image_fails_without_publishing_manifest(self):
        (self.root / "assets/manual/large.png").write_bytes(b"x" * 21000)
        with self.assertRaisesRegex(ValueError, "Single asset"):
            self.pack()
        self.assertFalse((self.root / "asset-bundles/manifest.json").exists())

    def test_archive_hash_failure_restores_nothing(self):
        self.pack()
        archive = self.root / self.manifest()["bundles"][-1]["path"]
        archive.write_bytes(archive.read_bytes() + b"corrupt")
        self.remove_images()
        with self.assertRaisesRegex(ValueError, "Archive SHA256"):
            unpack_assets(self.root)
        self.assertTrue(all(not (self.root / path).exists() for path in self.originals))

    def test_member_hash_failure_restores_nothing_even_with_valid_archive_hash(self):
        self.pack()
        manifest = self.manifest()
        members = self.ordinary_members(manifest["bundles"][0])
        info, data = members[0]
        members[0] = (info, b"X" + data[1:])
        self.replace_archive(manifest, members)
        self.remove_images()
        with self.assertRaisesRegex(ValueError, "Asset SHA256"):
            unpack_assets(self.root)
        self.assertTrue(all(not (self.root / path).exists() for path in self.originals))

    def test_existing_newer_asset_is_never_overwritten(self):
        self.pack()
        path = self.root / next(iter(self.originals))
        path.write_bytes(b"a newer ingestion")
        with self.assertRaisesRegex(ValueError, "repack after ingestion"):
            unpack_assets(self.root)
        self.assertEqual(path.read_bytes(), b"a newer ingestion")

    def test_manifest_rejects_traversal_absolute_and_non_asset_paths(self):
        for unsafe in ["assets/manual/../../escape.png", "/assets/manual/escape.png", "assets/manual/./a.png", "assets/manual\\a.png", "web/a.png", "assets/manual/active.html", "assets/releases/other.png"]:
            with self.subTest(path=unsafe):
                self.pack()
                manifest = self.manifest()
                manifest["bundles"][0]["files"][0]["path"] = unsafe
                self.save_manifest(manifest)
                with self.assertRaises(ValueError):
                    unpack_assets(self.root)

    def test_archive_rejects_traversal_and_special_members(self):
        self.pack()
        for name, member_type in [("assets/manual/../../escape.png", tarfile.REGTYPE), ("/tmp/escape.png", tarfile.REGTYPE), ("assets/manual/second.png", tarfile.SYMTYPE), ("assets/manual/second.png", tarfile.LNKTYPE), ("assets/manual/second.png", tarfile.DIRTYPE), ("assets/manual/second.png", tarfile.FIFOTYPE)]:
            with self.subTest(name=name, member_type=member_type):
                self.pack()
                manifest = self.manifest()
                info = tarfile.TarInfo(name)
                info.type = member_type
                info.linkname = "../../escape.png" if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
                self.replace_archive(manifest, [(info, b"")])
                with self.assertRaises(ValueError):
                    unpack_assets(self.root)
        self.assertFalse((self.root / "escape.png").exists())

    def test_source_and_destination_symlinks_are_refused(self):
        symbolic = self.root / "assets/manual/link.png"
        symbolic.symlink_to(self.root / "release-notes-pdf/source.pdf")
        with self.assertRaisesRegex(ValueError, "Symbolic link"):
            self.pack()
        symbolic.unlink()
        self.pack()
        shutil.rmtree(self.root / "assets/manual")
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "assets/manual").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic link"):
            unpack_assets(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_or_missing_tar_members_are_refused(self):
        for duplicate in [True, False]:
            with self.subTest(duplicate=duplicate):
                self.pack()
                manifest = self.manifest()
                members = self.ordinary_members(manifest["bundles"][0])
                members = members + [members[0]] if duplicate else []
                self.replace_archive(manifest, members)
                with self.assertRaises(ValueError):
                    unpack_assets(self.root)

    def test_uncompressed_size_is_enforced_before_tar_reads(self):
        self.pack()
        manifest = self.manifest()
        manifest["bundles"][0]["uncompressed_bytes"] = 10240
        self.save_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "declared uncompressed size"):
            unpack_assets(self.root)


if __name__ == "__main__":
    unittest.main()
