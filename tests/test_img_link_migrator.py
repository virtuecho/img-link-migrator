import json
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import img_link_migrator as migrator


class FakeClient:
    def __init__(self):
        self.download_calls = []
        self.upload_calls = []

    def download(self, url):
        self.download_calls.append(url)
        if "fail.example" in url:
            raise migrator.MigrationError("Simulated download failure")
        return ("bytes:" + url).encode(), "image/png", "image.png"

    def upload(self, data, content_type, filename, url):
        self.upload_calls.append(url)
        return "https://i.ibb.co/test/" + pathlib.PurePosixPath(
            migrator.urllib.parse.urlsplit(url).path
        ).name


class ImmediateEvent:
    def is_set(self):
        return False

    def wait(self, timeout):
        return False


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class SourcePolicyTests(unittest.TestCase):
    def test_source_files_are_ascii_only(self):
        source_files = list(PROJECT_DIR.glob("*.py"))
        source_files.extend(PROJECT_DIR.glob("*.command"))
        source_files.extend((PROJECT_DIR / "tests").glob("*.py"))
        source_files.append(PROJECT_DIR / "pyproject.toml")
        for path in source_files:
            with self.subTest(path=path.name):
                path.read_bytes().decode("ascii")

    def test_user_agent_uses_the_project_name_and_version(self):
        pyproject = (PROJECT_DIR / "pyproject.toml").read_text(encoding="ascii")
        match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        project_version = match.group(1)
        self.assertEqual(migrator.APP_VERSION, project_version)
        self.assertEqual(
            migrator.USER_AGENT,
            "IMG-Link-Migrator/{}".format(project_version),
        )
        command_text = (PROJECT_DIR / "img-link-migrator.command").read_text(
            encoding="ascii"
        )
        self.assertIn('readonly APP_VERSION="{}"'.format(project_version), command_text)


class ObservingClient(FakeClient):
    def __init__(self, note):
        super().__init__()
        self.note = note
        self.first_was_written_before_second_download = False

    def download(self, url):
        if url.endswith("/second.png"):
            current = self.note.read_text(encoding="utf-8")
            self.first_was_written_before_second_download = (
                "https://i.ibb.co/test/first.png" in current
                and "https://cdn.example/second.png" in current
            )
        return super().download(url)


class CancelAfterUploadClient(FakeClient):
    def __init__(self, cancel_event):
        super().__init__()
        self.cancel_event = cancel_event

    def upload(self, data, content_type, filename, url):
        uploaded = super().upload(data, content_type, filename, url)
        self.cancel_event.set()
        return uploaded


class ParserTests(unittest.TestCase):
    def test_extracts_supported_images_and_skips_code_and_imgbb(self):
        text = """---
source: "https://meta.example/cover.png"
---

![Xiaohongshu](https://sns-webpic-qc.xhscdn.com/a/b.webp)
<img alt="x" src="https://cdn.example.com/photo.jpg">
`![inline](https://skip.example/inline.png)`
![already](https://i.ibb.co/existing/image.png)

```md
![fenced](https://skip.example/fenced.png)
```

![reference][hero]
[hero]: https://cdn.example.com/hero.png
"""
        refs = migrator.extract_image_references(pathlib.Path("note.md"), text)
        self.assertEqual(
            [ref.url for ref in refs],
            [
                "https://sns-webpic-qc.xhscdn.com/a/b.webp",
                "https://cdn.example.com/photo.jpg",
                "https://cdn.example.com/hero.png",
            ],
        )

    def test_host_filter_accepts_subdomains(self):
        text = (
            "![a](https://a.xhscdn.com/a.png)\n"
            "![b](https://other.example/b.png)\n"
        )
        refs = migrator.extract_image_references(
            pathlib.Path("note.md"), text, include_hosts=("xhscdn.com",)
        )
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].url, "https://a.xhscdn.com/a.png")


class MigrationTests(unittest.TestCase):
    def test_each_uploaded_url_is_written_before_the_next_download(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            note = root / "note.md"
            note.write_text(
                "![first](https://cdn.example/first.png)\n"
                "![second](https://cdn.example/second.png)\n",
                encoding="utf-8",
            )
            client = ObservingClient(note)
            report = migrator.MigrationEngine(
                migrator.StateStore(root / "state"),
                client=client,
            ).run([note], apply=True)

            self.assertTrue(client.first_was_written_before_second_download)
            self.assertEqual(report.failed_urls, {})
            updated = note.read_text(encoding="utf-8")
            self.assertIn("https://i.ibb.co/test/first.png", updated)
            self.assertIn("https://i.ibb.co/test/second.png", updated)
            manifest = json.loads(
                (pathlib.Path(report.backup_dir) / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(manifest["files"]), 1)
            backup_text = pathlib.Path(
                next(iter(manifest["files"].values()))
            ).read_text(encoding="utf-8")
            self.assertIn("https://cdn.example/first.png", backup_text)
            self.assertIn("https://cdn.example/second.png", backup_text)

    def test_cancel_keeps_the_url_written_immediately_after_upload(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            note = root / "note.md"
            note.write_text(
                "![first](https://cdn.example/first.png)\n"
                "![second](https://cdn.example/second.png)\n",
                encoding="utf-8",
            )
            cancel_event = threading.Event()
            client = CancelAfterUploadClient(cancel_event)
            report = migrator.MigrationEngine(
                migrator.StateStore(root / "state"),
                client=client,
                cancel_event=cancel_event,
            ).run([note], apply=True)

            updated = note.read_text(encoding="utf-8")
            self.assertTrue(report.cancelled)
            self.assertIn("https://i.ibb.co/test/first.png", updated)
            self.assertIn("https://cdn.example/second.png", updated)

    def test_successful_links_are_replaced_and_failures_remain(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            note = root / "note.md"
            note.write_text(
                "![ok](https://cdn.example/ok.png)\n"
                "![again](https://cdn.example/ok.png)\n"
                "![fail](https://fail.example/no.png)\n",
                encoding="utf-8",
            )
            state = migrator.StateStore(root / "state")
            fake = FakeClient()
            engine = migrator.MigrationEngine(state, client=fake)
            report = engine.run([note], apply=True)

            updated = note.read_text(encoding="utf-8")
            self.assertEqual(fake.download_calls.count("https://cdn.example/ok.png"), 1)
            self.assertEqual(updated.count("https://i.ibb.co/test/ok.png"), 2)
            self.assertIn("https://fail.example/no.png", updated)
            self.assertEqual(report.uploaded_urls, 1)
            self.assertEqual(len(report.failed_urls), 1)
            self.assertEqual(len(report.updated_files), 1)
            self.assertTrue(pathlib.Path(report.backup_dir).exists())

    def test_scan_mode_never_calls_network_or_changes_file(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            note = root / "note.md"
            original = "![x](https://cdn.example/x.png)\n"
            note.write_text(original, encoding="utf-8")
            fake = FakeClient()
            engine = migrator.MigrationEngine(
                migrator.StateStore(root / "state"), client=fake
            )
            report = engine.run([root], apply=False)
            self.assertEqual(report.unique_url_count, 1)
            self.assertEqual(fake.download_calls, [])
            self.assertEqual(note.read_text(encoding="utf-8"), original)

    def test_backup_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            root = pathlib.Path(raw_dir)
            note = root / "note.md"
            note.write_text(
                "![x](https://cdn.example/x.png)\n", encoding="utf-8"
            )
            engine = migrator.MigrationEngine(
                migrator.StateStore(root / "state"),
                client=FakeClient(),
                backup_enabled=False,
            )
            report = engine.run([note], apply=True)
            self.assertEqual(len(report.updated_files), 1)
            self.assertIsNone(report.backup_dir)
            self.assertFalse((root / "state" / "backups").exists())

    def test_automatic_retry_reaches_a_later_success(self):
        events = []
        client = migrator.ImgBBClient(
            "fake-key",
            retries=2,
            callback=events.append,
            cancel_event=ImmediateEvent(),
        )
        attempts = []

        def flaky_action():
            attempts.append(True)
            if len(attempts) < 3:
                raise migrator.MigrationError("temporary failure")
            return "ok"

        result = client._retry(flaky_action, "test", "https://example.com/x.png")
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual([event["kind"] for event in events], ["retry", "retry"])

    def test_temporary_cache_does_not_satisfy_permanent_request(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            store = migrator.StateStore(pathlib.Path(raw_dir))
            store.data["urls"]["source"] = {
                "url": "https://i.ibb.co/temp.png",
                "expires_at": time.time() + 3600,
            }
            self.assertIsNone(store.lookup_url("source", required_expiration=0))
            self.assertEqual(
                store.lookup_url("source", required_expiration=600),
                "https://i.ibb.co/temp.png",
            )

    def test_provider_caches_are_isolated(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            store = migrator.StateStore(pathlib.Path(raw_dir))
            store.record(
                "source",
                "digest",
                "https://i.ibb.co/image.png",
                0,
                namespace="imgbb",
            )
            store.record(
                "source",
                "digest",
                "https://www.picgo.net/images/image.png",
                0,
                namespace="chevereto:https://www.picgo.net",
            )
            self.assertEqual(
                store.lookup_url("source", namespace="imgbb"),
                "https://i.ibb.co/image.png",
            )
            self.assertEqual(
                store.lookup_url(
                    "source", namespace="chevereto:https://www.picgo.net"
                ),
                "https://www.picgo.net/images/image.png",
            )


class CheveretoClientTests(unittest.TestCase):
    def test_upload_uses_chevereto_multipart_contract(self):
        responses = [
            FakeHTTPResponse(
                {
                    "status_code": 200,
                    "success": {"message": "file uploaded", "code": 200},
                    "image": {
                        "url": "https://www.picgo.net/images/example.png"
                    },
                    "status_txt": "OK",
                }
            ),
        ]
        client = migrator.CheveretoClient(
            "fake-chevereto-key",
            expiration=600,
            retries=0,
            cancel_event=ImmediateEvent(),
        )
        with mock.patch.object(
            migrator.urllib.request, "urlopen", side_effect=responses
        ) as urlopen:
            result = client.upload(
                b"\x89PNG\r\n\x1a\nimage",
                "image/png",
                "example.png",
                "https://source.example/example.png",
            )

        self.assertEqual(result, "https://www.picgo.net/images/example.png")
        self.assertEqual(urlopen.call_count, 1)
        upload_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(
            upload_request.full_url,
            "https://www.picgo.net/api/1/upload",
        )
        headers = {name.lower(): value for name, value in upload_request.header_items()}
        self.assertEqual(headers["x-api-key"], "fake-chevereto-key")
        self.assertIn(b'name="source"; filename="example.png"', upload_request.data)
        self.assertIn(b'name="format"', upload_request.data)
        self.assertIn(b'name="expiration"', upload_request.data)
        self.assertIn(b"PT600S", upload_request.data)

    def test_picgo_destination_is_excluded_for_chevereto(self):
        excluded = migrator._provider_excluded_hosts(
            "chevereto", "https://www.picgo.net/"
        )
        self.assertFalse(
            migrator._url_allowed(
                "https://cdn.picgo.net/images/example.png", (), excluded
            )
        )
        self.assertTrue(
            migrator._url_allowed(
                "https://cdn.example.com/images/example.png", (), excluded
            )
        )

    def test_chevereto_base_url_rejects_embedded_credentials(self):
        with self.assertRaises(migrator.MigrationError):
            migrator.CheveretoClient(
                "fake-chevereto-key",
                base_url="https://user:password@images.example.com",
            )


if __name__ == "__main__":
    unittest.main()
