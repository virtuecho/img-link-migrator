"""AVIF/WebP processing with HDR support and an original-byte fallback."""
import dataclasses
import pathlib
import io
import threading
import platform
import subprocess
import sys
import tempfile

POLICY_VERSION = "v2-hdr-original-fallback"
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


def boxes(data, start=0, end=None):
    """Read container boxes for metadata, without decoding image pixels."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        size = int.from_bytes(data[start:start + 4], "big")
        header = 8
        if size == 1:
            size = int.from_bytes(data[start + 8:start + 16], "big")
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("Unreadable image container metadata.")
        yield data[start + 4:start + 8], start + header, start + size
        start += size


def primary_nclx(data):
    """Read NCLX associated with the primary item, excluding auxiliary images."""
    meta = next((box for box in boxes(data) if box[0] == b"meta"), None)
    if not meta:
        return None
    children = list(boxes(data, meta[1] + 4, meta[2]))
    pitm = next(box for box in children if box[0] == b"pitm")
    primary = int.from_bytes(data[pitm[1] + 4:pitm[2]], "big")
    iprp = next(box for box in children if box[0] == b"iprp")
    properties = list(boxes(data, iprp[1], iprp[2]))
    ipco = next(box for box in properties if box[0] == b"ipco")
    entries = list(boxes(data, ipco[1], ipco[2]))
    for _, start, end in (box for box in properties if box[0] == b"ipma"):
        version = data[start]
        wide = int.from_bytes(data[start + 1:start + 4], "big") & 1
        count = int.from_bytes(data[start + 4:start + 8], "big")
        pos = start + 8
        for _ in range(count):
            item_bytes = 4 if version else 2
            item = int.from_bytes(data[pos:pos + item_bytes], "big")
            pos += item_bytes
            associations = data[pos]
            pos += 1
            for _ in range(associations):
                width = 2 if wide else 1
                index = int.from_bytes(data[pos:pos + width], "big") & (0x7fff if wide else 0x7f)
                pos += width
                if pos > end:
                    raise ValueError("Unreadable image property association.")
                if item == primary and index:
                    kind, begin, finish = entries[index - 1]
                    if kind == b"colr" and data[begin:begin + 4] == b"nclx" and finish - begin >= 11:
                        return data[begin + 4:begin + 11]
    return None


def codec_path(name):
    root = pathlib.Path(sys._MEIPASS) if getattr(sys, "frozen", False) else pathlib.Path(__file__).parent / "build" / ("codecs-" + platform.machine())
    return root / name


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

    def run_codec(self, args):
        self.check_cancel()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([str(arg) for arg in args], stdout=subprocess.DEVNULL, stderr=errors)
            try:
                while True:
                    self.check_cancel()
                    try:
                        code = process.wait(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.wait()
                raise
            if code:
                errors.seek(0)
                raise ValueError(errors.read().decode("utf-8", "replace")[-2000:].strip() or "Image codec failed.")

    def expand_hdr(self, data, source, jpeg):
        """Use native color management to render recovered HDR into PQ pixels."""
        import pyvips
        with tempfile.TemporaryDirectory(prefix="img-link-hdr-") as folder:
            input_path = pathlib.Path(folder) / "source"
            output_path = pathlib.Path(folder) / "pq.png"
            args = [codec_path("hdr-decode"), input_path, output_path]
            if jpeg:
                linear = source.uhdr2scRGB()
                if linear.bands == 3:
                    linear = linear.bandjoin(1.0)
                input_path.write_bytes(linear.cast("float").write_to_memory())
                args += [linear.width, linear.height]
            else:
                input_path.write_bytes(data)
            self.run_codec(args)
            return pyvips.Image.new_from_buffer(output_path.read_bytes(), "", access="random")

    def encode_avif(self, source, bits, profile, sampling, lossless=False, quality=80):
        with tempfile.TemporaryDirectory(prefix="img-link-avif-") as folder:
            input_path = pathlib.Path(folder) / "source.png"
            output_path = pathlib.Path(folder) / "output.avif"
            source.pngsave(str(input_path), bitdepth=16 if bits > 8 else 8)
            args = [codec_path("avifenc"), "--speed", 6, "--jobs", 4, "--depth", bits,
                    "--yuv", "444" if lossless else sampling, "--range", "full", "--qalpha", 100]
            args += ["--lossless"] if lossless else ["--qcolor", quality]
            if profile:
                primaries = int.from_bytes(profile[:2], "big")
                transfer = int.from_bytes(profile[2:4], "big")
                matrix = 0 if lossless else 9 if primaries == 9 else 1 if primaries == 1 else 6
                args += ["--cicp", f"{primaries}/{transfer}/{matrix}"]
            args += [input_path, output_path]
            self.run_codec(args)
            return output_path.read_bytes()

    def prepare(self, data, filename, content_type="application/octet-stream"):
        from img_link_migrator import CancelledError
        self.check_cancel()
        try:
            return self._prepare(data, filename)
        except CancelledError:
            raise
        except Exception as exc:
            self.check_cancel()
            return PreparedImage(data, content_type, filename, {
                "original_bytes": len(data), "bytes": len(data),
                "format": content_type.removeprefix("image/"),
                "method": "original_fallback",
                "warning": "Processing unavailable; uploading original bytes. " + str(exc),
            })

    def _prepare(self, data, filename):
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
        formats = {"jpeg": "jpeg", "uhdr": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "tiff": "tiff",
                   "svg": "svg", "jp2k": "jp2", "jxl": "jxl"}
        fmt = next((value for prefix, value in formats.items() if loader.startswith(prefix)), None)
        if loader.startswith("heif"):
            fmt = "avif" if source.get("heif-compression") == "av1" else "heic"
        elif loader.startswith("fallback-"):
            fmt = loader.removeprefix("fallback-")
        detail = {"original_bytes": len(data), "original_format": fmt or loader,
                  "width": source.width, "height": source.height}
        profile = primary_nclx(data) if fmt in {"heic", "avif"} else None
        decoded = None
        gainmap = bool(source.get_typeof("gainmap-data") or source.get_typeof("gainmap"))
        if fmt == "heic":
            import pi_heif
            decoded = pi_heif.open_heif(data, convert_hdr_to_8bit=False, hdr_to_16bit=True)
            metadata = decoded[decoded.primary_index].info
            gainmap = gainmap or any("hdrgainmap" in kind or "hdrgm" in kind or "21496" in kind for kind in metadata.get("aux", {}))
        elif fmt == "avif":
            ftyp = next((box for box in boxes(data) if box[0] == b"ftyp"), None)
            if ftyp:
                _, start, end = ftyp
                brands = [data[start:start + 4]] + [data[pos:pos + 4] for pos in range(start + 8, end, 4)]
                gainmap = gainmap or b"tmap" in brands
        hdr = gainmap or bool(profile and int.from_bytes(profile[2:4], "big") in {16, 18})
        bits = int(source.get("bits-per-sample")) if source.get_typeof("bits-per-sample") else (16 if source.format == "ushort" else 8)
        chroma = container_chroma(data) if fmt in {"avif", "heic"} else None
        if source.get_typeof("jpeg-chroma-subsample"):
            chroma = source.get("jpeg-chroma-subsample").replace(":", "")
        detail.update(hdr=hdr, bit_depth=bits, source_chroma=chroma or "RGB")
        if profile:
            detail.update(color_primaries=int.from_bytes(profile[:2], "big"),
                          transfer_characteristics=int.from_bytes(profile[2:4], "big"),
                          color_matrix=int.from_bytes(profile[4:6], "big"),
                          color_range="full" if profile[6] & 128 else "limited")
        if fmt in self.formats and len(data) <= self.limit:
            return self.result(data, fmt, filename, detail, "original")
        if source.width * source.height > 100_000_000:
            raise ValueError("Image exceeds the 100 megapixel processing limit.")
        animated_png = fmt == "png" and b"acTL" in data and b"acTL" in data[:data.find(b"IDAT")]
        if animated_png or (source.get_typeof("n-pages") and source.get("n-pages") > 1) or (fmt == "avif" and b"avis" in data[:64]):
            raise ValueError("Animated/multi-image compression would lose frames.")
        if gainmap:
            if fmt == "avif":
                raise ValueError("AVIF gain-map conversion is unavailable; keep the original HDR data.")
            source = self.expand_hdr(data, source, fmt == "jpeg")
            bits = 12
            profile = bytes.fromhex("00090010000980")
            detail.update(width=source.width, height=source.height, hdr_representation="PQ")
        elif fmt == "heic":
            # The small libvips wheel reads HEIC headers but has no HEVC decoder.
            # pi-heif adds decoding without an unnecessary HEVC encoder.
            if len(decoded) > 1:
                raise ValueError("Multi-image HEIC cannot be compressed without losing images.")
            image = decoded[decoded.primary_index]
            metadata = image.info.copy()
            pi_heif.set_orientation(metadata)
            bands = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4}.get(image.mode.split(";")[0])
            if not bands:
                raise ValueError("Unsupported HEIC decoded color layout.")
            bits = image.info["bit_depth"]
            source = pyvips.Image.new_from_memory(image.data, *image.size, bands, "ushort" if bits > 8 else "uchar")
            source = source.copy(interpretation=("grey16" if bits > 8 else "b-w") if bands <= 2 else ("rgb16" if bits > 8 else "srgb"))
            for original, field in (("icc_profile", "icc-profile-data"), ("exif", "exif-data")):
                if metadata.get(original):
                    source.set_type(pyvips.GValue.blob_type, field, metadata[original])
        if bits not in (8, 10, 12):
            raise ValueError("This input bit depth cannot be retained in AVIF or WebP.")
        if source.interpretation in {"cmyk", "lab", "labs", "scRGB"}:
            raise ValueError("This color space cannot be safely retained by the bundled encoder.")
        detail.update(bit_depth=bits, orientation="preserved", color_range="full")
        if profile:
            detail.update(color_primaries=int.from_bytes(profile[:2], "big"),
                          transfer_characteristics=int.from_bytes(profile[2:4], "big"))
        candidates = []
        self.check_cancel()
        try:
            encoded = self.encode_avif(source, bits, profile, "444", lossless=True)
            if len(encoded) <= self.limit:
                candidates.append((encoded, "avif", "lossless_avif"))
        except (pyvips.Error, ValueError, OSError):
            pass
        # WebP has no NCLX container; explicit NCLX sources stay in AVIF.
        ordinary_color = profile is None
        if bits == 8 and ordinary_color and not hdr:
            self.check_cancel()
            try:
                encoded = source.webpsave_buffer(lossless=True, effort=4)
                if len(encoded) <= self.limit:
                    candidates.append((encoded, "webp", "lossless_webp"))
            except pyvips.Error:
                pass
        if candidates:
            encoded, fmt, method = min(candidates, key=lambda item: len(item[0]))
            if fmt == "avif":
                detail.update(output_chroma="444", color_matrix=0)
            return self.result(encoded, fmt, filename, detail, method)
        sampling = "420" if chroma == "420" else "444"
        scale = 1.0
        for _ in range(32):
            self.check_cancel()
            current = source if scale == 1 else source.resize(scale, kernel="lanczos3")
            for quality in (80, 70):
                self.check_cancel()
                try:
                    encoded = self.encode_avif(current, bits, profile, sampling, quality=quality)
                except pyvips.Error as exc:
                    raise ValueError("AVIF encoding failed: " + str(exc)) from None
                if len(encoded) <= self.limit:
                    detail.update(width=current.width, height=current.height, quality=quality,
                                  output_chroma=sampling,
                                  color_matrix=9 if profile and profile[:2] == b"\x00\x09" else 1 if profile and profile[:2] == b"\x00\x01" else 6)
                    return self.result(encoded, "avif", filename, detail, "lossy_avif")
            if min(current.width, current.height) <= 16:
                break
            scale *= .85
        raise ValueError("Image could not meet the size limit.")

    @staticmethod
    def result(data, fmt, filename, detail, method):
        extension = "jpg" if fmt == "jpeg" else fmt
        mime = {"svg": "image/svg+xml", "ico": "image/x-icon", "heic": "image/heic"}.get(fmt, "image/" + fmt)
        detail = {**detail, "bytes": len(data), "format": fmt, "method": method}
        if method == "original" and "source_chroma" in detail:
            detail["output_chroma"] = detail["source_chroma"]
        return PreparedImage(data, mime, pathlib.Path(filename).stem + "." + extension, detail)
