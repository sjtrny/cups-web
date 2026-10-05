"""Check colour pixels and the preview API without a printer or network."""
import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fitz
from PIL import Image

import app as server
import documents


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "colour.pdf"
        with fitz.open() as doc:
            for colors in [((.1, .2, .7), (.9, .9, .2)), ((.9, .1, .2), (.2, .9, .9))]:
                page = doc.new_page(width=144, height=144)
                for rect, color in zip([(12, 12, 68, 132), (76, 12, 132, 132)], colors):
                    page.draw_rect(fitz.Rect(rect), color=None, fill=color)
            doc.save(self.source)
        self.caps = {"name": "Test", "options": [], "attributes": {"color-supported": True}}
        uploads, jobs = self.root / "uploads", self.root / "jobs"
        uploads.mkdir()
        jobs.mkdir()
        for key, value in [("STATE", self.root), ("UPLOADS", uploads), ("JOBS", jobs)]:
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server.printers, "validated_options", side_effect=lambda name, options: (self.caps, options))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(server.printers, "paper_size", return_value={"width": 144, "height": 144})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = server.app.test_client()
        self.headers = {"X-Print-UI": "1"}
        upload = self.client.post("/api/uploads", data={"file": (io.BytesIO(self.source.read_bytes()), "colour-preview-test.pdf")}, headers=self.headers)
        self.assertEqual(upload.status_code, 201)
        self.payload = {"upload": upload.get_json()["id"], "printer": "Test", "layout": {"scale": "none"}}

    def assert_pixels(self, encoded, expected):
        image = Image.open(io.BytesIO(encoded)).convert("RGB")
        self.assertEqual(image.size, (216, 216))
        pixels = set(image.getdata())
        if expected == "color":
            self.assertTrue(any(r != g or g != b for r, g, b in pixels), "Colour preview must preserve coloured artwork")
        else:
            self.assertTrue(all(r == g == b for r, g, b in pixels), "Preview must contain no colour")
            values = {r for r, _, _ in pixels}
            if expected == "bi-level":
                self.assertEqual(values, {0, 255}, "Bi-level preview must contain only black and white")
            else:
                self.assertTrue(any(0 < v < 255 for v in values), "Grayscale must preserve shaded artwork")

    def check_api(self, options, expected, page=1):
        response = self.client.post("/api/preview", json={**self.payload, "options": options, "page": page}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()
        self.assertEqual(result["pages"], 2)
        self.assert_pixels(base64.b64decode(result["image"].split(",", 1)[1]), expected)
        return result

    def test_rendering_keeps_page_size_pages_and_original_vectors(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for mode in ["color", "grayscale", "bi-level"]:
            for page in [1, 2]:
                with self.subTest(mode=mode, page=page):
                    image, count = documents.preview(self.source, page, mode)
                    self.assertEqual(count, 2)
                    self.assert_pixels(image, mode)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        with fitz.open(self.source) as doc:
            self.assertTrue(all(page.get_drawings() for page in doc))

    def test_ipp_selections_and_default(self):
        self.caps["attributes"]["print-color-mode-default"] = "monochrome"
        self.check_api({}, "grayscale")
        for value in ["color", "auto", "monochrome", "process-monochrome", "auto-monochrome", "bi-level", "process-bi-level"]:
            with self.subTest(value=value):
                expected = "color" if value in {"color", "auto"} else "bi-level" if "bi-level" in value else "grayscale"
                self.check_api({"print-color-mode": value}, expected)
        self.check_api({"print-color-mode": "color"}, "color", page=2)

    def test_driver_selections_override_defaults(self):
        self.caps["attributes"]["print-color-mode-default"] = "monochrome"
        for key, gray, color in [("ColorModel", "Gray", "RGB"), ("ColorMode", "Grayscale", "Color"),
                                 ("OutputMode", "BlackAndWhite", "Color"), ("BRMonoColor", "Mono", "Color")]:
            with self.subTest(key=key):
                self.caps["options"] = [{"key": key, "default": color}]
                self.check_api({}, "color")
                self.check_api({key: gray}, "grayscale")
                self.check_api({key: color}, "color")
                self.caps["options"][0]["default"] = gray
                self.check_api({}, "grayscale")
                self.check_api({key: color}, "color")

    def test_automatic_and_missing_mode_on_monochrome_printer(self):
        self.caps["attributes"] = {"color-supported": False}
        self.check_api({}, "grayscale")
        self.check_api({"print-color-mode": "auto"}, "grayscale")
        self.caps["options"] = [{"key": "ColorModel", "default": "Gray"}]
        self.check_api({"print-color-mode": "bi-level"}, "bi-level")

    def test_preview_conversion_preserves_print_pdf_and_native_options(self):
        options = {"ColorModel": "Gray", "print-quality": "5"}
        result = self.check_api(options, "grayscale")
        pdf = self.client.get(result["document"])
        self.addCleanup(pdf.close)
        self.assertEqual(pdf.status_code, 200)
        with fitz.open(stream=pdf.data, filetype="pdf") as doc:
            self.assertTrue(doc[0].get_drawings(), "Prepared PDF must retain vector artwork")
            self.assertTrue(any(len(set(d["fill"])) > 1 for d in doc[0].get_drawings()))
        conn = Mock()
        conn.printFile.return_value = 1
        with patch.object(server.printers, "connection", return_value=conn):
            response = self.client.post("/api/print", json={**self.payload, "options": options, "submission": "a" * 32}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        args = conn.printFile.call_args.args
        self.assertEqual(Path(args[1]).read_bytes(), pdf.data)
        self.assertEqual((args[3]["ColorModel"], args[3]["print-quality"]), ("Gray", "5"))

    def test_repeated_preview_and_quality_changes_reuse_rendering(self):
        with patch.object(documents, "prepare", wraps=documents.prepare) as prepare, \
             patch.object(documents, "preview", wraps=documents.preview) as render:
            first = self.check_api({"ColorModel": "Gray", "print-quality": "3"}, "grayscale")
            repeated = self.check_api({"ColorModel": "Gray", "print-quality": "3"}, "grayscale")
            changed = self.check_api({"ColorModel": "Gray", "print-quality": "5"}, "grayscale")
        self.assertEqual(first, repeated)
        self.assertEqual(first["image"], changed["image"])
        self.assertNotEqual(first["document"], changed["document"], "Job options must retain their own submission identity")
        self.assertEqual((prepare.call_count, render.call_count), (1, 1))

    def test_cache_updates_for_colour_layout_pages_and_paper(self):
        with patch.object(documents, "prepare", wraps=documents.prepare) as prepare, \
             patch.object(documents, "preview", wraps=documents.preview) as render:
            self.check_api({"ColorModel": "RGB"}, "color")
            self.check_api({"ColorModel": "Gray"}, "grayscale")
            self.assertEqual((prepare.call_count, render.call_count), (1, 2))
            self.payload["layout"]["offset_x"] = 2
            self.check_api({"ColorModel": "Gray"}, "grayscale")
            self.check_api({"ColorModel": "Gray"}, "grayscale", page=2)
            self.assertEqual((prepare.call_count, render.call_count), (2, 4))
            with patch.object(server.printers, "paper_size", return_value={"width": 144, "height": 288}):
                response = self.client.post("/api/preview", json={**self.payload, "options": {"ColorModel": "Gray"}}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                image = Image.open(io.BytesIO(base64.b64decode(response.get_json()["image"].split(",", 1)[1])))
                self.assertEqual(image.size, (216, 432))
            self.assertEqual((prepare.call_count, render.call_count), (3, 5))
            response = self.client.post("/api/preview", json={**self.payload, "options": {"ColorModel": "Gray", "number-up": "2"}}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["pages"], 1)
            self.assertEqual((prepare.call_count, render.call_count), (4, 6))

    def test_reused_pdf_keeps_job_options_and_submission_idempotency(self):
        conn = Mock()
        conn.printFile.return_value = 1
        original = {**self.payload, "options": {"ColorModel": "Gray", "print-quality": "3"}, "submission": "a" * 32}
        changed = {**original, "options": {**original["options"], "print-quality": "5"}}
        with patch.object(server.printers, "connection", return_value=conn):
            self.assertEqual(self.client.post("/api/print", json=original, headers=self.headers).status_code, 201)
            self.assertEqual(self.client.post("/api/print", json=original, headers=self.headers).status_code, 200)
            self.assertEqual(self.client.post("/api/print", json=changed, headers=self.headers).status_code, 400)
            self.assertEqual(conn.printFile.call_count, 1)
            changed["submission"] = "b" * 32
            self.assertEqual(self.client.post("/api/print", json=changed, headers=self.headers).status_code, 201)
        first, second = [call.args for call in conn.printFile.call_args_list]
        self.assertEqual(Path(first[1]).read_bytes(), Path(second[1]).read_bytes())
        self.assertEqual((first[3]["print-quality"], second[3]["print-quality"]), ("3", "5"))


if __name__ == "__main__":
    unittest.main()
