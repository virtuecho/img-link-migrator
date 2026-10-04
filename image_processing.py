"""Two size policies backed by bundled libvips, libheif, WebP, and AV1 codecs."""
import dataclasses
import pathlib
import io
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


def nclx_positions(data):
    """Locate bounded ISO BMFF nclx color-property boxes."""
    start = 0
    while (pos := data.find(b"colrnclx", start)) >= 0:
        size = int.from_bytes(data[pos - 4:pos], "big") if pos >= 4 else 0
        if size >= 19 and pos - 4 + size <= len(data):
            yield pos + 8
        start = pos + 8


def preserve_nclx(data, profile):
    if profile is None:
        return data
    positions = list(nclx_positions(data))
    if not positions:
        raise ValueError("The encoder cannot retain this NCLX color profile.")
    output = bytearray(data)
    for pos in positions:
        # Retain primaries/transfer. Matrix/range describe the newly encoded YUV
        # or RGB values and must remain those selected by the encoder.
        output[pos:pos + 4] = profile[:4]
    return bytes(output)


class ImageProcessor:
    def __init__(self, provider="imgbb", mode="size_limit", cancel=None):
        if mode not in {"size_limit", "original"}:
            raise ValueError("Unknown image processing mode.")
        self.mode = mode
        self.platform_limit = PLATFORM_LIMITS[provider]
        self.limit = 999_999 if mode == "size_limit" else self.platform_limit
        self.formats = IMGBB_FORMATS if provider == "imgbb" else COMMON_FORMATS
        self.cancel = cancel or threading.Event()
        self.namespace = f"{POLICY_VERSION}:{mode}:{self.platform_limit}"

    def check_cancel(self):
        if self.cancel.is_set():
            from img_link_migrator import CancelledError
            raise CancelledError("Task cancelled.")

    def prepare(self, data, filename):
        import pyvips
        pyvips.cache_set_max(0)
        self.check_cancel()
        try:
            source = pyvips.Image.new_from_buffer(data, "", access="random")
        except pyvips.Error as exc:
            # Pillow supplies BMP/ICO/PSD loaders absent from the small libvips build.
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 100_000_000
            try:
                image = Image.open(io.BytesIO(data))
                fmt = {"jpg": "jpeg", "jpeg2000": "jp2"}.get(image.format.lower(), image.format.lower())
                detail = {"original_bytes": len(data), "original_format": fmt, "width": image.width, "height": image.height}
                if fmt in self.formats and len(data) <= self.limit:
                    image.verify()
                    return self.result(data, fmt, filename, detail, "original")
                if image.width * image.height > 100_000_000 or getattr(image, "n_frames", 1) > 1:
                    raise ValueError("Multi-frame or oversized decoded image cannot be processed safely.")
                if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                    raise ValueError("This fallback image bit depth or color space cannot be retained.")
                pixels = image.convert("RGBA" if "A" in image.mode or "transparency" in image.info else "RGB")
                source = pyvips.Image.new_from_memory(pixels.tobytes(), pixels.width, pixels.height, len(pixels.getbands()), "uchar").copy(interpretation="srgb")
                for original, field in (("icc_profile", "icc-profile-data"), ("exif", "exif-data")):
                    if image.info.get(original):
                        source.set_type(pyvips.GValue.blob_type, field, image.info[original])
                source.set_type(pyvips.GValue.gstr_type, "vips-loader", "fallback-" + fmt)
            except Exception as fallback_error:
                raise ValueError("Unsupported or invalid image data: " + str(fallback_error)) from None
        loader = source.get("vips-loader")
        formats = {"jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "tiff": "tiff",
                   "svg": "svg", "jp2k": "jp2", "jxl": "jxl"}
        fmt = next((value for prefix, value in formats.items() if loader.startswith(prefix)), None)
        if loader.startswith("heif"):
            fmt = "avif" if source.get("heif-compression") == "av1" else "heic"
        elif loader.startswith("fallback-"):
            fmt = loader.removeprefix("fallback-")
        detail = {"original_bytes": len(data), "original_format": fmt or loader,
                  "width": source.width, "height": source.height}
        if fmt in self.formats and len(data) <= self.limit:
            return self.result(data, fmt, filename, detail, "original")
        if fmt in {"heic", "avif"} and (b"hdrgainmap" in data or b"hdrgm" in data or b"urn:iso:std:iso:ts:21496" in data):
            raise ValueError("HDR gain-map auxiliary data cannot be retained by this encoder. Original link retained.")
        profiles = {data[pos:pos + 7] for pos in nclx_positions(data)} if fmt in {"heic", "avif"} else set()
        if len(profiles) > 1:
            raise ValueError("Multiple distinct NCLX profiles cannot be retained safely.")
        profile = next(iter(profiles), None)
        if source.width * source.height > 100_000_000:
            raise ValueError("Image exceeds the 100 megapixel processing limit.")
        animated_png = fmt == "png" and b"acTL" in data and b"acTL" in data[:data.find(b"IDAT")]
        if animated_png or (source.get_typeof("n-pages") and source.get("n-pages") > 1) or (fmt == "avif" and b"avis" in data[:64]):
            raise ValueError("Animated/multi-image input cannot be compressed without losing frames. Original link retained.")
        bits = int(source.get("bits-per-sample")) if source.get_typeof("bits-per-sample") else (16 if source.format == "ushort" else 8)
        if fmt == "heic":
            # The small libvips wheel reads HEIC headers but has no HEVC decoder.
            # pi-heif adds decoding without an unnecessary HEVC encoder.
            import pi_heif
            decoded = pi_heif.open_heif(data, convert_hdr_to_8bit=False, hdr_to_16bit=True)
            if len(decoded) > 1:
                raise ValueError("Multi-image HEIC cannot be compressed without losing images.")
            image = decoded[decoded.primary_index]
            bands = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4}.get(image.mode.split(";")[0])
            if not bands:
                raise ValueError("Unsupported HEIC decoded color layout.")
            bits = image.info["bit_depth"]
            source = pyvips.Image.new_from_memory(image.data, *image.size, bands, "ushort" if bits > 8 else "uchar")
            source = source.copy(interpretation=("grey16" if bits > 8 else "b-w") if bands <= 2 else ("rgb16" if bits > 8 else "srgb"))
            for original, field in (("icc_profile", "icc-profile-data"), ("exif", "exif-data")):
                if image.info.get(original):
                    source.set_type(pyvips.GValue.blob_type, field, image.info[original])
        if bits not in (8, 10, 12):
            raise ValueError("This input bit depth cannot be retained in AVIF or WebP. Original link retained.")
        if source.interpretation in {"cmyk", "lab", "labs", "scRGB"}:
            raise ValueError("This color space cannot be safely retained by the bundled encoder.")
        chroma = container_chroma(data) if fmt in {"avif", "heic"} else None
        if source.get_typeof("jpeg-chroma-subsample"):
            chroma = source.get("jpeg-chroma-subsample").replace(":", "")
        detail.update(bit_depth=bits, source_chroma=chroma or "RGB", orientation="preserved")
        if profile:
            detail.update(color_primaries=int.from_bytes(profile[:2], "big"),
                          transfer_characteristics=int.from_bytes(profile[2:4], "big"))
        candidates = []
        self.check_cancel()
        try:
            encoded = source.heifsave_buffer(compression="av1", lossless=True, bitdepth=bits, effort=4, subsample_mode="off")
            encoded = preserve_nclx(encoded, profile)
            if len(encoded) <= self.limit:
                candidates.append((encoded, "avif", "lossless_avif"))
        except pyvips.Error:
            pass
        ordinary_color = profile is None or profile[:4] in {b"\x00\x01\x00\x0d", b"\x00\x02\x00\x02"}
        if bits == 8 and ordinary_color:
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
        sampling = "on" if chroma == "420" else "off" if chroma in {"411", "422", "440", "444"} else "auto"
        scale = 1.0
        for _ in range(32):
            self.check_cancel()
            current = source if scale == 1 else source.resize(scale, kernel="lanczos3")
            for quality in (80, 70):
                self.check_cancel()
                try:
                    encoded = current.heifsave_buffer(compression="av1", Q=quality, bitdepth=bits,
                                                      effort=4, subsample_mode=sampling)
                    encoded = preserve_nclx(encoded, profile)
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
        if fmt in {"heic", "avif"}:
            detail["output_chroma"] = container_chroma(data)
        return PreparedImage(data, mime, pathlib.Path(filename).stem + "." + extension, detail)
