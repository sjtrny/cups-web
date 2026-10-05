"""Check actual PDF geometry; no printer or network is used."""
import io
import tempfile
import subprocess
import struct
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageOps

import documents


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=288, height=432)
            page.draw_rect(fitz.Rect(72, 108, 216, 324), fill=(0, 0, 0), color=None)
            doc.save(self.source)

    def render(self, layout, options=None):
        result = self.root / "output.pdf"
        documents.prepare(self.source, result, {"width": 288, "height": 432}, options or {}, layout)
        with fitz.open(result) as doc:
            self.assertEqual(tuple(doc[0].rect), (0, 0, 288, 432))
            pix = doc[0].get_pixmap(colorspace=fitz.csGRAY, alpha=False)
            image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            bbox = image.point(lambda p: 255 if p < 128 else 0).getbbox()
            return bbox, doc.page_count

    def test_actual_size_and_half_scale_preserve_vectors(self):
        full, _ = self.render({"scale": "none"})
        half, _ = self.render({"scale": "custom", "percent": 50})
        self.assertEqual(full, (72, 108, 216, 324))
        self.assertEqual(half, (108, 162, 180, 270))
        with fitz.open(self.root / "output.pdf") as doc:
            self.assertTrue(doc[0].get_drawings(), "Scaling should preserve vector artwork")

    def test_translation_is_independent_of_scale_and_rotation(self):
        for layout in [{"scale": "none"}, {"scale": "fit"}, {"scale": "custom", "percent": 50},
                       {"scale": "custom", "percent": 50, "rotation": 90}]:
            with self.subTest(layout=layout):
                original, _ = self.render(layout)
                shifted, _ = self.render({**layout, "offset_x": 25.4 / 6, "offset_y": -6.35})
                # The physical shift is +12/-18 PDF points even at 50% and 90°.
                self.assertEqual(shifted, (original[0]+12, original[1]-18, original[2]+12, original[3]-18))

    def test_signed_translation_clips_to_paper(self):
        bbox, _ = self.render({"offset_x": -50.8, "offset_y": 50.8})
        self.assertEqual(bbox, (0, 252, 72, 432))

    def test_translation_outside_paper_produces_blank_page(self):
        for offsets in [{"offset_x": 1000}, {"offset_x": -1000}, {"offset_y": 1000}, {"offset_y": -1000}]:
            with self.subTest(offsets=offsets):
                bbox, count = self.render(offsets)
                self.assertIsNone(bbox)
                self.assertEqual(count, 1)
                image, pages = documents.preview(self.root / "output.pdf", 1)
                self.assertTrue(image.startswith(b"\x89PNG"))
                self.assertEqual(pages, 1)

    def test_translation_stays_inside_nup_cell(self):
        self.source = self.root / "two.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=288, height=432)
            page.draw_rect(page.rect, fill=(0, 0, 0), color=None)
            doc.new_page(width=288, height=432)
            doc.save(self.source)
        bbox, count = self.render({"scale": "fit", "offset_y": 25.4}, {"number-up": "2"})
        self.assertEqual(count, 1)
        self.assertEqual(bbox, (72, 72, 216, 216), "Shifted first-page content must not enter the second cell")

    def test_old_layout_defaults_and_invalid_offsets(self):
        old = documents.layout_settings({"scale": "custom", "percent": 50, "rotation": 90, "crop": True})
        self.assertEqual((old["offset_x"], old["offset_y"]), (0, 0))
        for key in ["offset_x", "offset_y"]:
            for value in [None, "abc", -1001, 1001, float("nan"), float("inf"), float("-inf")]:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "offset|Offset"):
                    documents.layout_settings({key: value})

    def test_crop_then_fit_fills_label_without_source_white_border(self):
        bbox, _ = self.render({"scale": "fit", "crop": True})
        # The intentional two-pixel crop safety border also scales up.
        self.assertLessEqual(bbox[0], 8)
        self.assertLessEqual(bbox[1], 8)
        self.assertGreaterEqual(bbox[2], 280)
        self.assertGreaterEqual(bbox[3], 424)

    def test_fill_is_clipped_inside_nup_cells(self):
        with fitz.open(self.source) as doc:
            for i in range(3):
                p = doc.new_page(width=288, height=432)
                p.draw_rect(fitz.Rect(0, 0, 288, 432), fill=(0, 0, 0), color=None)
            doc.save(self.root / "four.pdf")
        self.source = self.root / "four.pdf"
        _, count = self.render({"scale": "custom", "percent": 400}, {"number-up": "4"})
        self.assertEqual(count, 1)

    def test_rotation_keeps_physical_paper_size(self):
        bbox, _ = self.render({"scale": "none", "rotation": 90})
        self.assertEqual(bbox, (36, 144, 252, 288))

    def test_page_selection_and_invalid_ranges(self):
        self.assertEqual(documents.selected_pages(8, "1-3,5,8", "odd", True), [4, 2, 0])
        for value in ["0", "1-9", "3-1", "abc", "1-2-3"]:
            with self.assertRaises(ValueError):
                documents.selected_pages(8, value)

    def test_transparent_image_and_multipage_tiff(self):
        image = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
        path = self.root / "input"
        image.save(path, format="PNG")
        info = documents.normalize(path, "transparent.png")
        self.assertEqual(info["pages"], 1)
        with fitz.open(self.root / "source.pdf") as doc:
            pix = doc[0].get_pixmap(colorspace=fitz.csGRAY, alpha=False)
            self.assertTrue(all(v == 255 for v in pix.samples), "Transparency should print as white")
        image = image.convert("RGB")
        image.save(path, format="TIFF", save_all=True, append_images=[image])
        self.assertEqual(documents.normalize(path, "two.tiff")["pages"], 2)

    def test_jpeg_keeps_original_encoded_pixels(self):
        path = self.root / "input"
        image = Image.new("RGB", (80, 40), (20, 80, 160))
        image.save(path, format="JPEG", quality=93)
        original = path.read_bytes()
        info = documents.normalize(path, "photo.jpg")
        self.assertEqual((info["kind"], info["pages"]), ("image", 1))
        with fitz.open(self.root / "source.pdf") as doc:
            encoded = doc.extract_image(doc[0].get_images()[0][0])
            self.assertEqual(encoded["ext"], "jpeg")
            self.assertEqual(encoded["image"], original, "JPEG uploads must not be decoded and recompressed as PNG")

    def test_jpeg_exif_rotation_and_mirroring_remain_correct(self):
        path = self.root / "input"
        image = Image.new("RGB", (80, 40), (20, 80, 160))
        image.paste((200, 20, 40), (0, 0, 30, 20))
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                exif = Image.Exif()
                exif[274] = orientation
                image.save(path, format="JPEG", quality=95, exif=exif)
                with Image.open(path) as source:
                    expected = ImageOps.exif_transpose(source).convert("RGB")
                documents.normalize(path, "phone.jpg")
                with fitz.open(self.root / "source.pdf") as doc:
                    encoded = doc.extract_image(doc[0].get_images()[0][0])
                    with Image.open(io.BytesIO(encoded["image"])) as actual:
                        self.assertEqual(actual.size, expected.size)
                        self.assertIsNone(ImageChops.difference(actual.convert("RGB"), expected).getbbox())

    def test_common_raster_formats_are_detected_by_content(self):
        path = self.root / "input"
        for file_format in ["JPEG", "PNG", "GIF", "WEBP", "BMP", "JPEG2000"]:
            with self.subTest(file_format=file_format):
                image = Image.new("RGB", (32, 24), (20, 80, 160))
                image.save(path, format=file_format)
                info = documents.normalize(path, "no-extension")
                self.assertEqual((info["kind"], info["pages"]), ("image", 1))
                with fitz.open(self.root / "source.pdf") as converted:
                    self.assertEqual(converted.page_count, 1)
                    self.assertGreater(len(converted[0].get_images()), 0)

    def test_broken_exif_metadata_does_not_hide_valid_pixels(self):
        path = self.root / "input"
        content = self.root / "clean"
        Image.new("RGB", (32, 24), (20, 80, 160)).save(content, format="JPEG")
        jpeg = content.read_bytes()
        malformed = b"Exif\0\0http://not-a-tiff-metadata-block"
        invalid_app1 = b"\xff\xe1" + struct.pack(">H", len(malformed) + 2) + malformed
        exif = Image.Exif()
        exif[274] = 1
        valid = exif.tobytes()
        valid_app1 = b"\xff\xe1" + struct.pack(">H", len(valid) + 2) + valid
        # A valid one-frame MP index makes Pillow use its MPO decoder. This
        # reproduces the uploaded iPhone file's failure when Pillow seeks frame
        # zero and reloads the malformed EXIF block.
        second = jpeg[:2] + invalid_app1 + jpeg[2:]
        placeholder = b"\0" * 32
        mp_ifd = (b"MM\0\x2a\0\0\0\x08" + struct.pack(">H", 2)
                  + struct.pack(">HHLL", 0xB001, 4, 1, 2)
                  + struct.pack(">HHLL", 0xB002, 7, len(placeholder), 38)
                  + b"\0\0\0\0" + placeholder)
        app2_size = 4 + len(mp_ifd)
        first_size = len(jpeg) + len(valid_app1) + app2_size + 4
        tiff_offset = 2 + len(valid_app1) + 8
        mp_entries = (struct.pack(">LLLHH", 0x00030000, first_size, 0, 0, 0)
                      + struct.pack(">LLLHH", 0x00010001, len(second), first_size - tiff_offset, 0, 0))
        mp_ifd = mp_ifd[:-len(placeholder)] + mp_entries
        mpf = b"MPF\0" + mp_ifd
        app2 = b"\xff\xe2" + struct.pack(">H", len(mpf) + 2) + mpf
        first = jpeg[:2] + valid_app1 + app2 + jpeg[2:]
        self.assertEqual(len(first), first_size)
        path.write_bytes(first + second)
        with Image.open(path) as image:
            self.assertEqual(image.format, "MPO")
            self.assertEqual(image._MpoImageFile__mpoffsets[1], first_size)
            self.assertEqual(path.read_bytes()[first_size:first_size+4], second[:4])
            self.assertIsNotNone(image._exif)
            self.assertTrue(image._exif._loaded)
            with self.assertRaisesRegex(SyntaxError, "not a TIFF file"):
                image.seek(1)
        info = documents.normalize(path, "phone-photo.jpg")
        self.assertEqual((info["kind"], info["pages"]), ("image", 1))

    def test_heic_and_avif_are_detected_by_content(self):
        png = self.root / "original.png"
        Image.new("RGB", (64, 48), (20, 80, 160)).save(png)
        for file_format, arguments in [("HEIC", []), ("AVIF", ["-A"])]:
            with self.subTest(file_format=file_format):
                path = self.root / "input"
                result = subprocess.run(["heif-enc", *arguments, "-q", "90", "-o", str(path), str(png)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                self.assertEqual(result.returncode, 0, f"Test fixture encoder must support {file_format}")
                self.assertTrue(documents.is_heif(path.read_bytes()))
                info = documents.normalize(path, "no-extension")
                self.assertEqual((info["kind"], info["pages"]), ("image", 1))

    def test_invalid_image_returns_a_client_error(self):
        path = self.root / "input"
        path.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "supported raster image"):
            documents.normalize(path, "broken.bin")

    def test_invalid_scaling_and_encrypted_pdf(self):
        for percent in [0, -1, 1001, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                documents.layout_settings({"scale": "custom", "percent": percent})
        with fitz.open(self.source) as doc:
            encrypted = self.root / "locked"
            doc.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="password")
        with self.assertRaisesRegex(ValueError, "Unlock"):
            documents.normalize(encrypted, "locked.pdf")


if __name__ == "__main__":
    unittest.main()
