"""Live capture validation and scope/provenance checks, without a console."""

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.capture_lua_api import build_report, capture, validate_descriptors


class LuaCaptureTests(unittest.TestCase):
    def test_rejects_malformed_and_duplicate_descriptors(self):
        for free in ([["Name", "args"]], [["Name", {}, "nothing"]], [["../Name", "", ""]], [["Name", "", ""], ["Name", "", ""]]):
            with self.subTest(free=free):
                with self.assertRaises(ValueError):
                    validate_descriptors({"free": free, "object": [["Other", "", ""]]})

    def test_same_name_in_different_scopes_retains_distinct_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / "data/live-lua"
            manual = root / "data/manual"
            live.mkdir(parents=True)
            manual.mkdir(parents=True)
            api = {"free": [["ToAddr", "string:address", "<handle>"]], "object": [["ToAddr", "handle", "string"]]}
            (live / "api-descriptors.json").write_text(json.dumps(api))
            digest = hashlib.sha256((live / "api-descriptors.json").read_bytes()).hexdigest()
            metadata = {"api_sha256": digest, "build": {"version": "2.5.1.1"}, "captured_at": "2026-10-29T00:00:00+00:00", "bridge_version": "0.49.0", "lua_version": "Lua 5.5"}
            (live / "capture.json").write_text(json.dumps(metadata))
            (manual / "documents.jsonl").write_text(json.dumps({"id": "manual/lua_object_toaddr", "title": "ToAddr(handle)", "category": "lua"}) + "\n")
            (manual / "commands.jsonl").write_text("")
            report = build_report(root)
            self.assertEqual(report["covered_signatures"], 1)
            self.assertEqual(report["missing_by_scope"], {"free": ["ToAddr"], "object": []})
            docs = [json.loads(line) for line in (live / "documents.jsonl").read_text().splitlines()]
            self.assertEqual(len(docs), 2)
            self.assertEqual(docs[0]["command"], "ToAddr")
            self.assertEqual(docs[0]["lua_scope"], "free")
            self.assertIn("&lt;handle&gt;", (root / docs[0]["html_path"]).read_text())
            overview = (root / docs[1]["html_path"]).read_text()
            self.assertIn("manual%2Flua_object_toaddr", overview)
            self.assertIn("live-lua%2Ffree-toaddr", overview)
            (live / "api-descriptors.json").write_text("{}")
            with self.assertRaises(ValueError):
                build_report(root)

    def test_temporary_lua_is_restored_when_token_capture_fails(self):
        class FakeBridge:
            def __init__(self):
                self.commands = []
                self.policies = []
                self.closed = False

            def request(self, operation, args):
                if operation == "ping":
                    return {"lua": {"enabled": False}, "build": {"version": "2.5.1.1"}}
                if operation == "api":
                    return {"free": [["Name", "", ""]], "object": [["Other", "", ""]]}
                raise ValueError("simulated token capture failure")

            def command(self, value):
                self.commands.append(value)
                return {"feedback": "OK"}

            def lua_enabled(self, value):
                self.policies.append(value)

            def close(self):
                self.closed = True

        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as folder, patch("scripts.capture_lua_api.Bridge", return_value=bridge):
            args = argparse.Namespace(root=Path(folder), port=9800, helplua=False, tokens=True, temporary_lua=True)
            with self.assertRaisesRegex(ValueError, "simulated token"):
                capture(args)
        self.assertEqual(bridge.commands, ['Plugin "gma3_mcp_bridge" "lua on"', 'Plugin "gma3_mcp_bridge" "lua off"'])
        self.assertEqual(bridge.policies, [True, False])
        self.assertTrue(bridge.closed)


if __name__ == "__main__":
    unittest.main()
