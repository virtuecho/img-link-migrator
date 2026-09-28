#!/usr/bin/env python3
"""Migrate external images in Markdown-syntax text files to image hosts.

The module provides both a CLI and a small Tk GUI.  It deliberately stores no
API key on disk; use the provider environment variable or paste the key into
the GUI for the current run.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import mimetypes
import os
import pathlib
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


APP_NAME = "IMG Link Migrator"
APP_VERSION = "0.6.0"
ENV_IMGBB_API_KEY = "IMGBB_API_KEY"
ENV_CHEVERETO_API_KEY = "CHEVERETO_API_KEY"
DEFAULT_PROVIDER = "chevereto"
IMGBB_CACHE_NAMESPACE = "imgbb"
DEFAULT_CHEVERETO_URL = "https://www.picgo.net"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
UPLOADS_PER_MINUTE = 50
MIN_UPLOAD_INTERVAL_SECONDS = 1.21
UPLOAD_RATE_COOLDOWN_SECONDS = 60.0
IMGBB_EXCLUDED_HOSTS = ("ibb.co", "i.ibb.co", "api.imgbb.com")
DEFAULT_EXCLUDED_HOSTS = IMGBB_EXCLUDED_HOSTS
USER_AGENT = "IMG-Link-Migrator/{}".format(APP_VERSION)


class MigrationError(RuntimeError):
    """A user-facing migration error."""


class CancelledError(MigrationError):
    """Raised when the user cancels a run."""


class UploadRateLimiter:
    """Serialize upload request starts across all workers for one run."""

    def __init__(
        self,
        interval_seconds: float = MIN_UPLOAD_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, cancel_event: threading.Event) -> None:
        while True:
            with self._lock:
                now = self._clock()
                delay = self._next_allowed - now
                if delay <= 0:
                    self._next_allowed = now + self.interval_seconds
                    return
            if cancel_event.wait(delay):
                raise CancelledError("Task cancelled.")

    def defer(self, seconds: float) -> None:
        with self._lock:
            self._next_allowed = max(
                self._next_allowed,
                self._clock() + seconds,
            )


def _is_upload_rate_error(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("flood", "rate limit", "too many requests")
    )


def _is_duplicate_upload_error(message: str) -> bool:
    normalized = message.lower()
    return "duplicated upload" in normalized or "duplicate upload" in normalized


@dataclasses.dataclass(frozen=True)
class ImageReference:
    path: pathlib.Path
    url: str
    start: int
    end: int
    syntax: str


@dataclasses.dataclass
class FilePlan:
    path: pathlib.Path
    text: str
    fingerprint: str
    references: List[ImageReference]


@dataclasses.dataclass
class UrlResult:
    original_url: str
    new_url: Optional[str] = None
    status: str = "pending"
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.new_url) and self.status in {"uploaded", "cached"}


@dataclasses.dataclass
class MigrationReport:
    scanned_files: int = 0
    reference_count: int = 0
    unique_url_count: int = 0
    uploaded_urls: int = 0
    cached_urls: int = 0
    failed_urls: Dict[str, str] = dataclasses.field(default_factory=dict)
    updated_files: List[str] = dataclasses.field(default_factory=list)
    backup_dir: Optional[str] = None
    cancelled: bool = False
    results: Dict[str, UrlResult] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "scanned_files": self.scanned_files,
            "reference_count": self.reference_count,
            "unique_url_count": self.unique_url_count,
            "uploaded_urls": self.uploaded_urls,
            "cached_urls": self.cached_urls,
            "failed_urls": self.failed_urls,
            "updated_files": self.updated_files,
            "backup_dir": self.backup_dir,
            "cancelled": self.cancelled,
            "results": {
                url: dataclasses.asdict(result) for url, result in self.results.items()
            },
        }


ProgressCallback = Callable[[Dict[str, object]], None]


def _notify(callback: Optional[ProgressCallback], **event: object) -> None:
    if callback:
        callback(event)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _app_data_dir() -> pathlib.Path:
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        root = os.environ.get("APPDATA")
        if root:
            return pathlib.Path(root) / APP_NAME
    root = os.environ.get("XDG_STATE_HOME")
    if root:
        return pathlib.Path(root) / "img-link-migrator"
    return pathlib.Path.home() / ".local" / "state" / "img-link-migrator"


def _host_matches(host: str, rule: str) -> bool:
    host = host.lower().strip(".")
    rule = rule.lower().strip().strip(".")
    return bool(rule) and (host == rule or host.endswith("." + rule))


def _url_allowed(
    url: str, include_hosts: Sequence[str], exclude_hosts: Sequence[str]
) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if any(_host_matches(host, item) for item in exclude_hosts):
        return False
    if include_hosts and not any(_host_matches(host, item) for item in include_hosts):
        return False
    return True


def _line_spans(text: str) -> Iterable[Tuple[int, str]]:
    offset = 0
    for line in text.splitlines(keepends=True):
        yield offset, line
        offset += len(line)
    if not text or text.endswith(("\n", "\r")):
        return


def _mask_inline_code(line: str) -> str:
    """Mask Markdown inline-code spans while preserving string offsets."""

    chars = list(line)
    pos = 0
    while pos < len(line):
        if line[pos] != "`":
            pos += 1
            continue
        end_ticks = pos
        while end_ticks < len(line) and line[end_ticks] == "`":
            end_ticks += 1
        marker = line[pos:end_ticks]
        closing = line.find(marker, end_ticks)
        if closing < 0:
            pos = end_ticks
            continue
        for idx in range(pos, closing + len(marker)):
            chars[idx] = " "
        pos = closing + len(marker)
    return "".join(chars)


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*(?:<(?P<angle>https?://[^>\r\n]+)>|"
    r"(?P<plain>https?://[^\s)\r\n]+))",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(
    r"""(?<![\w<"'=])https?://[^\s<"']+""",
    re.IGNORECASE,
)
_HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*?\s+src\s*=\s*(?:"
    r'"(?P<double>https?://[^\"]+)"|'
    r"'(?P<single>https?://[^']+)'|"
    r"(?P<plain>https?://[^\s>]+))",
    re.IGNORECASE,
)
_REFERENCE_USE_RE = re.compile(r"!\[[^\]\r\n]*\]\[(?P<id>[^\]\r\n]*)\]")
_REFERENCE_DEF_RE = re.compile(
    r"^(?P<prefix>\s*)\[(?P<id>[^\]\r\n]+)\]:\s*"
    r"(?:<(?P<angle>https?://[^>\s]+)>|(?P<plain>https?://\S+))",
    re.IGNORECASE,
)


def extract_image_references(
    path: pathlib.Path,
    text: str,
    include_hosts: Sequence[str] = (),
    exclude_hosts: Sequence[str] = DEFAULT_EXCLUDED_HOSTS,
) -> List[ImageReference]:
    """Return replaceable external-image URL spans outside metadata and code."""

    references: List[ImageReference] = []
    reference_ids: Set[str] = set()
    seen_spans: Set[Tuple[int, int]] = set()
    in_frontmatter = text.startswith("---\n") or text.startswith("---\r\n")
    frontmatter_done = not in_frontmatter
    in_fence = False
    fence_marker = ""
    allowed_lines: List[Tuple[int, str, str]] = []

    for offset, line in _line_spans(text):
        stripped = line.lstrip()
        if not frontmatter_done:
            if offset > 0 and stripped.rstrip("\r\n") in {"---", "..."}:
                frontmatter_done = True
            continue

        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue

        masked = _mask_inline_code(line)
        allowed_lines.append((offset, line, masked))
        for match in _REFERENCE_USE_RE.finditer(masked):
            ref_id = match.group("id").strip().casefold()
            if not ref_id:
                # Empty IDs use the alt text; resolving that adds ambiguity, so
                # leave them untouched instead of risking a wrong replacement.
                continue
            reference_ids.add(ref_id)

    def add(url: str, start: int, end: int, syntax: str) -> None:
        if (start, end) in seen_spans:
            return
        if not _url_allowed(url, include_hosts, exclude_hosts):
            return
        seen_spans.add((start, end))
        references.append(ImageReference(path, url, start, end, syntax))

    for offset, original, masked in allowed_lines:
        for match in _MARKDOWN_IMAGE_RE.finditer(masked):
            group = "angle" if match.group("angle") is not None else "plain"
            start, end = match.span(group)
            add(original[start:end], offset + start, offset + end, "markdown")

        for match in _HTML_IMAGE_RE.finditer(masked):
            group = next(
                name
                for name in ("double", "single", "plain")
                if match.group(name) is not None
            )
            start, end = match.span(group)
            add(original[start:end], offset + start, offset + end, "html")

        definition = _REFERENCE_DEF_RE.match(masked)
        if definition and definition.group("id").strip().casefold() in reference_ids:
            group = "angle" if definition.group("angle") is not None else "plain"
            start, end = definition.span(group)
            add(original[start:end], offset + start, offset + end, "reference")

    if path.suffix.lower() == ".txt":
        for offset, original, masked in allowed_lines:
            if _REFERENCE_DEF_RE.match(masked):
                continue
            for match in _BARE_URL_RE.finditer(masked):
                start, end = match.span()
                prefix = masked[:start]
                if re.search(r"\]\(\s*$", prefix) or re.search(
                    r"<[^>]*\b(?:src|href)\s*=\s*$", prefix, re.IGNORECASE
                ):
                    continue
                while end > start and original[end - 1] in ".,;)]}":
                    end -= 1
                add(original[start:end], offset + start, offset + end, "bare")

    references.sort(key=lambda item: item.start)
    return references


def discover_markdown_files(targets: Sequence[pathlib.Path]) -> List[pathlib.Path]:
    files: Set[pathlib.Path] = set()
    for raw_target in targets:
        target = raw_target.expanduser().resolve()
        if target.is_file() and target.suffix.lower() in {".md", ".markdown", ".txt"}:
            files.add(target)
            continue
        if target.is_dir():
            for path in target.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in {".md", ".markdown", ".txt"}
                    and not any(part.startswith(".") for part in path.relative_to(target).parts)
                ):
                    files.add(path.resolve())
            continue
        raise MigrationError(
            "Target does not exist or is not a supported text file/directory: {}".format(
                target
            )
        )
    return sorted(files, key=lambda path: str(path).casefold())


def scan_targets(
    targets: Sequence[pathlib.Path],
    include_hosts: Sequence[str] = (),
    exclude_hosts: Sequence[str] = DEFAULT_EXCLUDED_HOSTS,
    callback: Optional[ProgressCallback] = None,
) -> List[FilePlan]:
    paths = discover_markdown_files(targets)
    plans: List[FilePlan] = []
    for index, path in enumerate(paths, 1):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            _notify(
                callback,
                kind="warning",
                path=str(path),
                message="Skipped non-UTF-8 file: {}".format(exc),
            )
            continue
        references = extract_image_references(
            path, text, include_hosts=include_hosts, exclude_hosts=exclude_hosts
        )
        plans.append(FilePlan(path, text, _sha256_text(text), references))
        _notify(
            callback,
            kind="scan",
            path=str(path),
            references=len(references),
            current=index,
            total=len(paths),
        )
    return plans


class StateStore:
    """Persistent URL/content cache.  No credentials are stored."""

    def __init__(self, root: Optional[pathlib.Path] = None) -> None:
        self.root = (root or _app_data_dir()).expanduser().resolve()
        self.path = self.root / "state.json"
        self._lock = threading.Lock()
        self.data: Dict[str, object] = {"version": 1, "urls": {}, "hashes": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == 1:
                self.data = raw
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # A broken cache should never prevent a migration.
            return

    @staticmethod
    def _entry_valid(entry: object, required_expiration: int) -> bool:
        if not isinstance(entry, dict) or not entry.get("url"):
            return False
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return True
        if required_expiration == 0:
            # A temporary upload must not satisfy a request for permanent
            # storage, even when it has not expired yet.
            return False
        try:
            return float(expires_at) > time.time() + required_expiration + 60
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _cache_key(value: str, namespace: str) -> str:
        if namespace == IMGBB_CACHE_NAMESPACE:
            return value
        return "{}\0{}".format(namespace, value)

    def lookup_url(
        self,
        original_url: str,
        required_expiration: int = 0,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> Optional[str]:
        with self._lock:
            urls = self.data.get("urls", {})
            key = self._cache_key(original_url, namespace)
            entry = urls.get(key) if isinstance(urls, dict) else None
            if self._entry_valid(entry, required_expiration):
                return str(entry["url"])
        return None

    def lookup_hash(
        self,
        digest: str,
        required_expiration: int = 0,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> Optional[str]:
        with self._lock:
            hashes = self.data.get("hashes", {})
            key = self._cache_key(digest, namespace)
            entry = hashes.get(key) if isinstance(hashes, dict) else None
            if self._entry_valid(entry, required_expiration):
                return str(entry["url"])
        return None

    def record(
        self,
        original_url: str,
        digest: str,
        new_url: str,
        expiration: int,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> None:
        now = time.time()
        entry = {
            "url": new_url,
            "sha256": digest,
            "uploaded_at": now,
            "expires_at": now + expiration if expiration else None,
        }
        with self._lock:
            urls = self.data.setdefault("urls", {})
            hashes = self.data.setdefault("hashes", {})
            if isinstance(urls, dict):
                urls[self._cache_key(original_url, namespace)] = entry
            if isinstance(hashes, dict):
                hashes[self._cache_key(digest, namespace)] = entry
            self._save_locked()

    def _save_locked(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _media_type(content_type: str, url: str) -> Tuple[str, str]:
    """Choose upload metadata without validating the downloaded content."""

    content_type = content_type.split(";", 1)[0].strip().lower()
    path = urllib.parse.urlsplit(url).path
    if not content_type.startswith("image/"):
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    extension = mimetypes.guess_extension(content_type) or pathlib.Path(path).suffix
    return content_type, extension or ".img"


def _safe_filename(url: str, extension: str) -> str:
    raw_name = pathlib.PurePosixPath(urllib.parse.urlsplit(url).path).name
    raw_name = urllib.parse.unquote(raw_name)
    stem = pathlib.Path(raw_name).stem if raw_name else "image"
    stem = re.sub(r"[^0-9A-Za-z._-]+", "-", stem).strip("-._") or "image"
    suffix = pathlib.Path(raw_name).suffix if raw_name else ""
    if not suffix or len(suffix) > 10:
        suffix = extension
    return (stem[:80] + suffix.lower())[:100]


def _multipart_file_body(
    boundary: str,
    field_name: str,
    data: bytes,
    content_type: str,
    filename: str,
    fields: Sequence[Tuple[str, str]] = (),
) -> bytes:
    chunks: List[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                "--{0}\r\n"
                'Content-Disposition: form-data; name="{1}"\r\n\r\n'
                "{2}\r\n"
            ).format(boundary, name.replace('"', ""), value).encode("utf-8")
        )
    chunks.append(
        (
            "--{0}\r\n"
            'Content-Disposition: form-data; name="{1}"; filename="{2}"\r\n'
            "Content-Type: {3}\r\n\r\n"
        ).format(
            boundary,
            field_name.replace('"', ""),
            filename.replace('"', ""),
            content_type,
        ).encode("utf-8")
    )
    chunks.extend((data, "\r\n--{}--\r\n".format(boundary).encode("ascii")))
    return b"".join(chunks)


def _normalize_chevereto_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise MigrationError("Chevereto base URL must be a valid HTTPS URL.")
    if parsed.username or parsed.password:
        raise MigrationError("Chevereto base URL cannot contain credentials.")
    if parsed.query or parsed.fragment:
        raise MigrationError("Chevereto base URL cannot contain a query or fragment.")
    clean_path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, clean_path, "", "")
    )


class BaseUploadClient:
    provider_name = "image host"
    environment_variable = "API_KEY"
    cache_namespace = "default"

    def __init__(
        self,
        api_key: str,
        expiration: int = 0,
        retries: int = 3,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.expiration = expiration
        self.retries = retries
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()
        self.upload_rate_limiter = UploadRateLimiter()
        if not self.api_key:
            raise MigrationError(
                "Missing {} API key. Set {} or enter it in the GUI.".format(
                    self.provider_name, self.environment_variable
                )
            )
        if expiration and not 60 <= expiration <= 15_552_000:
            raise MigrationError(
                "Expiration must be 0 or between 60 and 15552000 seconds."
            )
        if retries < 0 or retries > 10:
            raise MigrationError("Retries must be between 0 and 10.")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError("Task cancelled.")

    def _retry(
        self, action: Callable[[], object], stage: str, url: str
    ) -> object:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            self._check_cancelled()
            try:
                return action()
            except CancelledError:
                raise
            except Exception as exc:  # Network/protocol failures become user-facing.
                last_error = exc
                if attempt > self.retries:
                    break
                if stage == "Upload" and _is_upload_rate_error(str(exc)):
                    delay = UPLOAD_RATE_COOLDOWN_SECONDS
                    self.upload_rate_limiter.defer(delay)
                else:
                    delay = min(8.0, 2 ** (attempt - 1)) + random.random() * 0.25
                _notify(
                    self.callback,
                    kind="retry",
                    url=url,
                    stage=stage,
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    delay=round(delay, 1),
                    message=str(exc),
                )
                if self.cancel_event.wait(delay):
                    raise CancelledError("Task cancelled.")
        raise MigrationError(
            "{} failed after {} attempt(s): {}".format(
                stage, self.retries + 1, last_error
            )
        )

    def download(self, url: str) -> Tuple[bytes, str, str]:
        def action() -> Tuple[bytes, str, str]:
            headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if _host_matches(host, "xhscdn.com"):
                headers["Referer"] = "https://www.xiaohongshu.com/"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_IMAGE_BYTES:
                        raise MigrationError("Image exceeds the 32 MB safety limit.")
                    chunks: List[bytes] = []
                    total = 0
                    while True:
                        self._check_cancelled()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise MigrationError("Image exceeds the 32 MB safety limit.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        raise MigrationError("Downloaded image is empty.")
                    content_type, extension = _media_type(
                        response.headers.get("Content-Type", ""), url
                    )
                    return data, content_type, _safe_filename(url, extension)
            except urllib.error.HTTPError as exc:
                raise MigrationError(
                    "Source server returned HTTP {}.".format(exc.code)
                ) from None
            except urllib.error.URLError as exc:
                raise MigrationError(
                    "Could not reach source server: {}".format(exc.reason)
                ) from None

        return self._retry(action, "Download", url)  # type: ignore[return-value]

    def upload(self, data: bytes, content_type: str, filename: str, url: str) -> str:
        raise NotImplementedError

    def transfer(self, url: str) -> Tuple[str, str]:
        data, content_type, filename = self.download(url)
        digest = hashlib.sha256(data).hexdigest()
        return self.upload(data, content_type, filename, url), digest


class ImgBBClient(BaseUploadClient):
    provider_name = "ImgBB"
    environment_variable = ENV_IMGBB_API_KEY
    cache_namespace = IMGBB_CACHE_NAMESPACE

    def upload(self, data: bytes, content_type: str, filename: str, url: str) -> str:
        def action() -> str:
            self.upload_rate_limiter.wait(self.cancel_event)
            boundary = "----ImgBBMigrator{}".format(uuid.uuid4().hex)
            body = _multipart_file_body(
                boundary, "image", data, content_type, filename
            )
            query = {"key": self.api_key}
            if self.expiration:
                query["expiration"] = str(self.expiration)
            endpoint = "https://api.imgbb.com/1/upload?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                    "Content-Length": str(len(body)),
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = "ImgBB returned HTTP {}.".format(exc.code)
                try:
                    payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                    api_message = payload.get("error", {}).get("message")
                    if api_message:
                        message += " {}".format(api_message)
                except (ValueError, AttributeError):
                    pass
                raise MigrationError(message) from None
            except urllib.error.URLError as exc:
                raise MigrationError(
                    "Could not reach ImgBB: {}".format(exc.reason)
                ) from None
            except ValueError:
                raise MigrationError("ImgBB returned an invalid response.") from None

            if not payload.get("success"):
                message = payload.get("error", {}).get("message", "ImgBB upload failed.")
                raise MigrationError(str(message))
            data_payload = payload.get("data") or {}
            new_url = data_payload.get("url") or data_payload.get("display_url")
            if not isinstance(new_url, str) or not new_url.startswith("https://"):
                raise MigrationError(
                    "ImgBB response does not contain a valid HTTPS image URL."
                )
            return new_url

        return self._retry(action, "Upload", url)  # type: ignore[return-value]


class CheveretoClient(BaseUploadClient):
    provider_name = "Chevereto"
    environment_variable = ENV_CHEVERETO_API_KEY

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_CHEVERETO_URL,
        expiration: int = 0,
        retries: int = 3,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.base_url = _normalize_chevereto_base_url(base_url)
        self.cache_namespace = "chevereto:{}".format(self.base_url.lower())
        super().__init__(
            api_key,
            expiration=expiration,
            retries=retries,
            callback=callback,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _error_message(payload: object, fallback: str) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            status = payload.get("status_txt")
            if status:
                return str(status)
        return fallback

    def _request_json(self, request: urllib.request.Request, action: str) -> Dict[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            fallback = "Chevereto {} returned HTTP {}.".format(action, exc.code)
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                error_message = self._error_message(payload, "")
                fallback += " {}".format(error_message)
                if isinstance(payload, dict) and _is_duplicate_upload_error(
                    error_message
                ):
                    image_payload = payload.get("image")
                    existing_url = (
                        image_payload.get("url")
                        if isinstance(image_payload, dict)
                        else None
                    )
                    parsed = urllib.parse.urlsplit(existing_url or "")
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        return payload
            except (ValueError, AttributeError):
                pass
            raise MigrationError(fallback.strip()) from None
        except urllib.error.URLError as exc:
            raise MigrationError(
                "Could not reach Chevereto: {}".format(exc.reason)
            ) from None
        except ValueError:
            raise MigrationError("Chevereto returned an invalid response.") from None
        if not isinstance(payload, dict):
            raise MigrationError("Chevereto returned an invalid response.")
        return payload

    def upload(self, data: bytes, content_type: str, filename: str, url: str) -> str:
        def action() -> str:
            self.upload_rate_limiter.wait(self.cancel_event)
            boundary = "----IMGLinkMigrator{}".format(uuid.uuid4().hex)
            fields: List[Tuple[str, str]] = [("format", "json")]
            if self.expiration:
                fields.append(("expiration", "PT{}S".format(self.expiration)))
            body = _multipart_file_body(
                boundary,
                "source",
                data,
                content_type,
                filename,
                fields=fields,
            )
            request = urllib.request.Request(
                self.base_url + "/api/1/upload",
                data=body,
                headers={
                    "X-API-Key": self.api_key,
                    "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                    "Content-Length": str(len(body)),
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            payload = self._request_json(request, "upload")
            error_message = self._error_message(payload, "unexpected response")
            duplicate_upload = _is_duplicate_upload_error(error_message)
            if (
                payload.get("error")
                or payload.get("status_code") not in {200, "200"}
            ) and not duplicate_upload:
                raise MigrationError(
                    "Chevereto upload failed: {}".format(
                        error_message
                    )
                )
            image_payload = payload.get("image")
            new_url = image_payload.get("url") if isinstance(image_payload, dict) else None
            if not isinstance(new_url, str):
                new_url = None
            parsed = urllib.parse.urlsplit(new_url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise MigrationError(
                    "Chevereto response does not contain a valid image URL."
                )
            return new_url

        return self._retry(action, "Upload", url)  # type: ignore[return-value]


class BackupSession:
    def __init__(self, state_root: pathlib.Path) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.root = state_root / "backups" / (stamp + "-" + uuid.uuid4().hex[:6])
        self.manifest: Dict[str, str] = {}

    def backup(self, path: pathlib.Path) -> pathlib.Path:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        destination = self.root / (digest + "-" + path.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        self.manifest[str(path)] = str(destination)
        self._write_manifest()
        return destination

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps({"files": self.manifest}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _replace_references(
    text: str,
    references: Sequence[ImageReference],
    results: Dict[str, UrlResult],
) -> str:
    output = text
    for reference in sorted(references, key=lambda item: item.start, reverse=True):
        result = results.get(reference.url)
        if result and result.succeeded and result.new_url:
            output = (
                output[: reference.start]
                + result.new_url
                + output[reference.end :]
            )
    return output


def _atomic_write(path: pathlib.Path, text: str) -> None:
    stat = path.stat()
    fd, temp_name = tempfile.mkstemp(
        prefix="." + path.name + "-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temp_name, stat.st_mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class MigrationEngine:
    def __init__(
        self,
        store: StateStore,
        client: Optional[BaseUploadClient] = None,
        include_hosts: Sequence[str] = (),
        exclude_hosts: Sequence[str] = DEFAULT_EXCLUDED_HOSTS,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        expiration: int = 0,
        backup_enabled: bool = True,
        cache_namespace: Optional[str] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.include_hosts = tuple(include_hosts)
        self.exclude_hosts = tuple(exclude_hosts)
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()
        self.expiration = expiration
        self.backup_enabled = backup_enabled
        self.cache_namespace = cache_namespace or getattr(
            client, "cache_namespace", IMGBB_CACHE_NAMESPACE
        )

    def _write_completed_url(
        self,
        url: str,
        result: UrlResult,
        plans: Sequence[FilePlan],
        report: MigrationReport,
        backup: Optional[BackupSession],
        backed_up_paths: Set[pathlib.Path],
    ) -> List[str]:
        """Immediately replace one completed URL before the next upload starts."""

        errors: List[str] = []
        for plan in plans:
            if not any(reference.url == url for reference in plan.references):
                continue
            try:
                current_text = plan.path.read_text(encoding="utf-8")
                if _sha256_text(current_text) != plan.fingerprint:
                    message = (
                        "File changed after scanning; write skipped to avoid "
                        "overwriting newer content."
                    )
                    errors.append("{}: {}".format(plan.path, message))
                    _notify(
                        self.callback,
                        kind="write_failed",
                        path=str(plan.path),
                        url=url,
                        message=message,
                    )
                    continue

                current_references = extract_image_references(
                    plan.path,
                    current_text,
                    include_hosts=self.include_hosts,
                    exclude_hosts=self.exclude_hosts,
                )
                applicable = [
                    reference
                    for reference in current_references
                    if reference.url == url
                ]
                if not applicable:
                    continue

                new_text = _replace_references(
                    current_text, applicable, {url: result}
                )
                if new_text == current_text:
                    continue
                if backup and plan.path not in backed_up_paths:
                    backup.backup(plan.path)
                    backed_up_paths.add(plan.path)
                _atomic_write(plan.path, new_text)
                plan.text = new_text
                plan.fingerprint = _sha256_text(new_text)
                path_text = str(plan.path)
                if path_text not in report.updated_files:
                    report.updated_files.append(path_text)
                _notify(
                    self.callback,
                    kind="file_updated",
                    path=path_text,
                    url=url,
                    replacements=len(applicable),
                )
            except Exception as exc:
                message = "Write failed: {}".format(exc)
                errors.append("{}: {}".format(plan.path, message))
                _notify(
                    self.callback,
                    kind="write_failed",
                    path=str(plan.path),
                    url=url,
                    message=message,
                )
        return errors

    def run(
        self,
        targets: Sequence[pathlib.Path],
        apply: bool,
        only_urls: Optional[Set[str]] = None,
        plans: Optional[Sequence[FilePlan]] = None,
    ) -> MigrationReport:
        if plans is None:
            plans = scan_targets(
                targets,
                include_hosts=self.include_hosts,
                exclude_hosts=self.exclude_hosts,
                callback=self.callback,
            )
        else:
            plans = list(plans)
        if only_urls is not None:
            filtered: List[FilePlan] = []
            for plan in plans:
                refs = [ref for ref in plan.references if ref.url in only_urls]
                filtered.append(dataclasses.replace(plan, references=refs))
            plans = filtered

        all_refs = [ref for plan in plans for ref in plan.references]
        urls = list(dict.fromkeys(ref.url for ref in all_refs))
        report = MigrationReport(
            scanned_files=len(plans),
            reference_count=len(all_refs),
            unique_url_count=len(urls),
        )
        _notify(
            self.callback,
            kind="discovered",
            files=report.scanned_files,
            references=report.reference_count,
            urls=report.unique_url_count,
            items=[
                {"url": ref.url, "path": str(ref.path), "syntax": ref.syntax}
                for ref in all_refs
            ],
        )
        if not apply or not urls:
            return report
        if self.client is None:
            raise MigrationError(
                "Internal error: upload client is missing for an apply run."
            )

        results: Dict[str, UrlResult] = {}
        backup = BackupSession(self.store.root) if self.backup_enabled else None
        backed_up_paths: Set[pathlib.Path] = set()
        try:
            for index, url in enumerate(urls, 1):
                if self.cancel_event.is_set():
                    raise CancelledError("Task cancelled.")
                first_ref = next(ref for ref in all_refs if ref.url == url)
                _notify(
                    self.callback,
                    kind="url_start",
                    url=url,
                    path=str(first_ref.path),
                    current=index,
                    total=len(urls),
                )
                cached = self.store.lookup_url(
                    url, self.expiration, namespace=self.cache_namespace
                )
                if cached:
                    result = UrlResult(
                        url, cached, "cached", "Reused local migration cache."
                    )
                    results[url] = result
                    report.cached_urls += 1
                    write_errors = self._write_completed_url(
                        url,
                        result,
                        plans,
                        report,
                        backup,
                        backed_up_paths,
                    )
                    if write_errors:
                        result.status = "failed"
                        result.detail = (
                            "Cached URL is ready, but Markdown write failed: {}"
                        ).format(
                            "; ".join(write_errors)
                        )
                        report.failed_urls[url] = result.detail
                    _notify(
                        self.callback,
                        kind="url_done",
                        url=url,
                        path=str(first_ref.path),
                        status=result.status,
                        new_url=cached,
                        message=result.detail,
                        current=index,
                        total=len(urls),
                    )
                    continue
                try:
                    data, content_type, filename = self.client.download(url)
                    digest = hashlib.sha256(data).hexdigest()
                    hash_cached = self.store.lookup_hash(
                        digest, self.expiration, namespace=self.cache_namespace
                    )
                    if hash_cached:
                        self.store.record(
                            url,
                            digest,
                            hash_cached,
                            self.expiration,
                            namespace=self.cache_namespace,
                        )
                        result = UrlResult(
                            url,
                            hash_cached,
                            "cached",
                            "Reused an uploaded image with identical content.",
                        )
                        report.cached_urls += 1
                    else:
                        new_url = self.client.upload(
                            data, content_type, filename, url
                        )
                        self.store.record(
                            url,
                            digest,
                            new_url,
                            self.expiration,
                            namespace=self.cache_namespace,
                        )
                        result = UrlResult(url, new_url, "uploaded", "Upload succeeded.")
                        report.uploaded_urls += 1
                    results[url] = result
                    write_errors = self._write_completed_url(
                        url,
                        result,
                        plans,
                        report,
                        backup,
                        backed_up_paths,
                    )
                    if write_errors:
                        result.status = "failed"
                        result.detail = (
                            "Image is ready, but Markdown write failed: {}"
                        ).format(
                            "; ".join(write_errors)
                        )
                        report.failed_urls[url] = result.detail
                    _notify(
                        self.callback,
                        kind="url_done",
                        url=url,
                        path=str(first_ref.path),
                        status=result.status,
                        new_url=result.new_url,
                        message=result.detail,
                        current=index,
                        total=len(urls),
                    )
                except CancelledError:
                    raise
                except Exception as exc:
                    result = UrlResult(url, None, "failed", str(exc))
                    results[url] = result
                    report.failed_urls[url] = str(exc)
                    _notify(
                        self.callback,
                        kind="url_done",
                        url=url,
                        path=str(first_ref.path),
                        status="failed",
                        message=str(exc),
                        current=index,
                        total=len(urls),
                    )
        except CancelledError:
            report.cancelled = True

        report.results = results
        if report.updated_files and backup:
            report.backup_dir = str(backup.root)
        return report


def _parse_hosts(values: Sequence[str]) -> Tuple[str, ...]:
    hosts: List[str] = []
    for value in values:
        hosts.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(hosts))


def _provider_environment_variable(provider: str) -> str:
    if provider == "imgbb":
        return ENV_IMGBB_API_KEY
    if provider == "chevereto":
        return ENV_CHEVERETO_API_KEY
    raise MigrationError("Unsupported upload provider: {}".format(provider))


def _provider_excluded_hosts(
    provider: str, chevereto_url: str = DEFAULT_CHEVERETO_URL
) -> Tuple[str, ...]:
    if provider == "imgbb":
        return IMGBB_EXCLUDED_HOSTS
    if provider == "chevereto":
        base_url = _normalize_chevereto_base_url(chevereto_url)
        host = urllib.parse.urlsplit(base_url).hostname
        if host == "www.picgo.net":
            host = "picgo.net"
        return (host,) if host else ()
    raise MigrationError("Unsupported upload provider: {}".format(provider))


def _provider_cache_namespace(
    provider: str, chevereto_url: str = DEFAULT_CHEVERETO_URL
) -> str:
    if provider == "imgbb":
        return IMGBB_CACHE_NAMESPACE
    if provider == "chevereto":
        return "chevereto:{}".format(
            _normalize_chevereto_base_url(chevereto_url).lower()
        )
    raise MigrationError("Unsupported upload provider: {}".format(provider))


def _create_upload_client(
    provider: str,
    api_key: str,
    expiration: int,
    retries: int,
    callback: Optional[ProgressCallback],
    cancel_event: threading.Event,
    chevereto_url: str = DEFAULT_CHEVERETO_URL,
) -> BaseUploadClient:
    if provider == "imgbb":
        return ImgBBClient(
            api_key,
            expiration=expiration,
            retries=retries,
            callback=callback,
            cancel_event=cancel_event,
        )
    if provider == "chevereto":
        return CheveretoClient(
            api_key,
            base_url=chevereto_url,
            expiration=expiration,
            retries=retries,
            callback=callback,
            cancel_event=cancel_event,
        )
    raise MigrationError("Unsupported upload provider: {}".format(provider))


class CLIProgress:
    """Render interactive progress in place; keep redirected output readable."""

    def __init__(self) -> None:
        self.stream = sys.stdout
        self.interactive = self.stream.isatty()
        self.lock = threading.Lock()
        self.total = 0
        self.completed = 0
        self.uploaded = 0
        self.reused = 0
        self.failed = 0
        self.active: Dict[str, str] = {}
        self.last_width = 0

    @staticmethod
    def _label(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        name = pathlib.PurePosixPath(parsed.path).name
        return "{}{}".format(parsed.hostname or url, "/" + name if name else "")

    def _render(self) -> None:
        if not self.interactive:
            return
        active = next(iter(self.active.values()), "idle")
        line = (
            "Progress {}/{} | uploaded {} | reused {} | failed {} | active: {}"
        ).format(
            self.completed,
            self.total,
            self.uploaded,
            self.reused,
            self.failed,
            active,
        )
        width = shutil.get_terminal_size((100, 20)).columns
        if len(line) > width:
            line = line[: max(0, width - 1)]
        self.stream.write("\r" + " " * self.last_width + "\r" + line)
        self.stream.flush()
        self.last_width = len(line)

    def _note(self, message: str) -> None:
        if self.interactive:
            self.stream.write("\r" + " " * self.last_width + "\r")
            self.stream.flush()
            self.last_width = 0
        print(message, file=sys.stderr)
        self._render()

    def __call__(self, event: Dict[str, object]) -> None:
        kind = event.get("kind")
        with self.lock:
            if kind == "discovered":
                self.total = int(event.get("urls", 0) or 0)
            elif kind == "url_start":
                url = str(event.get("url", ""))
                self.total = int(event.get("total", self.total) or self.total)
                self.active[url] = "Processing " + self._label(url)
                if not self.interactive:
                    print(
                        "[{}/{}] {}".format(
                            event.get("current"), event.get("total"), url
                        )
                    )
                self._render()
            elif kind == "retry":
                url = str(event.get("url", ""))
                stage = str(event.get("stage", "transfer"))
                self.active[url] = "Retrying {}: {}".format(
                    stage.lower(), self._label(url)
                )
                message = (
                    "Automatic retry: {stage} attempt {next_attempt} starts in "
                    "{delay}s ({message})"
                ).format(**event)
                if not self.interactive:
                    self._note(message)
                else:
                    self._render()
            elif kind == "url_done":
                url = str(event.get("url", ""))
                status = str(event.get("status", ""))
                self.active.pop(url, None)
                self.completed += 1
                if status == "uploaded":
                    self.uploaded += 1
                elif status in {"cached", "reused"}:
                    self.reused += 1
                elif status == "failed":
                    self.failed += 1
                if not self.interactive and status:
                    self._note("  {}: {}".format(status, event.get("message", "")))
                else:
                    self._render()
            elif kind == "file_updated":
                if not self.interactive:
                    self._note("  Updated: {}".format(event.get("path", "")))
            elif kind in {"warning", "write_failed"}:
                self._note(
                    "Warning: {} {}".format(
                        event.get("path", ""), event.get("message", "")
                    )
                )

    def finish(self) -> None:
        with self.lock:
            if self.interactive and self.last_width:
                self.stream.write("\n")
                self.stream.flush()
                self.last_width = 0


def _run_cli(args: argparse.Namespace) -> int:
    print("Bare HTTP(S) URLs are recognized if and only if the file is .txt.")
    targets = [pathlib.Path(item) for item in args.targets]
    if not targets:
        raise MigrationError("Specify at least one .txt, .md, .markdown file, or directory.")
    include_hosts = _parse_hosts(args.include_host)
    exclude_hosts = _provider_excluded_hosts(
        args.provider, args.chevereto_url
    ) + _parse_hosts(args.exclude_host)
    cache_namespace = _provider_cache_namespace(
        args.provider, args.chevereto_url
    )
    state_root = pathlib.Path(args.state_dir) if args.state_dir else None
    store = StateStore(state_root)
    cli_progress = CLIProgress()
    plans = scan_targets(
        targets,
        include_hosts=include_hosts,
        exclude_hosts=exclude_hosts,
        callback=cli_progress,
    )
    refs = [reference for plan in plans for reference in plan.references]
    urls = list(dict.fromkeys(reference.url for reference in refs))
    domain_urls: Dict[str, List[str]] = {}
    for url in urls:
        host = urllib.parse.urlsplit(url).hostname or "unknown"
        domain_urls.setdefault(host.lower(), []).append(url)
    domains = sorted(domain_urls)
    skipped_domains: Set[str] = set()

    def print_domain_choices() -> None:
        print("Image source domains:")
        for index, host in enumerate(domains, 1):
            state = " [skipped]" if host in skipped_domains else ""
            print(
                "  {}. {} ({} unique URL(s)){}".format(
                    index, host, len(domain_urls[host]), state
                )
            )

    print(
        "Scanned {} file(s), found {} image reference(s) / {} unique URL(s).".format(
            len(plans), len(refs), len(urls)
        )
    )
    print_domain_choices()

    if domains and sys.stdin.isatty() and not (args.apply and args.yes):
        while True:
            try:
                choice = input(
                    "Enter N to inspect, 'x N' (e.g. x 2) to skip, "
                    "'i N' to include, "
                    "or Return to continue: "
                ).strip()
            except EOFError:
                break
            if not choice:
                break

            parts = choice.split()
            action = "view"
            number = choice
            if len(parts) == 2 and parts[0].lower() in {"x", "i"}:
                action, number = parts[0].lower(), parts[1]
            if not number.isdigit() or not 1 <= int(number) <= len(domains):
                print(
                    "Use N, 'x N', or 'i N' with a domain number from 1 to {}; "
                    "press Return to continue.".format(len(domains))
                )
                continue
            host = domains[int(number) - 1]
            if action == "view":
                print("Image URL candidates for {}:".format(host))
                for url in domain_urls[host]:
                    print("  {}".format(url))
                while True:
                    try:
                        detail_prompt = (
                            "Press Return or 'b' to return to domains, or "
                            "'x' to skip {}: ".format(host)
                        )
                        detail_choice = input(
                            detail_prompt
                        ).strip().lower()
                    except EOFError:
                        detail_choice = "b"
                    if detail_choice in {"", "b"}:
                        break
                    if detail_choice == "x":
                        skipped_domains.add(host)
                        print("{} will be skipped during migration.".format(host))
                        break
                    print("Enter 'b' to return, 'x' to skip this domain, or press Return.")
                print_domain_choices()
            elif action == "x":
                skipped_domains.add(host)
                print("{} will be skipped during migration.".format(host))
                print_domain_choices()
            else:
                skipped_domains.discard(host)
                print("{} will be included in migration.".format(host))
                print_domain_choices()

    selected_urls = {
        url
        for url in urls
        if (urllib.parse.urlsplit(url).hostname or "unknown").lower()
        not in skipped_domains
    }
    if skipped_domains:
        print(
            "Skipping {} domain(s), {} unique URL(s).".format(
                len(skipped_domains), len(urls) - len(selected_urls)
            )
        )

    if args.apply and selected_urls and not args.yes:
        if not sys.stdin.isatty():
            raise MigrationError(
                "Migration needs confirmation. Run interactively or pass --yes."
            )
        try:
            answer = input("Start migration for these image URLs? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            print("Migration cancelled; no links were changed.")
            return 0

    cancel_event = threading.Event()
    client = None
    if args.apply and selected_urls:
        env_name = _provider_environment_variable(args.provider)
        client = _create_upload_client(
            args.provider,
            args.api_key or os.environ.get(env_name, ""),
            args.expiration,
            args.retries,
            cli_progress,
            cancel_event,
            chevereto_url=args.chevereto_url,
        )
    engine = MigrationEngine(
        store,
        client=client,
        include_hosts=include_hosts,
        exclude_hosts=exclude_hosts,
        callback=cli_progress,
        cancel_event=cancel_event,
        expiration=args.expiration,
        backup_enabled=not args.no_backup,
        cache_namespace=cache_namespace,
    )
    try:
        report = engine.run(
            targets,
            apply=args.apply,
            only_urls=selected_urls,
            plans=plans,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        cli_progress.finish()
        print("Task cancelled.", file=sys.stderr)
        return 130
    finally:
        cli_progress.finish()

    summary = (
        "Scanned {files} file(s), found {refs} reference(s) / {urls} unique "
        "URL(s); uploaded {uploaded}, reused {cached}, updated {updated} "
        "file(s), failed {failed}."
    ).format(
        files=report.scanned_files,
        refs=report.reference_count,
        urls=report.unique_url_count,
        uploaded=report.uploaded_urls,
        cached=report.cached_urls,
        updated=len(report.updated_files),
        failed=len(report.failed_urls),
    )
    print(summary)
    if report.failed_urls:
        print("Failed URLs:")
        for url, detail in report.failed_urls.items():
            print("  {}\n    {}".format(url, detail))
    if report.backup_dir:
        print("Backup: {}".format(report.backup_dir))
    if args.report:
        report_path = pathlib.Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("Report: {}".format(report_path))
    if report.cancelled:
        return 130
    return 2 if report.failed_urls else 0


class MigratorGUI:
    def __init__(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog, messagebox, ttk
        except ImportError as exc:
            raise MigrationError(
                "This Python installation does not provide Tk GUI support: {}".format(
                    exc
                )
            )

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1120x700")
        self.root.minsize(820, 560)

        self.provider_var = tk.StringVar(value="PicGo.net (Chevereto)")
        self.key_var = tk.StringVar(
            value=os.environ.get(ENV_CHEVERETO_API_KEY, "")
        )
        self.include_var = tk.StringVar()
        self.expiration_var = tk.StringVar(value="0")
        self.retries_var = tk.StringVar(value="3")
        self.backup_var = tk.BooleanVar(value=True)
        self.summary_var = tk.StringVar(
            value="Add one or more files or folders to scan."
        )
        self.progress_var = tk.DoubleVar(value=0)
        self.event_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.last_failed: Set[str] = set()
        self.last_domains: Tuple[str, ...] = ()
        self.last_domain_urls: Dict[str, List[str]] = {}
        self.skipped_domains: Set[str] = set()
        self.last_scan_urls: Set[str] = set()
        self.last_scan_targets: Optional[Tuple[pathlib.Path, ...]] = None
        self.has_scan = False
        self.apply_run = False
        self.domain_choice_var = tk.StringVar()
        self.rows: Dict[str, str] = {}

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._poll)

    def _build(self) -> None:
        ttk = self.ttk
        root = self.root
        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(10, weight=1)

        target_frame = ttk.LabelFrame(outer, text="Files and folders to scan", padding=4)
        target_frame.grid(row=0, column=0, columnspan=4, sticky="ew", pady=4)
        target_frame.columnconfigure(0, weight=1)
        self.target_listbox = self.tk.Listbox(
            target_frame, height=3, selectmode="extended", exportselection=False
        )
        self.target_listbox.grid(row=0, column=0, sticky="ew")
        target_scrollbar = ttk.Scrollbar(
            target_frame, orient="vertical", command=self.target_listbox.yview
        )
        target_scrollbar.grid(row=0, column=1, sticky="ns")
        self.target_listbox.configure(yscrollcommand=target_scrollbar.set)
        target_actions = ttk.Frame(target_frame)
        target_actions.grid(row=0, column=2, sticky="ns", padx=(6, 0))
        ttk.Button(target_actions, text="Add files", command=self._choose_file).pack(
            fill="x", pady=1
        )
        ttk.Button(
            target_actions, text="Add folder", command=self._choose_folder
        ).pack(fill="x", pady=1)
        ttk.Button(
            target_actions, text="Remove selected", command=self._remove_targets
        ).pack(fill="x", pady=1)

        ttk.Label(outer, text="Upload provider").grid(
            row=1, column=0, sticky="w", pady=4
        )
        provider_box = ttk.Combobox(
            outer,
            textvariable=self.provider_var,
            values=("PicGo.net (Chevereto)", "ImgBB"),
            state="readonly",
        )
        provider_box.grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=8, pady=4
        )
        provider_box.bind("<<ComboboxSelected>>", self._provider_changed)

        self.key_label = ttk.Label(outer, text="PicGo.net API key")
        self.key_label.grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.key_var, show="*").grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=8, pady=4
        )

        ttk.Label(outer, text="Include hosts").grid(
            row=3, column=0, sticky="w", pady=4
        )
        ttk.Entry(outer, textvariable=self.include_var).grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=8, pady=4
        )
        ttk.Label(
            outer,
            text=(
                "Leave empty for every external image host; separate hosts "
                "with commas. Existing links from the selected destination "
                "are skipped.\nBare HTTP(S) URLs are recognized if and only "
                "if the file is .txt."
            ),
            wraplength=1000,
            justify="left",
        ).grid(row=4, column=1, columnspan=3, sticky="w", padx=8)

        options = ttk.Frame(outer)
        options.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 6))
        ttk.Label(options, text="Expiration (seconds; 0 = permanent)").pack(
            side="left"
        )
        ttk.Entry(options, textvariable=self.expiration_var, width=12).pack(
            side="left", padx=(6, 20)
        )
        ttk.Label(options, text="Automatic retries").pack(side="left")
        ttk.Spinbox(
            options, from_=0, to=10, textvariable=self.retries_var, width=5
        ).pack(side="left", padx=6)
        ttk.Checkbutton(
            options, text="Back up before changes", variable=self.backup_var
        ).pack(side="left", padx=(20, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=6)
        self.scan_button = ttk.Button(actions, text="Scan", command=self._scan)
        self.scan_button.pack(side="left")
        self.migrate_button = ttk.Button(
            actions, text="Start migration", command=self._migrate
        )
        self.migrate_button.pack(side="left", padx=6)
        self.retry_button = ttk.Button(
            actions, text="Retry failed", command=self._retry_failed, state="disabled"
        )
        self.retry_button.pack(side="left")
        self.cancel_button = ttk.Button(
            actions, text="Cancel", command=self._cancel, state="disabled"
        )
        self.cancel_button.pack(side="right")

        domain_frame = ttk.LabelFrame(outer, text="Image source domains", padding=6)
        domain_frame.grid(row=7, column=0, columnspan=5, sticky="ew", pady=(4, 6))
        domain_frame.columnconfigure(0, weight=1)
        self.domain_text = self.tk.Text(
            domain_frame, height=4, wrap="none", state="disabled"
        )
        self.domain_text.grid(row=0, column=0, sticky="ew")
        domain_scrollbar = ttk.Scrollbar(
            domain_frame, orient="vertical", command=self.domain_text.yview
        )
        domain_scrollbar.grid(row=0, column=1, sticky="ns")
        self.domain_text.configure(yscrollcommand=domain_scrollbar.set)

        inspect_frame = ttk.Frame(outer)
        inspect_frame.grid(row=8, column=0, columnspan=5, sticky="ew", pady=(0, 4))
        ttk.Label(inspect_frame, text="Domain number").pack(side="left")
        ttk.Entry(
            inspect_frame, textvariable=self.domain_choice_var, width=8
        ).pack(side="left", padx=6)
        ttk.Button(
            inspect_frame,
            text="Show candidate URLs",
            command=self._show_domain_urls,
        ).pack(side="left")
        ttk.Button(
            inspect_frame, text="Skip domain", command=self._skip_domain
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            inspect_frame, text="Include domain", command=self._include_domain
        ).pack(side="left", padx=6)

        ttk.Progressbar(
            outer, variable=self.progress_var, maximum=100, mode="determinate"
        ).grid(row=9, column=0, columnspan=4, sticky="ew", pady=(4, 8))

        columns = ("status", "url", "file", "detail")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings")
        self.tree.heading("status", text="Status")
        self.tree.heading("url", text="Original image URL")
        self.tree.heading("file", text="File")
        self.tree.heading("detail", text="Details")
        self.tree.column("status", width=90, stretch=False)
        self.tree.column("url", width=370)
        self.tree.column("file", width=260)
        self.tree.column("detail", width=250)
        self.tree.grid(row=10, column=0, columnspan=4, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=10, column=4, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        ttk.Label(outer, textvariable=self.summary_var, anchor="w").grid(
            row=11, column=0, columnspan=4, sticky="ew", pady=(8, 0)
        )

    def _provider_id(self) -> str:
        return (
            "chevereto"
            if self.provider_var.get().startswith("PicGo.net")
            else "imgbb"
        )

    def _provider_changed(self, _event: object = None) -> None:
        provider = self._provider_id()
        env_name = _provider_environment_variable(provider)
        self.key_label.configure(
            text="{} API key".format(
                "PicGo.net" if provider == "chevereto" else "ImgBB"
            )
        )
        self.key_var.set(os.environ.get(env_name, ""))

    def _choose_file(self) -> None:
        selected = self.filedialog.askopenfilenames(
            title="Choose Markdown text files",
            filetypes=[("Markdown text", "*.txt *.md *.markdown"), ("All files", "*")],
        )
        self._add_targets(selected)

    def _choose_folder(self) -> None:
        selected = self.filedialog.askdirectory(
            title="Choose a vault or directory"
        )
        if selected:
            self._add_targets((selected,))

    def _add_targets(self, selected: Sequence[str]) -> None:
        existing = set(self.target_listbox.get(0, "end"))
        for item in selected:
            path = str(pathlib.Path(item).expanduser().resolve())
            if path not in existing:
                self.target_listbox.insert("end", path)
                existing.add(path)

    def _remove_targets(self) -> None:
        for index in reversed(self.target_listbox.curselection()):
            self.target_listbox.delete(index)

    def _selected_targets(self) -> Tuple[pathlib.Path, ...]:
        return tuple(pathlib.Path(path) for path in self.target_listbox.get(0, "end"))

    def _settings(
        self, apply: bool
    ) -> Tuple[Tuple[pathlib.Path, ...], str, str, int, int, Tuple[str, ...], bool]:
        targets = self._selected_targets()
        if not targets or any(not target.exists() for target in targets):
            raise MigrationError("Add at least one existing file or folder.")
        try:
            expiration = int(self.expiration_var.get())
            retries = int(self.retries_var.get())
        except ValueError:
            raise MigrationError("Expiration and retries must be integers.")
        if expiration and not 60 <= expiration <= 15_552_000:
            raise MigrationError(
                "Expiration must be 0 or between 60 and 15552000 seconds."
            )
        if not 0 <= retries <= 10:
            raise MigrationError("Retries must be between 0 and 10.")
        provider = self._provider_id()
        if apply and not self.key_var.get().strip():
            raise MigrationError("Enter an API key for the selected provider.")
        hosts = _parse_hosts([self.include_var.get()])
        return (
            targets,
            provider,
            self.key_var.get().strip(),
            expiration,
            retries,
            hosts,
            bool(self.backup_var.get()),
        )

    def _scan(self) -> None:
        self.has_scan = False
        self.last_domains = ()
        self.last_domain_urls = {}
        self.skipped_domains = set()
        self.last_scan_urls = set()
        self.domain_choice_var.set("")
        self._update_domain_list()
        self.last_scan_targets = tuple(
            target.expanduser().resolve() for target in self._selected_targets()
        )
        self._start(False, None)

    def _migrate(self) -> None:
        if not self.has_scan:
            self.messagebox.showinfo(
                "Scan first", "Scan the selected files to review image links and domains."
            )
            return
        targets = tuple(
            target.expanduser().resolve() for target in self._selected_targets()
        )
        if targets != self.last_scan_targets:
            self.messagebox.showinfo(
                "Scan again", "The selected target changed. Scan it before migration."
            )
            return
        if not self.last_domains:
            self.messagebox.showinfo(APP_NAME, "The last scan found no image links.")
            return
        included_domains = "\n".join(
            "  {}. {} ({} unique URL(s))".format(
                index,
                host,
                len(self.last_domain_urls.get(host, [])),
            )
            for index, host in enumerate(self.last_domains, 1)
            if host not in self.skipped_domains
        )
        selected_urls = {
            url
            for url in self.last_scan_urls
            if (urllib.parse.urlsplit(url).hostname or "").lower()
            not in self.skipped_domains
        }
        if not selected_urls:
            self.messagebox.showinfo(
                APP_NAME, "All scanned domains are skipped. Include a domain to migrate links."
            )
            return
        skipped_text = "\nSkipped domains: {}".format(
            ", ".join(sorted(self.skipped_domains))
        ) if self.skipped_domains else ""
        if not self.messagebox.askyesno(
            "Confirm migration",
            "Domains to migrate:\n{}{}\n\n"
            "Successful uploads will modify the selected text files. "
            "{} Continue?".format(
                included_domains,
                skipped_text,
                "Original files will be backed up first."
                if self.backup_var.get()
                else "Backups are disabled for this run."
            ),
        ):
            return
        self._start(True, selected_urls)

    def _domain_for_choice(self) -> Optional[str]:
        if not self.has_scan:
            self.messagebox.showinfo(APP_NAME, "Scan files before selecting a domain.")
            return None
        if not self.last_domains:
            self.messagebox.showinfo(APP_NAME, "The scan found no image source domains.")
            return None
        try:
            index = int(self.domain_choice_var.get().strip())
        except ValueError:
            self.messagebox.showinfo(APP_NAME, "Enter a domain number from the list.")
            return None
        if not 1 <= index <= len(self.last_domains):
            self.messagebox.showinfo(
                APP_NAME,
                "Enter a number from 1 to {}.".format(len(self.last_domains)),
            )
            return None
        return self.last_domains[index - 1]

    def _skip_domain(self) -> None:
        host = self._domain_for_choice()
        if host is not None:
            self._set_domain_skipped(host, True)

    def _include_domain(self) -> None:
        host = self._domain_for_choice()
        if host is not None:
            self._set_domain_skipped(host, False)

    def _set_domain_skipped(self, host: str, skipped: bool) -> None:
        if skipped:
            self.skipped_domains.add(host)
            state = "skipped"
        else:
            self.skipped_domains.discard(host)
            state = "included"
        self._update_domain_list()
        self.summary_var.set("{} will be {} during migration.".format(host, state))

    def _update_domain_list(self) -> None:
        self.domain_text.configure(state="normal")
        self.domain_text.delete("1.0", "end")
        for index, host in enumerate(self.last_domains, 1):
            state = " [SKIPPED]" if host in self.skipped_domains else ""
            self.domain_text.insert(
                "end",
                "{}. {} ({} unique URL(s)){}\n".format(
                    index,
                    host,
                    len(self.last_domain_urls.get(host, [])),
                    state,
                ),
            )
        self.domain_text.configure(state="disabled")

    def _show_domain_urls(self) -> None:
        host = self._domain_for_choice()
        if host is None:
            return

        urls = self.last_domain_urls.get(host, [])
        window = self.tk.Toplevel(self.root)
        window.title("Image URLs from {}".format(host))
        window.geometry("900x500")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        text = self.tk.Text(window, wrap="none")
        text.grid(row=0, column=0, sticky="nsew")
        y_scrollbar = self.ttk.Scrollbar(
            window, orient="vertical", command=text.yview
        )
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar = self.ttk.Scrollbar(
            window, orient="horizontal", command=text.xview
        )
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        actions = self.ttk.Frame(window)
        actions.grid(row=2, column=0, columnspan=2, sticky="e", padx=6, pady=6)

        def set_domain_skipped(skipped: bool) -> None:
            self._set_domain_skipped(host, skipped)
            window.destroy()

        self.ttk.Button(
            actions, text="Return to domains", command=window.destroy
        ).pack(side="left", padx=4)
        self.ttk.Button(
            actions,
            text="Skip this domain",
            command=lambda: set_domain_skipped(True),
        ).pack(side="left", padx=4)
        self.ttk.Button(
            actions,
            text="Include this domain",
            command=lambda: set_domain_skipped(False),
        ).pack(side="left", padx=4)
        text.configure(
            yscrollcommand=y_scrollbar.set,
            xscrollcommand=x_scrollbar.set,
            state="normal",
        )
        text.insert("1.0", "\n".join(urls))
        text.configure(state="disabled")
        self.domain_choice_var.set("")

    def _retry_failed(self) -> None:
        if self.last_failed:
            self._start(True, set(self.last_failed))

    def _start(self, apply: bool, only_urls: Optional[Set[str]]) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            (
                targets,
                provider,
                api_key,
                expiration,
                retries,
                hosts,
                backup_enabled,
            ) = self._settings(apply)
        except MigrationError as exc:
            self.messagebox.showerror(APP_NAME, str(exc))
            return
        self.cancel_event = threading.Event()
        self.apply_run = apply
        self.progress_var.set(0)
        self.summary_var.set(
            "{} in progress...".format("Migration" if apply else "Scan")
        )
        if only_urls is None:
            self.rows.clear()
            for item in self.tree.get_children():
                self.tree.delete(item)
        self._set_busy(True)

        def callback(event: Dict[str, object]) -> None:
            self.event_queue.put(("event", event))

        def work() -> None:
            try:
                store = StateStore()
                client = None
                if apply:
                    client = _create_upload_client(
                        provider,
                        api_key,
                        expiration,
                        retries,
                        callback,
                        self.cancel_event,
                    )
                excluded_hosts = _provider_excluded_hosts(provider)
                engine = MigrationEngine(
                    store,
                    client=client,
                    include_hosts=hosts,
                    exclude_hosts=excluded_hosts,
                    callback=callback,
                    cancel_event=self.cancel_event,
                    expiration=expiration,
                    backup_enabled=backup_enabled,
                    cache_namespace=_provider_cache_namespace(provider),
                )
                report = engine.run(targets, apply=apply, only_urls=only_urls)
                self.event_queue.put(("done", report))
            except Exception as exc:
                self.event_queue.put(("error", exc))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.migrate_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.retry_button.configure(
                state="normal" if self.last_failed else "disabled"
            )

    def _row_for(self, url: str, path: str = "") -> str:
        row = self.rows.get(url)
        if row:
            return row
        row = self.tree.insert("", "end", values=("Pending", url, path, ""))
        self.rows[url] = row
        return row

    def _handle_event(self, event: Dict[str, object]) -> None:
        kind = event.get("kind")
        if kind == "discovered":
            if self.apply_run:
                return
            items = event.get("items", [])
            domain_urls: Dict[str, Set[str]] = {}
            urls: Set[str] = set()
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        url = str(item.get("url", ""))
                        urls.add(url)
                        host = urllib.parse.urlsplit(url).hostname
                        if host:
                            domain_urls.setdefault(host.lower(), set()).add(url)
            self.last_domains = tuple(sorted(domain_urls))
            self.last_domain_urls = {
                host: sorted(domain_urls[host]) for host in self.last_domains
            }
            self.skipped_domains.clear()
            self.last_scan_urls = urls
            self.has_scan = True
            self._update_domain_list()
            self.summary_var.set(
                "Found {} unique image URL(s) across {} reference(s) in {} domain(s). "
                "Enter a domain number to inspect URLs, or use Skip/Include domain.".format(
                    event.get("urls", 0),
                    event.get("references", 0),
                    len(self.last_domains),
                )
            )
        elif kind == "url_start":
            url = str(event.get("url", ""))
            row = self._row_for(url, str(event.get("path", "")))
            values = list(self.tree.item(row, "values"))
            values[0] = "Processing"
            values[3] = ""
            self.tree.item(row, values=values)
            total = int(event.get("total", 0) or 0)
            current = int(event.get("current", 0) or 0)
            self.progress_var.set((current - 1) * 100 / total if total else 0)
        elif kind == "retry":
            url = str(event.get("url", ""))
            row = self._row_for(url)
            values = list(self.tree.item(row, "values"))
            values[0] = "Retrying"
            values[3] = "Attempt {} in {} second(s)".format(
                event.get("next_attempt"), event.get("delay")
            )
            self.tree.item(row, values=values)
        elif kind == "url_done":
            url = str(event.get("url", ""))
            row = self._row_for(url, str(event.get("path", "")))
            label = {
                "uploaded": "Uploaded",
                "cached": "Reused",
                "failed": "Failed",
            }.get(str(event.get("status")), str(event.get("status")))
            values = list(self.tree.item(row, "values"))
            values[0] = label
            values[3] = str(event.get("message", ""))
            self.tree.item(row, values=values)
            total = int(event.get("total", 0) or 0)
            current = int(event.get("current", 0) or 0)
            self.progress_var.set(current * 100 / total if total else 0)
        elif kind in {"warning", "write_failed"}:
            self.summary_var.set(str(event.get("message", "Warning")))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "event":
                    self._handle_event(payload)  # type: ignore[arg-type]
                elif kind == "done":
                    report = payload
                    assert isinstance(report, MigrationReport)
                    self.apply_run = False
                    self.last_failed = set(report.failed_urls)
                    self._set_busy(False)
                    self.progress_var.set(100 if not report.cancelled else self.progress_var.get())
                    self.summary_var.set(
                        "Scanned {} file(s); uploaded {}, reused {}, updated {} "
                        "file(s), failed {}.{}".format(
                            report.scanned_files,
                            report.uploaded_urls,
                            report.cached_urls,
                            len(report.updated_files),
                            len(report.failed_urls),
                            " Cancelled." if report.cancelled else "",
                        )
                    )
                    if report.backup_dir:
                        self.summary_var.set(
                            self.summary_var.get()
                            + " Backup: "
                            + report.backup_dir
                        )
                    if report.failed_urls:
                        failures = "\n".join(
                            "{}: {}".format(url, detail)
                            for url, detail in report.failed_urls.items()
                        )
                        self.messagebox.showwarning(
                            APP_NAME, "All failed image URLs:\n\n" + failures
                        )
                elif kind == "error":
                    self.apply_run = False
                    self._set_busy(False)
                    self.messagebox.showerror(APP_NAME, str(payload))
                    self.summary_var.set("Task failed: {}".format(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.summary_var.set(
            "Cancelling after the current network request finishes..."
        )

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not self.messagebox.askyesno(
                "Quit",
                "A migration is still running. Cancel it and quit?",
            ):
                return
            self.cancel_event.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate external Markdown images to a supported image host."
    )
    parser.add_argument(
        "targets", nargs="*", help=".txt, .md, or .markdown files/directories"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gui", action="store_true", help="Open the desktop GUI")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Upload and replace links; without this flag, only scan",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm migration without prompting (for non-interactive use)",
    )
    parser.add_argument(
        "--provider",
        choices=("imgbb", "chevereto"),
        default=DEFAULT_PROVIDER,
        help="Upload provider: imgbb or chevereto (default: chevereto)",
    )
    parser.add_argument(
        "--chevereto-url",
        default=DEFAULT_CHEVERETO_URL,
        help="Chevereto site base URL (default: https://www.picgo.net)",
    )
    parser.add_argument(
        "--api-key",
        help=(
            "Selected provider API key; use IMGBB_API_KEY or "
            "CHEVERETO_API_KEY to keep it out of shell history"
        ),
    )
    parser.add_argument(
        "--expiration",
        type=int,
        default=0,
        help="Expiration in seconds: 0 or 60-15552000 (default: 0)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Automatic retries after download/upload failures (default: 3)",
    )
    parser.add_argument(
        "--include-host",
        action="append",
        default=[],
        help="Only migrate matching hosts; repeat or comma-separate values",
    )
    parser.add_argument(
        "--exclude-host",
        action="append",
        default=[],
        help="Exclude additional hosts; repeat or comma-separate values",
    )
    parser.add_argument(
        "--state-dir",
        help="Cache and backup directory (default: system app-data directory)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable backups before Markdown changes",
    )
    parser.add_argument("--report", help="Write a JSON report to this path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.gui or (not args.targets and not args.apply):
        try:
            MigratorGUI().run()
            return 0
        except MigrationError as exc:
            print("Error: {}".format(exc), file=sys.stderr)
            return 2
    try:
        return _run_cli(args)
    except MigrationError as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
