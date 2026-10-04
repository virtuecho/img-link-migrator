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
        self.report = None
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

    @staticmethod
    def signature(config):
        return json.dumps({k: config.get(k) for k in
                           ("targets", "provider", "baseURL", "includeHosts", "excludeHosts")}, sort_keys=True)

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
            config = request.get("settings", {})
            provider = "imgbb" if config.get("provider", "imgbb") == "imgbb" else "chevereto"
            base_url = config.get("baseURL") or core.DEFAULT_CHEVERETO_URL
            include = core._parse_hosts([config.get("includeHosts", "")])
            exclude = core._parse_hosts([config.get("excludeHosts", "")]) + core._provider_excluded_hosts(provider, base_url)
            targets = [pathlib.Path(p) for p in config.get("targets", [])]
            signature = self.signature(config)
            action = request.get("action")
            self.secret = config.get("apiKey", "").strip() or core._provider_default_api_key(provider)
            if action == "scan":
                self.plans = core.scan_targets(targets, include, exclude, self.emit)
                self.scan_signature = signature
                refs = [r for p in self.plans for r in p.references]
                urls = list(dict.fromkeys(r.url for r in refs))
                self.report = None
                self.emit({"kind": "scan_done", "files": len(self.plans), "references": len(refs),
                           "urls": urls, "items": [dataclasses.asdict(r) | {"path": str(r.path)} for r in refs]})
                return
            if action not in {"migrate", "retry"}:
                raise core.MigrationError("Unknown app request.")
            if self.plans is None or signature != self.scan_signature:
                raise core.MigrationError("Settings changed. Scan again before migrating.")
            expiration = int(config.get("expiration", 0))
            retries = int(config.get("retries", 3))
            client = core._create_upload_client(provider, self.secret, expiration, retries, self.emit, self.cancel, base_url)
            store = core.StateStore(pathlib.Path(config["stateDir"]) if config.get("stateDir") else None)
            selected = set(request.get("selectedURLs", []))
            if action == "retry":
                selected &= set(self.report.failed_urls) if self.report else set()
            processor = ImageProcessor(provider, config.get("mode", "size_limit"),
                                       config.get("platformLimit"), self.cancel)
            engine = core.MigrationEngine(store, client, include, exclude, self.emit, self.cancel,
                                          expiration, config.get("backup", True), image_processor=processor)
            self.report = engine.run(targets, True, selected, self.plans)
            report = self.clean(self.report.as_dict())
            if config.get("reportPath"):
                core._atomic_write(pathlib.Path(config["reportPath"]), json.dumps(report, indent=2) + "\n")
            self.emit({"kind": "done", "report": report})
        except Exception as exc:
            self.emit({"kind": "error", "message": str(exc)})
        finally:
            self.emit({"kind": "idle"})


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
