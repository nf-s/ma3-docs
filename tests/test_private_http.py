"""Private hosting must protect every route that exposes corpus material."""

import base64
import json
import tempfile
import threading
import unittest
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.auth import AuthConfig
from app.catalog import build_index
from app.server import Handler


class PrivateHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        files = {
            "web/index.html": "<title>Private reference</title>",
            "web/app.js": "'use strict';",
            "assets/icon.png": "test image",
            "content/manual/sample.md": "# Sample",
            "release-notes-pdf/sample.pdf": "test PDF",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        records = self.root / "data/manual/documents.jsonl"
        records.parent.mkdir(parents=True)
        records.write_text(json.dumps({
            "id": "manual/sample", "title": "Sample", "kind": "manual",
            "category": "topic", "version": "2.5", "authority": "manual",
            "text": "Private corpus text", "markdown_path": "content/manual/sample.md",
        }) + "\n")
        build_index(self.root)
        password = "test-only-password-not-a-secret"
        config = AuthConfig.from_environment("0.0.0.0", {"MA3_DOCS_PASSWORD": password})
        self.authorization = "Basic " + base64.b64encode(f"ma3:{password}".encode()).decode()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
            Handler, root=self.root, database=self.root / ".cache/catalog.sqlite", auth=config,
        ))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, method="GET", headers=()):
        client = HTTPConnection("127.0.0.1", self.server.server_address[1])
        client.putrequest(method, path)
        for name, value in headers:
            client.putheader(name, value)
        client.endheaders()
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_unauthenticated_and_wrong_password_cannot_read_any_material(self):
        paths = ("/", "/web/app.js", "/assets/icon.png", "/content/manual/sample.md",
                 "/release-notes-pdf/sample.pdf", "/api/stats", "/api/search?q=private",
                 "/api/document?id=manual%2Fsample&format=markdown")
        for path in paths:
            for method in ("GET", "HEAD"):
                for headers in ((), (("Authorization", "Basic d3Jvbmc6d3Jvbmc="),)):
                    with self.subTest(path=path, method=method, headers=headers):
                        status, response_headers, body = self.request(path, method, headers)
                        self.assertEqual(status, 401)
                        self.assertIn("Basic", response_headers["WWW-Authenticate"])
                        self.assertEqual(response_headers["Cache-Control"], "no-store")
                        self.assertNotIn(b"Private corpus text", body)

    def test_valid_credentials_access_api_and_files_without_shared_caching(self):
        headers = (("Authorization", self.authorization),)
        for path in ("/", "/assets/icon.png", "/release-notes-pdf/sample.pdf", "/api/search?q=private"):
            status, response_headers, _ = self.request(path, headers=headers)
            self.assertEqual(status, 200)
            self.assertNotIn("public", response_headers["Cache-Control"])
            self.assertEqual(response_headers["Vary"], "Authorization")

    def test_duplicate_authorization_headers_are_rejected(self):
        status, _, _ = self.request("/", headers=(("Authorization", self.authorization),) * 2)
        self.assertEqual(status, 401)

    def test_public_health_check_discloses_only_readiness(self):
        status, _, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ready"})


if __name__ == "__main__":
    unittest.main()
