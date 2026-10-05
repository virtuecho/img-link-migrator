"""Private JSON-lines bridge between the native app and the migration core."""
import dataclasses
import json
import pathlib
import signal
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import img_link_migrator as core
from image_processing import ImageProcessor


class Service:
    def __init__(self):
        self.plans = None
        self.scan_signature = None
        self.failed_urls = set()
        self.cancel = threading.Event()
        self.worker = None
        self.output_lock = threading.Lock()
        self.secret = ""

    def clean(self, value):
        if isinstance(value, str):
            return value.replace(self.secret, "[REDACTED]") if self.secret else value
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    def emit(self, event):
        with self.output_lock:
            print(json.dumps(self.clean(event), ensure_ascii=False), flush=True)

    def scan_progress(self, event):
        if self.cancel.is_set():
            raise core.CancelledError("Scan stopped.")
        self.emit(event)

    @staticmethod
    def signature(config):
        return json.dumps({k: config.get(k) for k in
                           ("targets", "provider", "includeHosts", "excludeHosts")}, sort_keys=True)

    def execute(self, request):
        action = request.get("action")
        if action == "stop":
            self.cancel.set()
            return
        if self.worker and self.worker.is_alive():
            self.emit({"kind": "error", "message": "A task is already running."})
            return
        self.cancel = threading.Event()
        self.worker = threading.Thread(target=self.run, args=(request,))
        self.worker.start()

    def run(self, request):
        try:
            if request.get("action") == "self_check":
                self.emit({"kind": "self_check", "result": self_check()})
                return
            config = request.get("settings", {})
            provider = config.get("provider", "imgbb")
            include = core._parse_hosts([config.get("includeHosts", "xhscdn")])
            exclude = core._parse_hosts([config.get("excludeHosts", "")]) + core._provider_excluded_hosts(provider)
            targets = [pathlib.Path(p) for p in config.get("targets", [])]
            signature = self.signature(config)
            action = request.get("action")
            self.secret = config.get("apiKey", "").strip()
            if action == "scan":
                self.plans = core.scan_targets(targets, include, exclude, self.scan_progress)
                self.scan_signature = signature
                refs = [r for p in self.plans for r in p.references]
                urls = list(dict.fromkeys(r.url for r in refs))
                self.failed_urls.clear()
                self.emit({"kind": "scan_done", "files": len(self.plans), "references": len(refs),
                           "urls": urls, "items": [dataclasses.asdict(r) | {"path": str(r.path)} for r in refs]})
                return
            if action not in {"migrate", "retry"}:
                raise core.MigrationError("Unknown app request.")
            if self.plans is None or signature != self.scan_signature:
                raise core.MigrationError("Settings changed. Scan again before migrating.")
            retries = int(config.get("retries", 3))
            client = core._create_upload_client(provider, self.secret, retries, self.emit, self.cancel)
            store = core.StateStore(pathlib.Path(config["stateDir"]) if config.get("stateDir") else None)
            selected = set(request.get("selectedURLs", []))
            if action == "retry":
                selected &= self.failed_urls
            processor = ImageProcessor(provider, config.get("mode", "size_limit"), cancel=self.cancel)
            engine = core.MigrationEngine(store, client, include, exclude, self.emit, self.cancel,
                                          backup_enabled=config.get("backup", True), image_processor=processor)
            summary = engine.run(targets, True, selected, self.plans)
            if action == "migrate":
                self.failed_urls.clear()
            self.failed_urls.difference_update(selected)
            self.failed_urls.update(summary.failed_urls)
            self.emit({"kind": "done", "summary": {
                "uploaded_urls": summary.uploaded_urls, "cached_urls": summary.cached_urls,
                "failed_urls": len(summary.failed_urls), "updated_files": len(summary.updated_files),
                "cancelled": summary.cancelled, "backup_dir": summary.backup_dir}})
        except Exception as exc:
            self.emit({"kind": "error", "message": str(exc)})
        finally:
            self.worker = None
            self.emit({"kind": "idle"})


def self_check():
    """Private bundle diagnostic: codecs and migration, with no external uploads."""
    import base64
    import io
    import tempfile
    import pyvips
    from PIL import Image
    png = pyvips.Image.black(32, 24, bands=3).pngsave_buffer()
    processor = ImageProcessor()
    result = processor.prepare(png + bytes(1_000_000), "tiny.png")
    assert len(result.data) < 1_000_000
    assert pyvips.Image.new_from_buffer(result.data, "").width == 32
    bmp = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(bmp, format="BMP")
    assert processor.prepare(bmp.getvalue(), "wrong.jpg").filename == "wrong.bmp"
    heic = base64.b64decode("AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAWltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABwaWN0AAAAAAAAAAAAAAAAAAAAAA5waXRtAAAAAAABAAAAImlsb2MAAAAAREAAAQABAAAAAAGNAAEAAAAAAAAAMwAAACNpaW5mAAAAAAABAAAAFWluZmUCAAAAAAEAAGh2YzEAAAAA6WlwcnAAAADKaXBjbwAAAHZodmNDAQNwAAAAAAAAAAAAHvAA/P34+AAADwNgAAEAGEABDAH//wNwAAADAJAAAAMAAAMAHroCQGEAAQAqQgEBA3AAAAMAkAAAAwAAAwAeoCCBBZbqrprm4CGgwIAAAAMAgAAAAwCEYgABAAZEAcFzwYkAAAAUaXNwZQAAAAAAAABAAAAAQAAAAChjbGFwAAAAEAAAAAEAAAAQAAAAAf///9AAAAAC////0AAAAAIAAAAQcGl4aQAAAAADCAgIAAAAF2lwbWEAAAAAAAAAAQABBIECBIMAAAA7bWRhdAAAAC8oAa8TIWRjQPgQ92f/6/g7B4RaP9N7HpzshHTA0iCAm0BIk11QCxYQgId2pVbc+A==")
    converted = ImageProcessor("chevereto").prepare(heic, "phone.heic")
    assert converted.detail["format"] in {"avif", "webp"}
    class Client:
        cache_namespace = "bundle-check"
        def download(self, url):
            return png + bytes(1_000_000), "image/png", "sample.png"
        def upload(self, data, content_type, filename, url):
            assert len(data) < 1_000_000
            return "https://destination.example/" + filename
    with tempfile.TemporaryDirectory(prefix="img-link-migrator-check-") as folder:
        path = pathlib.Path(folder) / "sample.md"
        path.write_text("![sample](https://source.example/sample.png)\n", encoding="utf-8")
        store = core.StateStore(pathlib.Path(folder) / "state")
        report = core.MigrationEngine(store, Client(), image_processor=processor).run([path], True)
        assert report.uploaded_urls == 1 and not report.failed_urls and report.backup_dir
        assert "destination.example" in path.read_text()
        path.write_text("![sample](https://source.example/sample.png)\n", encoding="utf-8")
        cached = core.MigrationEngine(store, Client(), image_processor=processor).run([path], True)
        assert cached.cached_urls == 1
        return {"codec": result.detail["format"], "heic_conversion": converted.detail["format"],
                "uploaded": report.uploaded_urls, "cached": cached.cached_urls, "backup": True}


def main():
    service = Service()
    signal.signal(signal.SIGTERM, lambda *_: service.cancel.set())
    for line in sys.stdin:
        try:
            service.execute(json.loads(line))
        except (ValueError, TypeError) as exc:
            service.emit({"kind": "error", "message": "Invalid app request: " + str(exc)})
    service.cancel.set()
    if service.worker:
        service.worker.join()


if __name__ == "__main__":
    main()
