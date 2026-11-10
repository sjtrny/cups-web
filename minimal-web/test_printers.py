"""Exercise real PPD parsing and API policy without a CUPS server or printer."""
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cups
import fitz

import app as server
import printers


class PrinterTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "printers.json"
        self.config.write_text("{}")
        env = patch.dict(os.environ, {"PRINTER_CONFIG": str(self.config), "STATE_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.ppd = Path(__file__).with_name("test-generic.ppd").read_text()
        self.attrs = {
            "color-supported": True,
            "job-creation-attributes-supported": ["copies", "sides", "print-color-mode", "printer-resolution", "finishings", "page-ranges"],
            "copies-supported": (1, 99), "copies-default": 1,
            "print-color-mode-supported": ["color", "monochrome"], "print-color-mode-default": "color",
            "sides-supported": ["one-sided", "two-sided-long-edge"], "sides-default": "one-sided",
            "print-quality-supported": [3, 4, 5], "print-quality-default": 4,
            "printer-resolution-supported": [(300, 300, 3)], "printer-resolution-default": (300, 300, 3),
        }
        self.conn = Mock()
        self.conn.getPrinters.return_value = {"Office": {}, "Other": {}}
        self.conn.getPrinterAttributes.side_effect = lambda name: self.attrs.copy()
        self.conn.getPPD.side_effect = self.ppd_file
        self.conn.printFile.return_value = 42
        connection = patch.object(printers, "connection", return_value=self.conn)
        connection.start()
        self.addCleanup(connection.stop)
        for name, path in [("STATE", self.root), ("UPLOADS", self.root / "uploads"), ("JOBS", self.root / "jobs")]:
            path.mkdir(exist_ok=True)
            patched = patch.object(server, name, path)
            patched.start()
            self.addCleanup(patched.stop)
        self.client = server.app.test_client()
        self.headers = {"X-Print-UI": "1"}

    def ppd_file(self, name):
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".ppd", mode="w", delete=False) as file:
            file.write(self.ppd)
            return file.name

    def preferences(self, controls):
        self.config.write_text(json.dumps({"Office": {"controls": controls}}))

    def payload(self):
        with fitz.open() as document:
            for _ in range(4):
                document.new_page().insert_text((30, 30), "Generic printer policy check")
            contents = document.tobytes()
        uploaded = self.client.post("/api/uploads", data={"file": (io.BytesIO(contents), "sample.pdf")}, headers=self.headers)
        self.assertEqual(uploaded.status_code, 201)
        return {"printer": "Office", "upload": uploaded.get_json()["id"], "submission": "a" * 32}

    def test_custom_dimensions_follow_a4_and_saved_custom_defaults(self):
        caps = printers.capabilities("Office")
        self.assertAlmostEqual(caps["custom_default"]["width"], 595 * 25.4 / 72)
        self.assertAlmostEqual(caps["custom_default"]["height"], 842 * 25.4 / 72)
        self.ppd = self.ppd.replace("*DefaultPageSize: A4", "*DefaultPageSize: Custom.90x130mm")
        caps = printers.capabilities("Office")
        self.assertAlmostEqual(caps["custom_default"]["width"], 90)
        self.assertAlmostEqual(caps["custom_default"]["height"], 130)
        paper = next(o for o in caps["options"] if o["key"] == "PageSize")
        self.assertIn("Custom", [c["value"] for c in paper["choices"]])
        self.assertNotIn("Custom.90x130mm", [c["value"] for c in paper["choices"]])

    def test_custom_dimensions_stay_within_driver_limits(self):
        self.ppd = self.ppd.replace('*ParamCustomPageSize Width: "1 points 72 1000"', '*ParamCustomPageSize Width: "1 points 72 400"')
        caps = printers.capabilities("Office")
        self.assertAlmostEqual(caps["custom_default"]["width"], 400 * 25.4 / 72)

    def test_quality_aliases_are_prominent_only_when_advertised(self):
        self.preferences({"BrPriority": "primary"})
        self.assertFalse(any(o["key"].startswith("Br") for o in printers.capabilities("Office")["options"]))
        for key in ["BrPriority", "cupsPrintQuality"]:
            with self.subTest(key=key):
                original = self.ppd
                self.ppd += f'\n*OpenUI *{key}/Print quality: PickOne\n*Default{key}: Normal\n*{key} Normal/Normal: ""\n*{key} High/High: ""\n*CloseUI: *{key}\n'
                options = {o["key"]: o for o in printers.capabilities("Office")["options"]}
                self.assertEqual(options[key]["placement"], "primary")
                self.assertNotIn("print-quality", options)
                self.ppd = original

    def test_driverless_queues_keep_their_own_controls_and_media(self):
        self.conn.getPPD.side_effect = cups.IPPError(cups.IPP_NOT_FOUND, "No PPD")
        self.attrs.update({"media-supported": ["iso_a4_210x297mm"], "media-default": "iso_a4_210x297mm",
                           "media-col-database": [{"media-key": "iso_a4_210x297mm", "media-size": {"x-dimension": 21000, "y-dimension": 29700}}]})
        caps = printers.capabilities("Office")
        self.assertIsNone(caps["custom"])
        self.assertEqual(next(o for o in caps["options"] if o["key"] == "media")["placement"], "primary")
        self.assertAlmostEqual(printers.paper_size(caps, {})["width"] * 25.4 / 72, 210)
        self.assertFalse(any(o["key"].startswith("Br") for o in caps["options"]))

    def test_preferences_are_per_queue_and_do_not_create_controls(self):
        self.preferences({"Duplex": "primary", "Resolution": "advanced", "copies": "hidden", "ColorModel": "disabled", "UnknownFeature": "primary"})
        options = {o["key"]: o for o in printers.capabilities("Office")["options"]}
        self.assertEqual(options["Duplex"]["placement"], "primary")
        self.assertEqual(options["Resolution"]["placement"], "advanced")
        self.assertEqual(options["copies"]["placement"], "hidden")
        self.assertTrue(options["ColorModel"]["disabled"])
        self.assertNotIn("UnknownFeature", options)
        other = {o["key"]: o for o in printers.capabilities("Other")["options"]}
        self.assertEqual(other["Duplex"]["placement"], "driver")
        self.assertEqual(other["copies"]["placement"], "primary")
        self.assertFalse(other["ColorModel"]["disabled"])

    def test_disabling_aliases_blocks_driver_and_ipp_names(self):
        for disabled, attempts in [
            ("ColorModel", {"ColorModel": "Gray", "print-color-mode": "monochrome"}),
            ("sides", {"Duplex": "DuplexNoTumble", "sides": "two-sided-long-edge"}),
            ("Resolution", {"Resolution": "600dpi", "printer-resolution": "300x300dpi"}),
            ("media", {"PageSize": "Letter", "media": "na_letter_8.5x11in"}),
        ]:
            self.preferences({disabled: "disabled"})
            for key, value in attempts.items():
                with self.subTest(disabled=disabled, key=key), self.assertRaisesRegex(ValueError, "disabled changes"):
                    printers.validated_options("Office", {key: value})

    def test_api_rejects_disabled_options_before_preview_print_or_defaults(self):
        payload = self.payload()
        self.preferences({"ColorModel": "disabled"})
        authorization = base64.b64encode(("admin:" + server.ADMIN_PASSWORD).encode()).decode()
        headers = {**self.headers, "Authorization": "Basic " + authorization}
        with patch.object(server.documents, "prepare") as prepare, patch.object(server.subprocess, "run") as run:
            for route in ["/api/preview", "/api/print", "/api/printers/Office/defaults"]:
                for key, value in [("ColorModel", "Gray"), ("print-color-mode", "monochrome")]:
                    with self.subTest(route=route, key=key):
                        response = self.client.post(route, json={**payload, "options": {key: value}}, headers=headers)
                        self.assertEqual(response.status_code, 400, response.get_json())
                        self.assertIn("disabled changes", response.get_json()["error"])
            prepare.assert_not_called()
            run.assert_not_called()
        self.conn.printFile.assert_not_called()

    def test_hidden_controls_remain_available_to_api_and_other_queues(self):
        self.preferences({"copies": "hidden", "ColorModel": "disabled"})
        payload = self.payload()
        response = self.client.post("/api/print", json={**payload, "options": {"copies": "2"}}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(self.conn.printFile.call_args.args[3]["copies"], "2")
        self.assertNotIn("ColorModel", self.conn.printFile.call_args.args[3])
        _, options = printers.validated_options("Other", {"ColorModel": "Gray"})
        self.assertEqual(options["ColorModel"], "Gray")

    def test_hidden_and_disabled_layout_defaults_reach_pdf_renderer(self):
        self.attrs["number-up-default"] = 4
        payload = self.payload()
        for mode in ["hidden", "disabled"]:
            self.preferences({"number-up": mode})
            response = self.client.post("/api/preview", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["pages"], 1)
        response = self.client.post("/api/print", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(self.conn.printFile.call_args.args[3]["number-up"], "1")

    def test_driver_constraints_still_use_hidden_defaults(self):
        self.preferences({"ColorModel": "hidden"})
        with self.assertRaisesRegex(ValueError, "Conflicting driver settings"):
            printers.validated_options("Office", {"Duplex": "DuplexTumble"})

    def test_configuration_reload_and_invalid_configuration(self):
        self.preferences({"copies": "disabled"})
        with self.assertRaisesRegex(ValueError, "disabled changes"):
            printers.validated_options("Office", {"copies": "2"})
        self.config.write_text("{}")
        self.assertEqual(printers.validated_options("Office", {"copies": "2"})[1]["copies"], "2")
        for contents in ['{', '[]', '{"Office":{"control":{}}}', '{"Office":{"controls":{"copies":"disable"}}}']:
            self.config.write_text(contents)
            response = self.client.get("/api/printers/Office")
            self.assertEqual(response.status_code, 400)
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            printers.capabilities("Office")
        with patch.dict(os.environ):
            os.environ.pop("PRINTER_CONFIG")
            self.assertEqual(printers.control_preferences("Office"), {})


if __name__ == "__main__":
    unittest.main()
