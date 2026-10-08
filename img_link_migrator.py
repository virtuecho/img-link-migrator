#!/usr/bin/env python3
"""Shared scanning, upload, cache, and safe-write core for the macOS app."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import mimetypes
import os
import pathlib
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
APP_VERSION = "1.0.0"
DEFAULT_PROVIDER = "imgbb"
IMGBB_CACHE_NAMESPACE = "imgbb"
PICGO_BASE_URL = "https://www.picgo.net"
MAX_IMAGE_BYTES = 100_000_000
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
class MigrationSummary:
    scanned_files: int = 0
    reference_count: int = 0
    unique_url_count: int = 0
    uploaded_urls: int = 0
    cached_urls: int = 0
    failed_urls: Dict[str, str] = dataclasses.field(default_factory=dict)
    updated_files: List[str] = dataclasses.field(default_factory=list)
    backup_dir: Optional[str] = None
    cancelled: bool = False


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
    # xhscdn is a host keyword; other rules retain domain/subdomain matching.
    if rule == "xhscdn":
        return "xhscdn" in host
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

    # Bare URLs are supported only in TXT exports and only for xhscdn hosts.
    # These spans are also used for replacement, so scanning and editing agree.
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
                url = original[start:end]
                try:
                    host = urllib.parse.urlsplit(url).hostname or ""
                except ValueError:
                    continue
                if "xhscdn" in host.lower():
                    add(url, offset + start, offset + end, "bare")

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
    def _entry_valid(entry: object) -> bool:
        # Legacy temporary uploads must not be reused as permanent images.
        return isinstance(entry, dict) and bool(entry.get("url")) and entry.get("expires_at") is None

    @staticmethod
    def _cache_key(value: str, namespace: str) -> str:
        if namespace == IMGBB_CACHE_NAMESPACE:
            return value
        return "{}\0{}".format(namespace, value)

    def lookup_url(
        self,
        original_url: str,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> Optional[str]:
        with self._lock:
            urls = self.data.get("urls", {})
            key = self._cache_key(original_url, namespace)
            entry = urls.get(key) if isinstance(urls, dict) else None
            if self._entry_valid(entry):
                return str(entry["url"])
        return None

    def lookup_hash(
        self,
        digest: str,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> Optional[str]:
        with self._lock:
            hashes = self.data.get("hashes", {})
            key = self._cache_key(digest, namespace)
            entry = hashes.get(key) if isinstance(hashes, dict) else None
            if self._entry_valid(entry):
                return str(entry["url"])
        return None

    def record(
        self,
        original_url: str,
        digest: str,
        new_url: str,
        namespace: str = IMGBB_CACHE_NAMESPACE,
    ) -> None:
        now = time.time()
        entry = {
            "url": new_url,
            "sha256": digest,
            "uploaded_at": now,
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


class BaseUploadClient:
    provider_name = "image host"
    cache_namespace = "default"

    def __init__(
        self,
        api_key: str,
        retries: int = 3,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.retries = retries
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()
        self.upload_rate_limiter = UploadRateLimiter()
        if not self.api_key:
            raise MigrationError(
                "Enter your {} API key in the app.".format(self.provider_name)
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
            if _host_matches(host, "xhscdn"):
                headers["Referer"] = "https://www.xiaohongshu.com/"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_IMAGE_BYTES:
                        raise MigrationError("Image exceeds the 100 MB download limit.")
                    chunks: List[bytes] = []
                    total = 0
                    while True:
                        self._check_cancelled()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise MigrationError("Image exceeds the 100 MB download limit.")
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
    cache_namespace = IMGBB_CACHE_NAMESPACE

    def upload(self, data: bytes, content_type: str, filename: str, url: str) -> str:
        def action() -> str:
            self.upload_rate_limiter.wait(self.cancel_event)
            boundary = "----ImgBBMigrator{}".format(uuid.uuid4().hex)
            body = _multipart_file_body(
                boundary, "image", data, content_type, filename
            )
            query = {"key": self.api_key}
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
    provider_name = "PicGo.net"
    cache_namespace = "chevereto:" + PICGO_BASE_URL

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
            fallback = "PicGo.net {} returned HTTP {}.".format(action, exc.code)
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
                "Could not reach PicGo.net: {}".format(exc.reason)
            ) from None
        except ValueError:
            raise MigrationError("PicGo.net returned an invalid response.") from None
        if not isinstance(payload, dict):
            raise MigrationError("PicGo.net returned an invalid response.")
        return payload

    def upload(self, data: bytes, content_type: str, filename: str, url: str) -> str:
        def action() -> str:
            self.upload_rate_limiter.wait(self.cancel_event)
            boundary = "----IMGLinkMigrator{}".format(uuid.uuid4().hex)
            fields: List[Tuple[str, str]] = [("format", "json")]
            body = _multipart_file_body(
                boundary,
                "source",
                data,
                content_type,
                filename,
                fields=fields,
            )
            request = urllib.request.Request(
                PICGO_BASE_URL + "/api/1/upload",
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
                    "PicGo.net upload failed: {}".format(
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
                    "PicGo.net response does not contain a valid image URL."
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
        backup_enabled: bool = True,
        cache_namespace: Optional[str] = None,
        image_processor=None,
    ) -> None:
        self.store = store
        self.image_processor = image_processor
        self.client = client
        self.include_hosts = tuple(include_hosts)
        self.exclude_hosts = tuple(exclude_hosts)
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()
        self.backup_enabled = backup_enabled
        self.cache_namespace = cache_namespace or getattr(
            client, "cache_namespace", IMGBB_CACHE_NAMESPACE
        )
        if image_processor:
            self.cache_namespace += ":" + image_processor.namespace

    def _write_completed_url(
        self,
        url: str,
        result: UrlResult,
        plans: Sequence[FilePlan],
        report: MigrationSummary,
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
    ) -> MigrationSummary:
        if plans is None:
            plans = scan_targets(
                targets,
                include_hosts=self.include_hosts,
                exclude_hosts=self.exclude_hosts,
                callback=self.callback,
            )
        else:
            plans = list(plans)
        original_plans = {plan.path: plan for plan in plans}
        if only_urls is not None:
            filtered: List[FilePlan] = []
            for plan in plans:
                refs = [ref for ref in plan.references if ref.url in only_urls]
                filtered.append(dataclasses.replace(plan, references=refs))
            plans = filtered

        all_refs = [ref for plan in plans for ref in plan.references]
        urls = list(dict.fromkeys(ref.url for ref in all_refs))
        report = MigrationSummary(
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
                    url, namespace=self.cache_namespace
                )
                if cached:
                    result = UrlResult(
                        url, cached, "cached", "Reused local migration cache."
                    )
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
                    if self.image_processor:
                        prepared = self.image_processor.prepare(data, filename, content_type)
                        data, content_type, filename = prepared.data, prepared.content_type, prepared.filename
                        _notify(self.callback, kind="image_prepared", url=url, **prepared.detail)
                    digest = hashlib.sha256(data).hexdigest()
                    hash_cached = self.store.lookup_hash(
                        digest, namespace=self.cache_namespace
                    )
                    if hash_cached:
                        self.store.record(
                            url,
                            digest,
                            hash_cached,
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
                            namespace=self.cache_namespace,
                        )
                        result = UrlResult(url, new_url, "uploaded", "Upload succeeded.")
                        report.uploaded_urls += 1
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

        # Carry our own writes into the scanned plans used by a later retry.
        for plan in plans:
            original_plans[plan.path].text = plan.text
            original_plans[plan.path].fingerprint = plan.fingerprint
        if report.updated_files and backup:
            report.backup_dir = str(backup.root)
        return report


def _parse_hosts(values: Sequence[str]) -> Tuple[str, ...]:
    hosts: List[str] = []
    for value in values:
        hosts.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(hosts))


def _provider_excluded_hosts(provider: str) -> Tuple[str, ...]:
    if provider == "imgbb":
        return IMGBB_EXCLUDED_HOSTS
    if provider == "chevereto":
        return ("picgo.net",)
    raise MigrationError("Unsupported upload provider: {}".format(provider))


def _create_upload_client(
    provider: str,
    api_key: str,
    retries: int,
    callback: Optional[ProgressCallback],
    cancel_event: threading.Event,
) -> BaseUploadClient:
    if provider == "imgbb":
        return ImgBBClient(
            api_key,
            retries=retries,
            callback=callback,
            cancel_event=cancel_event,
        )
    if provider == "chevereto":
        return CheveretoClient(
            api_key,
            retries=retries,
            callback=callback,
            cancel_event=cancel_event,
        )
    raise MigrationError("Unsupported upload provider: {}".format(provider))
