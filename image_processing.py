"""Two size policies backed by bundled libvips, libheif, WebP, and AV1 codecs."""
import dataclasses
import pathlib
import threading

POLICY_VERSION = "v1-avif-webp-q80-70-shrink85"
DOWNLOAD_LIMIT = 100_000_000
PLATFORM_LIMITS = {"imgbb": 32_000_000, "chevereto": 25_000_000}
COMMON_FORMATS = {"jpeg", "png", "bmp", "gif", "webp", "avif"}
IMGBB_FORMATS = COMMON_FORMATS | {"heic", "tiff", "svg", "jp2", "jxl", "ico", "psd"}


@dataclasses.dataclass
class PreparedImage:
    data: bytes
    content_type: str
    filename: str
    detail: dict


def container_chroma(data):
    """Read declared HEIF/AVIF chroma without changing the decoded pixels."""
    pos = data.find(b"av1C")
    if 0 <= pos <= len(data) - 7:
        flags = data[pos + 6]
        if flags & 16:
            return "400"
        return "420" if flags & 8 and flags & 4 else "422" if flags & 8 else "444"
    pos = data.find(b"hvcC")
    if 0 <= pos <= len(data) - 21:
        return {0: "400", 1: "420", 2: "422", 3: "444"}.get(data[pos + 20] & 3)
    return None


class ImageProcessor:
    def __init__(self, provider="imgbb", mode="size_limit", platform_limit=None, cancel=None):
        if mode not in {"size_limit", "original"}:
            raise ValueError("Unknown image processing mode.")
        self.mode = mode
        self.platform_limit = int(platform_limit or PLATFORM_LIMITS[provider])
        if not 1_000_000 <= self.platform_limit <= DOWNLOAD_LIMIT:
            raise ValueError("Platform limit must be between 1 and 100 MB.")
        self.limit = min(999_999, self.platform_limit) if mode == "size_limit" else self.platform_limit
        self.formats = IMGBB_FORMATS if provider == "imgbb" else COMMON_FORMATS
        self.cancel = cancel or threading.Event()
        self.namespace = f"{POLICY_VERSION}:{mode}:{self.platform_limit}"

    def check_cancel(self):
        if self.cancel.is_set():
            from img_link_migrator import CancelledError
            raise CancelledError("Task cancelled.")

    def prepare(self, data, filename):
        import pyvips
        self.check_cancel()
        try:
            source = pyvips.Image.new_from_buffer(data, "", access="random")
        except pyvips.Error as exc:
            raise ValueError("Unsupported or invalid image data: " + str(exc)) from None
        loader = source.get("vips-loader")
        formats = {"jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "tiff": "tiff",
                   "magick": pathlib.Path(filename).suffix.lower().lstrip("."), "svg": "svg", "jp2k": "jp2", "jxl": "jxl"}
        fmt = next((value for prefix, value in formats.items() if loader.startswith(prefix)), None)
        if loader.startswith("heif"):
            fmt = "avif" if source.get("heif-compression") == "av1" else "heic"
        detail = {"original_bytes": len(data), "original_format": fmt or loader,
                  "width": source.width, "height": source.height}
        if fmt in self.formats and len(data) <= self.limit:
            return self.result(data, fmt, filename, detail, "original")
        if source.width * source.height > 100_000_000:
            raise ValueError("Image exceeds the 100 megapixel processing limit.")
        if source.get_typeof("n-pages") and source.get("n-pages") > 1:
            raise ValueError("Animated/multi-image input cannot be compressed without losing frames. Original link retained.")
        bits = int(source.get("bits-per-sample")) if source.get_typeof("bits-per-sample") else (16 if source.format == "ushort" else 8)
        if bits not in (8, 10, 12):
            raise ValueError("This input bit depth cannot be retained in AVIF or WebP. Original link retained.")
        if source.interpretation in {"cmyk", "lab", "labs", "scRGB"}:
            raise ValueError("This color space cannot be safely retained by the bundled encoder.")
        chroma = container_chroma(data) if fmt in {"avif", "heic"} else None
        if source.get_typeof("jpeg-chroma-subsample"):
            chroma = source.get("jpeg-chroma-subsample").replace(":", "")
        detail.update(bit_depth=bits, source_chroma=chroma or "RGB", orientation="preserved")
        candidates = []
        lossless_source = source.copy()
        # RGB identity avoids a lossy RGB/YUV transform in the lossless candidate.
        for name, value in [("cicp-matrix-coefficients", 0), ("cicp-full-range-flag", 1)]:
            lossless_source.set_type(pyvips.GValue.gint_type, name, value)
        self.check_cancel()
        try:
            encoded = lossless_source.heifsave_buffer(compression="av1", lossless=True, bitdepth=bits, effort=4, subsample_mode="off")
            if len(encoded) <= self.limit:
                candidates.append((encoded, "avif", "lossless_avif"))
        except pyvips.Error:
            pass
        if bits == 8:
            self.check_cancel()
            try:
                encoded = source.webpsave_buffer(lossless=True, effort=4)
                if len(encoded) <= self.limit:
                    candidates.append((encoded, "webp", "lossless_webp"))
            except pyvips.Error:
                pass
        if candidates:
            encoded, fmt, method = min(candidates, key=lambda item: len(item[0]))
            return self.result(encoded, fmt, filename, detail, method)
        # libheif supports 420/444 here; 422 is represented as 444 without further subsampling.
        sampling = "on" if chroma == "420" else "off" if chroma in {"422", "444"} else "auto"
        scale = 1.0
        for _ in range(32):
            self.check_cancel()
            current = source if scale == 1 else source.resize(scale, kernel="lanczos3")
            for quality in (80, 70):
                self.check_cancel()
                try:
                    encoded = current.heifsave_buffer(compression="av1", Q=quality, bitdepth=bits,
                                                      effort=4, subsample_mode=sampling)
                except pyvips.Error as exc:
                    raise ValueError("AVIF encoding failed: " + str(exc)) from None
                if len(encoded) <= self.limit:
                    detail.update(width=current.width, height=current.height, quality=quality,
                                  output_chroma="420" if sampling == "on" else "444" if sampling == "off" else "encoder default")
                    return self.result(encoded, "avif", filename, detail, "lossy_avif")
            if min(current.width, current.height) <= 16:
                break
            scale *= .85
        raise ValueError("Image could not meet the size limit. Original link retained.")

    @staticmethod
    def result(data, fmt, filename, detail, method):
        extension = "jpg" if fmt == "jpeg" else fmt
        mime = {"svg": "image/svg+xml", "ico": "image/x-icon", "heic": "image/heic"}.get(fmt, "image/" + fmt)
        detail = {**detail, "bytes": len(data), "format": fmt, "method": method}
        return PreparedImage(data, mime, pathlib.Path(filename).stem + "." + extension, detail)
