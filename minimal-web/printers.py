"""Read and validate the controls published by CUPS and its printer drivers."""
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

import cups

cups.setServer(os.environ.get("CUPS_SERVER", "/run/cups/cups.sock"))
cups.setUser("root")

LABELS = {
    "copies": "Copies", "sides": "Sides", "media": "Paper size",
    "media-source": "Paper source", "media-type": "Paper type",
    "printer-resolution": "Resolution", "print-quality": "Print quality",
    "print-color-mode": "Color mode", "number-up": "Pages per sheet",
    "number-up-layout": "Page order on sheet", "orientation-requested": "Orientation",
    "page-ranges": "Pages", "page-set": "Page subset", "outputorder": "Output order",
    "page-border": "Page border", "job-hold-until": "Hold job until",
    "job-priority": "Job priority", "multiple-document-handling": "Collation",
}
ENUM_LABELS = {
    "orientation-requested": {3: "Portrait", 4: "Landscape", 5: "Reverse landscape", 6: "Reverse portrait"},
    "print-quality": {3: "Draft", 4: "Normal", 5: "High"},
    "finishings": {3: "None", 4: "Staple", 5: "Punch", 6: "Cover", 7: "Bind", 8: "Saddle stitch", 9: "Edge stitch"},
}
LAYOUT_OPTIONS = {
    "number-up": ([1, 2, 4, 6, 9, 16], 1),
    "number-up-layout": (["lrtb", "lrbt", "rltb", "rlbt", "tblr", "tbrl", "btlr", "btrl"], "lrtb"),
    "page-set": (["all", "odd", "even"], "all"),
    "outputorder": (["normal", "reverse"], "normal"),
    "page-border": (["none", "single", "double"], "none"),
}
# Compatibility names are used only when the selected queue exposes them.
OPTION_ALIASES = {
    "media": {"PageSize", "PageRegion", "media-col"},
    "sides": {"Duplex"},
    "print-color-mode": {"ColorModel", "ColorMode", "OutputMode", "BRMonoColor"},
    "printer-resolution": {"Resolution", "BRResolution"},
    "print-quality": {"BrPriority", "cupsPrintQuality"},
    "media-source": {"InputSlot"},
    "media-type": {"MediaType"},
}
PRIMARY_OPTIONS = {"PageSize", "media", "copies", "print-quality"} | OPTION_ALIASES["print-quality"]


def control_preferences(name):
    """Read optional per-queue presentation/restrictions, shared by all workers."""
    configured = os.environ.get("PRINTER_CONFIG")
    path = Path(configured or str(Path(os.environ.get("STATE_DIR", "/data")) / "printers.json"))
    try:
        profiles = json.loads(path.read_text())
    except FileNotFoundError:
        if not configured:
            return {}
        raise ValueError("The configured printer preferences file is missing.") from None
    except (OSError, ValueError):
        raise ValueError("The printer preferences file must contain valid JSON.") from None
    if not isinstance(profiles, dict):
        raise ValueError("Printer preferences must be an object keyed by CUPS queue name.")
    for queue, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) - {"controls"}:
            raise ValueError(f"Printer preferences for {queue} must contain only a controls object.")
        controls = profile.get("controls", {})
        if not isinstance(controls, dict) or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", key)
            or not isinstance(value, str) or value not in {"primary", "advanced", "hidden", "disabled"}
            for key, value in controls.items()
        ):
            raise ValueError(f"Invalid control preferences for {queue}.")
    return profiles.get(name, {}).get("controls", {})


def apply_preferences(caps):
    preferences = control_preferences(caps["name"])
    disabled = {key for key, value in preferences.items() if value == "disabled"}
    for standard, aliases in OPTION_ALIASES.items():
        if disabled & (aliases | {standard}):
            disabled.update(aliases | {standard})
    caps["disabled_options"] = sorted(disabled)
    caps["hidden_options"] = [key for key, value in preferences.items() if value == "hidden"]
    for option in caps["options"]:
        key = option["key"]
        placement = "primary" if key in PRIMARY_OPTIONS else "driver" if option["source"] == "driver" else "advanced"
        preference = preferences.get(key)
        option["placement"] = preference if preference in {"primary", "advanced", "hidden"} else placement
        option["disabled"] = key in disabled
    if caps["custom"]:
        try:
            size = paper_size(caps, {})
        except ValueError:
            size = next(iter(caps["sizes"].values()), {})
        caps["custom_default"] = {
            axis: max(limits["min"], min(limits["max"], size.get(axis, limits["min"] * 72 / 25.4) * 25.4 / 72))
            for axis, limits in caps["custom"].items()
        }
    return caps


def connection():
    return cups.Connection()


def scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@contextmanager
def driver(conn, name):
    path = None
    try:
        try:
            path = conn.getPPD(name)
        except cups.IPPError as exc:
            if exc.args[0] not in (cups.IPP_NOT_FOUND, cups.IPP_OPERATION_NOT_SUPPORTED):
                raise
        yield (cups.PPD(path), Path(path).read_text(errors="replace")) if path else (None, "")
    finally:
        if path:
            os.unlink(path)


def groups(ppd):
    def walk(group):
        for option in group.options:
            yield group.text or group.name, option
        for child in group.subgroups:
            yield from walk(child)
    if ppd:
        for group in ppd.optionGroups:
            yield from walk(group)


def control(key, label, values, default, group="Other controls", source="IPP"):
    items = [{"value": scalar(v), "label": ENUM_LABELS.get(key, {}).get(v, scalar(v))} for v in values]
    multiple = isinstance(default, (list, tuple))
    return {"key": key, "label": label, "kind": "text-list" if multiple else "select", "choices": items,
            "default": ",".join(scalar(v) for v in default) if multiple else scalar(default), "group": group, "source": source}


def capabilities(name, conn=None):
    conn = conn or connection()
    if name not in conn.getPrinters():
        raise ValueError("Printer is no longer available. Refresh the printer list.")
    attrs = conn.getPrinterAttributes(name)
    options, sizes, custom = [], {}, {}
    with driver(conn, name) as (ppd, ppd_text):
        dimensions, areas = {}, {}
        for keyword, choice, value in re.findall(r'^\*(PaperDimension|ImageableArea)\s+([^\s/:]+)(?:/.*?)?:\s*"([^\n]+)"', ppd_text, re.M):
            (dimensions if keyword == "PaperDimension" else areas)[choice] = [float(v) for v in value.split()]
        for axis, minimum, maximum in re.findall(r'^\*ParamCustomPageSize\s+(Width|Height)(?:/.*?)?:\s*"\d+\s+points\s+([0-9.]+)\s+([0-9.]+)"', ppd_text, re.M):
            custom[axis.lower()] = {"min": float(minimum) * 25.4 / 72, "max": float(maximum) * 25.4 / 72}
        for group, option in groups(ppd):
            # PageRegion is the driver's duplicate of PageSize, not a second paper selection.
            if option.keyword == "PageRegion":
                continue
            entry = control(option.keyword, option.text or option.keyword,
                            [c["choice"] for c in option.choices], option.defchoice, group, "driver")
            entry["choices"] = [{"value": c["choice"], "label": c["text"] or c["choice"]} for c in option.choices]
            if option.ui == cups.PPD_UI_PICKMANY:
                entry["kind"] = "multiple"
            else:
                try:
                    nums = sorted(int(c["choice"]) for c in option.choices)
                    if len(nums) > 10 and nums == list(range(nums[0], nums[-1] + 1)):
                        entry.update(kind="number", min=nums[0], max=nums[-1], step=1)
                except ValueError:
                    pass
            options.append(entry)
            if option.keyword == "PageSize":
                # CUPS can append the saved custom size as a second choice.
                # Use the Custom fields for that size instead of a fixed entry.
                if custom and any(c["value"] == "Custom" for c in entry["choices"]):
                    entry["choices"] = [c for c in entry["choices"] if not c["value"].startswith("Custom.")]
                for choice in option.choices:
                    size = dimensions.get(choice["choice"])
                    if size:
                        sizes[choice["choice"]] = {"width": size[0], "height": size[1],
                                                  "imageable": areas.get(choice["choice"])}
        known = {o["key"] for o in options}
        job_keys = set(attrs.get("job-creation-attributes-supported", []))
        candidates = job_keys | {"copies", "sides", "print-color-mode", "printer-resolution", "print-quality", "media", "media-source", "media-type", "output-bin", "finishings", "orientation-requested", "job-priority"}
        excluded = {"document-format", "document-name", "job-name", "job-originating-user-name", "requesting-user-name", "print-scaling", "page-ranges", "media-col"}
        if "PageSize" in known:
            excluded.add("media")
        if "Duplex" in known:
            excluded.add("sides")
        if known & {"ColorModel", "ColorMode"}:
            excluded.add("print-color-mode")
        if known & {"Resolution", "BRResolution"}:
            excluded.add("printer-resolution")
        if "InputSlot" in known:
            excluded.add("media-source")
        if "MediaType" in known:
            excluded.add("media-type")
        if known & {"BrPriority", "Resolution", "BRResolution", "cupsPrintQuality"}:
            excluded.add("print-quality")
        for key in sorted(candidates - known - excluded - LAYOUT_OPTIONS.keys()):
            supported = attrs.get(key + "-supported")
            if supported is None:
                continue
            default = attrs.get(key + "-default")
            label = LABELS.get(key, key.replace("-", " ").capitalize())
            group = "Print settings"
            if key == "printer-resolution":
                resolutions = supported if isinstance(supported, list) else [supported]
                values = [f"{r[0]}x{r[1]}{'dpi' if r[2] == 3 else 'dpcm'}" for r in resolutions]
                if isinstance(default, (tuple, list)) and len(default) == 3:
                    default = f"{default[0]}x{default[1]}{'dpi' if default[2] == 3 else 'dpcm'}"
                options.append(control(key, label, values, default or values[0], group))
            elif key == "job-priority":
                maximum = supported[0] if isinstance(supported, (tuple, list)) else supported
                options.append({"key": key, "label": label, "kind": "number", "min": 1,
                                "max": maximum, "step": 1, "default": scalar(default or 50), "group": group, "source": "IPP"})
            elif isinstance(supported, tuple) and len(supported) == 2:
                options.append({"key": key, "label": label, "kind": "number", "min": supported[0],
                                "max": supported[1], "step": 1, "default": scalar(default or supported[0]), "group": group, "source": "IPP"})
            elif isinstance(supported, list) and supported and all(isinstance(v, (str, int, bool)) for v in supported):
                options.append(control(key, label, supported, default if default is not None else supported[0], group))
            elif isinstance(supported, bool):
                options.append(control(key, label, [False, True], bool(default), group))
        for key, (values, default) in LAYOUT_OPTIONS.items():
            if key not in known:
                options.append(control(key, LABELS[key], values, attrs.get(key + "-default", default), "Layout"))
    # Driverless queues use PWG media sizes instead of PPD PaperDimension entries.
    for record in attrs.get("media-col-database", []):
        if not isinstance(record, dict):
            continue
        media = record.get("media-size", {})
        key = record.get("media-key") or record.get("media-size-name")
        if key and "x-dimension" in media and "y-dimension" in media:
            sizes[key] = {"width": media["x-dimension"] * 72 / 2540,
                          "height": media["y-dimension"] * 72 / 2540, "imageable": None}
    return apply_preferences({"name": name, "info": attrs.get("printer-info", name), "model": attrs.get("printer-make-and-model", ""),
            "location": attrs.get("printer-location", ""), "state": attrs.get("printer-state", 3),
            "reasons": attrs.get("printer-state-reasons", []), "accepting": attrs.get("printer-is-accepting-jobs", True),
            "options": options, "sizes": sizes, "custom": custom if len(custom) == 2 else None, "attributes": attrs})


def validated_options(name, supplied, conn=None):
    if not isinstance(supplied, dict) or len(supplied) > 100:
        raise ValueError("Print options must be an object with at most 100 entries.")
    conn = conn or connection()
    caps = capabilities(name, conn)
    controls = {o["key"]: o for o in caps["options"]}
    allowed = set(caps["attributes"].get("job-creation-attributes-supported", [])) | set(controls) | {"page-ranges"}
    forbidden = {"document-format", "document-name", "requesting-user-name", "job-name", "job-originating-user-name", "printer-uri", "device-uri", "PageRegion"}
    clean = {}
    for key, value in supplied.items():
        if key in caps["disabled_options"]:
            raise ValueError(f"The administrator has disabled changes to {key} for this printer.")
        if key in forbidden or key not in allowed or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", key):
            raise ValueError(f"Unsupported option: {key}")
        if value is None or value == "":
            continue
        value = scalar(value)
        if len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError(f"Invalid value for {key}.")
        item = controls.get(key)
        if item and item["kind"] == "number":
            try:
                number = int(value)
            except ValueError:
                raise ValueError(f"{item['label']} must be a whole number.") from None
            if not item["min"] <= number <= item["max"]:
                raise ValueError(f"{item['label']} must be between {item['min']} and {item['max']}.")
        elif item and key == "PageSize" and value.startswith("Custom.") and caps["custom"]:
            width, height = custom_dimensions(value)
            for axis, number in [("width", width * 25.4 / 72), ("height", height * 25.4 / 72)]:
                limits = caps["custom"][axis]
                if not limits["min"] <= number <= limits["max"]:
                    raise ValueError(f"Custom {axis} must be between {limits['min']:.1f} and {limits['max']:.1f} mm.")
        elif item:
            values = value.split(",") if item["kind"] in {"multiple", "text-list"} else [value]
            if any(v not in {c["value"] for c in item["choices"]} for v in values):
                raise ValueError(f"Invalid choice for {item['label']}.")
        clean[key] = value
    # These defaults must reach the PDF renderer even when a control is hidden
    # or disabled. CUPS scaling/N-up are disabled for the prepared document.
    for key in LAYOUT_OPTIONS.keys() | {"orientation-requested"}:
        if key in controls and key not in clean:
            clean[key] = controls[key]["default"]
    with driver(conn, name) as (ppd, _):
        if ppd:
            ppd.markDefaults()
            for key, value in clean.items():
                if key in controls and controls[key]["source"] == "driver":
                    for choice in value.split(","):
                        ppd.markOption(key, choice)
            if ppd.conflicts():
                conflicts = [o.text or o.keyword for _, o in groups(ppd) if o.conflicted]
                raise ValueError("Conflicting driver settings: " + ", ".join(conflicts))
    return caps, clean


def preview_color_mode(caps, options):
    """Resolve job colour controls first, then queue defaults and capabilities."""
    keys = ("print-color-mode", "ColorModel", "ColorMode", "OutputMode", "BRMonoColor")
    grayscale = {"monochrome", "processmonochrome", "automonochrome", "gray", "grey",
                 "grayscale", "greyscale", "kgray", "devicegray", "black", "blackonly",
                 "blackwhite", "blackandwhite", "bw", "mono"}
    bilevel = {"bilevel", "processbilevel", "binary", "threshold"}
    color = {"auto", "automatic", "color", "colour", "rgb", "rgba", "cmy", "cmyk",
             "devicergb", "devicecmyk"}
    defaults = {o["key"]: o["default"] for o in caps.get("options", [])}
    attrs = caps.get("attributes", {})
    sources = (options, defaults, {"print-color-mode": attrs.get("print-color-mode-default", "")})
    for source in sources:
        for key in keys:
            value = str(source.get(key, "")).lower().replace("-", "").replace("_", "").replace(" ", "")
            if value in grayscale:
                return "grayscale"
            if value in bilevel:
                return "bi-level"
            if value in color:
                return "grayscale" if attrs.get("color-supported") is False else "color"
    return "grayscale" if attrs.get("color-supported") is False else "color"


def custom_dimensions(value):
    match = re.fullmatch(r"Custom\.([0-9.]+)x([0-9.]+)(mm|in|pt)?", value)
    if not match:
        raise ValueError("Use Custom.<width>x<height> with numeric dimensions and optional mm, in or pt units.")
    factor = {"mm": 72 / 25.4, "in": 72, "pt": 1, None: 1}[match[3]]
    return float(match[1]) * factor, float(match[2]) * factor


def paper_size(caps, options):
    key = "PageSize" if any(o["key"] == "PageSize" for o in caps["options"]) else "media"
    default = next((o["default"] for o in caps["options"] if o["key"] == key), None)
    choice = options.get(key, default)
    size = caps["sizes"].get(choice)
    if isinstance(choice, str) and choice.startswith("Custom."):
        width, height = custom_dimensions(choice)
        size = {"width": width, "height": height, "imageable": None}
    if not size and isinstance(choice, str):
        # PWG media keywords end in their dimensions, e.g. na_index-4x6_4x6in.
        match = re.search(r"(?:_|Custom\.)([0-9.]+)x([0-9.]+)(mm|in|pt)$", choice)
        if match:
            factor = {"mm": 72 / 25.4, "in": 72, "pt": 1}[match[3]]
            size = {"width": float(match[1]) * factor, "height": float(match[2]) * factor, "imageable": None}
    if not size:
        raise ValueError("This paper size has no dimensions. Select a fixed paper size for preview and printing.")
    if not 1 <= size["width"] <= 20000 or not 1 <= size["height"] <= 20000:
        raise ValueError("Paper dimensions are out of range.")
    return size
