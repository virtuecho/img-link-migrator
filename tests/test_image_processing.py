"""Small, codec-backed checks of the user-visible processing policies."""
import os
import io
import pathlib
import tempfile
import unittest

import pyvips
import img_link_migrator as core
from image_processing import ImageProcessor, nclx_positions


class ImagePolicyTests(unittest.TestCase):
    def test_modes_real_format_and_lossless_first(self):
        png = pyvips.Image.black(64, 48, bands=3).pngsave_buffer()
        padded = png + bytes(1_000_000)
        size = ImageProcessor()
        result = size.prepare(padded, "wrong-extension.jpg")
        self.assertLess(len(result.data), 1_000_000)
        self.assertIn(result.detail["method"], ("lossless_avif", "lossless_webp"))
        self.assertEqual((result.detail["width"], result.detail["height"]), (64, 48))
        self.assertEqual(ImageProcessor(mode="original").prepare(padded, "x.jpg").data, padded)
        self.assertEqual(size.prepare(png, "wrong.jpg").filename, "wrong.png")
        self.assertNotEqual(size.namespace, ImageProcessor(mode="original").namespace)
        self.assertEqual(ImageProcessor(provider="chevereto", mode="original").limit, 25_000_000)
        self.assertEqual(ImageProcessor(mode="original").limit, 32_000_000)

    def test_quality_cycle_shrinks_from_original(self):
        image = pyvips.Image.new_from_memory(os.urandom(256 * 192 * 3), 256, 192, 3, "uchar")
        processor = ImageProcessor()
        processor.limit = 12_000  # A smaller budget exercises the same shrink branch quickly.
        result = processor.prepare(image.pngsave_buffer(), "noise.png")
        self.assertLessEqual(len(result.data), processor.limit)
        self.assertEqual(result.detail["method"], "lossy_avif")
        self.assertIn(result.detail["quality"], (80, 70))
        self.assertLess(result.detail["width"], 256)
        decoded = pyvips.Image.new_from_buffer(result.data, "")
        self.assertEqual((decoded.width, decoded.height), (result.detail["width"], result.detail["height"]))

    def test_high_bit_depth_and_failed_input(self):
        image = (pyvips.Image.black(16, 16, bands=3) + 10000).cast("ushort").copy(interpretation="rgb16")
        avif = image.heifsave_buffer(compression="av1", bitdepth=10, lossless=True, subsample_mode="off")
        hdr = bytearray(avif)
        pos = next(nclx_positions(hdr))
        hdr[pos:pos + 4] = bytes.fromhex("00090010")  # BT.2020 primaries, PQ transfer.
        processor = ImageProcessor()
        processor.formats = set()  # Force recoding a valid high-bit-depth source.
        result = processor.prepare(bytes(hdr), "ten.avif")
        decoded = pyvips.Image.new_from_buffer(result.data, "")
        self.assertEqual(decoded.get("bits-per-sample"), 10)
        pos = next(nclx_positions(result.data))
        self.assertEqual(result.data[pos:pos + 4], bytes.fromhex("00090010"))
        with self.assertRaises(ValueError):
            processor.prepare(b"not an image", "bad.jpg")
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), "red").save(buffer, format="BMP")
        self.assertEqual(ImageProcessor().prepare(buffer.getvalue(), "wrong.jpg").filename, "wrong.bmp")
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), "red").save(buffer, format="PNG", save_all=True,
              append_images=[Image.new("RGB", (32, 24), "blue")], duration=100)
        with self.assertRaises(ValueError):
            ImageProcessor().prepare(buffer.getvalue() + bytes(1_000_000), "animated.png")

    def test_retry_keeps_our_writes_but_rejects_external_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "article.md"
            path.write_text("![a](https://source.example/a.png)\n![b](https://source.example/b.png)")
            plans = core.scan_targets([path])
            class Client:
                cache_namespace = "smoke"
                def download(self, url):
                    return url.encode(), "image/png", "x.png"
                def upload(self, data, content_type, filename, url):
                    return url.replace("source.example", "destination.example")
            store = core.StateStore(pathlib.Path(folder) / "state")
            engine = core.MigrationEngine(store, Client(), backup_enabled=True)
            first = engine.run([path], True, {"https://source.example/a.png"}, plans)
            second = engine.run([path], True, {"https://source.example/b.png"}, plans)
            self.assertFalse(first.failed_urls)
            self.assertFalse(second.failed_urls)
            self.assertNotIn("source.example", path.read_text())
            path.write_text(path.read_text() + "\nExternal edit")
            blocked = engine.run([path], True, {"https://source.example/a.png"}, plans)
            self.assertTrue(blocked.failed_urls)
            self.assertIn("External edit", path.read_text())


if __name__ == "__main__":
    unittest.main()
