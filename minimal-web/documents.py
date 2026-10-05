"""Produce the PDF that both the preview and the print job use."""
import io
import math
import struct
import subprocess
import tempfile
import textwrap
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageOps

MAX_PAGES = 500
MAX_IMAGE_PIXELS = 40_000_000
METADATA_ERRORS = (OSError, SyntaxError, ValueError, TypeError, AttributeError,
                   IndexError, KeyError, struct.error)
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
               b"mif1", b"msf1", b"avif", b"avis"}


def is_heif(data):
    """Recognize HEIF/HEIC/AVIF ISO media containers by their declared brands."""
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    brands = {data[index:index+4] for index in range(8, min(len(data), 256), 4)}
    return bool(brands & HEIF_BRANDS)


def add_image_page(doc, frame):
    if doc.page_count >= MAX_PAGES:
        raise ValueError(f"At most {MAX_PAGES} image pages are supported.")
    if frame.width * frame.height > MAX_IMAGE_PIXELS:
        raise ValueError("Image is too large. Use an image below 40 megapixels.")
    try:
        # Orientation is useful, but corrupt optional metadata must never make
        # otherwise valid pixels unprintable (common with converted phone MPOs).
        frame = ImageOps.exif_transpose(frame)
    except METADATA_ERRORS:
        frame.info.pop("exif", None)
        frame = frame.copy()
    frame = frame.convert("RGBA")
    background = Image.new("RGB", frame.size, "white")
    background.paste(frame, mask=frame.getchannel("A"))
    content = io.BytesIO()
    background.save(content, format="PNG")
    # Image uploads are fitted to the selected sheet by the UI.
    page = doc.new_page(width=frame.width * .24, height=frame.height * .24)
    page.insert_image(page.rect, stream=content.getvalue())


def add_pillow_image(doc, image, original=None):
    """Add every readable frame without seeking frame zero again."""
    try:
        direct_jpeg = (original is not None and image.format == "JPEG"
                       and image.mode in {"RGB", "L"} and image.getexif().get(274, 1) == 1)
    except METADATA_ERRORS:
        direct_jpeg = False
    if direct_jpeg:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image is too large. Use an image below 40 megapixels.")
        image.load()  # Verify pixels before retaining the original JPEG encoding.
        page = doc.new_page(width=image.width * .24, height=image.height * .24)
        page.insert_image(page.rect, stream=original)
    else:
        add_image_page(doc, image)
    for position in range(1, MAX_PAGES + 1):
        try:
            image.seek(position)
        except EOFError:
            return
        except METADATA_ERRORS:
            # Keep already decoded frames when only a secondary MPO/metadata
            # image is damaged. The primary visible photo remains printable.
            return
        add_image_page(doc, image)
    raise ValueError(f"At most {MAX_PAGES} image pages are supported.")


def add_heif_image(doc, path):
    """Decode HEIC, HEIF and AVIF with Debian's sandboxed command-line codec."""
    with tempfile.TemporaryDirectory(prefix="decode-", dir=path.parent) as folder:
        output = Path(folder) / "image.png"
        try:
            result = subprocess.run(
                ["/usr/bin/heif-convert", "--quiet", str(path), str(output)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("This HEIC, HEIF or AVIF image could not be decoded.") from exc
        images = sorted(Path(folder).glob("image*.png"))
        if result.returncode or not images:
            raise ValueError("This HEIC, HEIF or AVIF image could not be decoded.")
        for image_path in images:
            with Image.open(image_path) as image:
                add_pillow_image(doc, image)


def normalize(path, filename):
    data = path.read_bytes()
    if data.startswith(b"%PDF-"):
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            doc.close()
            raise ValueError("Unlock the PDF before uploading it.")
        kind = "pdf"
    elif filename.lower().endswith(".txt"):
        text = data.decode("utf-8-sig")
        lines = [part for line in text.splitlines() for part in (textwrap.wrap(line.expandtabs(4), 90) or [""])]
        doc, kind = fitz.open(), "text"
        for start in range(0, max(1, len(lines)), 65):
            doc.new_page(width=595, height=842).insert_text((36, 48), "\n".join(lines[start:start+65]), fontname="cour", fontsize=9)
    else:
        doc, kind = fitz.open(), "image"
        try:
            if is_heif(data):
                add_heif_image(doc, path)
            else:
                with Image.open(io.BytesIO(data)) as image:
                    add_pillow_image(doc, image, data)
        except ValueError:
            doc.close()
            raise
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            doc.close()
            raise ValueError("Upload a PDF, supported raster image, or UTF-8 text file.") from exc
        except Exception:
            doc.close()
            raise
    try:
        if not 0 < doc.page_count <= MAX_PAGES:
            raise ValueError(f"Documents must have 1–{MAX_PAGES} pages.")
        if any(not (1 <= p.rect.width <= 20000 and 1 <= p.rect.height <= 20000) for p in doc):
            raise ValueError("Document page dimensions are out of range.")
        path.with_name("source.pdf").write_bytes(doc.tobytes(garbage=3, deflate=True))
        return {"kind": kind, "pages": doc.page_count, "width": doc[0].rect.width, "height": doc[0].rect.height}
    finally:
        doc.close()


def selected_pages(count, ranges, subset="all", reverse=False):
    chosen = set()
    if ranges.strip():
        for part in ranges.replace(" ", ",").split(","):
            if not part:
                continue
            try:
                values = [int(x) for x in part.split("-")]
            except ValueError:
                raise ValueError("Pages must look like 1-3,5,8.") from None
            if len(values) == 1:
                first = last = values[0]
            elif len(values) == 2:
                first, last = values
            else:
                raise ValueError("Pages must look like 1-3,5,8.")
            if not 1 <= first <= last <= count:
                raise ValueError(f"Page numbers must be between 1 and {count}.")
            chosen.update(range(first-1, last))
    else:
        chosen.update(range(count))
    pages = [i for i in sorted(chosen) if subset == "all" or (i % 2 == 0 if subset == "odd" else i % 2 == 1)]
    if not pages:
        raise ValueError("No pages match the selected range and subset.")
    return pages[::-1] if reverse else pages


def layout_settings(supplied):
    if not isinstance(supplied, dict):
        raise ValueError("Layout must be an object.")
    mode = supplied.get("scale", "none")
    if mode not in {"none", "fit", "fill", "custom"}:
        raise ValueError("Choose actual size, fit, fill or custom scaling.")
    try:
        percent = float(supplied.get("percent", 100))
        rotation = int(supplied.get("rotation", 0))
    except (TypeError, ValueError):
        raise ValueError("Scale and rotation must be numbers.") from None
    if not math.isfinite(percent) or not 1 <= percent <= 1000:
        raise ValueError("Scale must be between 1 and 1000 percent.")
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("Rotation must be 0, 90, 180 or 270 degrees.")
    try:
        offsets = {key: float(supplied.get(key, 0)) for key in ("offset_x", "offset_y")}
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Horizontal and vertical offsets must be numbers in millimetres.") from None
    if any(not math.isfinite(value) or not -1000 <= value <= 1000 for value in offsets.values()):
        raise ValueError("Offsets must be between -1000 and 1000 millimetres.")
    return {"scale": mode, "percent": percent, "rotation": rotation,
            "crop": bool(supplied.get("crop", False)), **offsets}


def crop_white(page):
    zoom = min(1, 1500 / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    difference = ImageChops.invert(image).point(lambda value: 255 if value > 12 else 0)
    bbox = difference.getbbox()
    if not bbox:
        return page.rect
    # Keep a two-pixel border so antialiased text and barcode edges are retained.
    return fitz.Rect(max(0, bbox[0]-2)/zoom, max(0, bbox[1]-2)/zoom,
                     min(pix.width, bbox[2]+2)/zoom, min(pix.height, bbox[3]+2)/zoom) & page.rect


def cell_order(columns, rows, order):
    xs, ys = list(range(columns)), list(range(rows))
    if order.startswith("rl") or order.endswith("rl"):
        xs.reverse()
    if order.startswith("bt") or order.endswith("bt"):
        ys.reverse()
    if order[:2] in {"tb", "bt"}:
        return [(x, y) for x in xs for y in ys]
    return [(x, y) for y in ys for x in xs]


def prepare(source_path, destination, size, options, layout):
    layout = layout_settings(layout)
    width, height = size["width"], size["height"]
    number_up = int(options.get("number-up", 1))
    columns, rows = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3), 16: (4, 4)}[number_up]
    orientation = {3: 0, 4: 90, 5: 270, 6: 180}.get(int(options.get("orientation-requested", 3)), 0)
    rotation = (layout["rotation"] + orientation) % 360
    # Offsets use physical paper axes, independent of scale and content rotation.
    dx, dy = layout["offset_x"] * 72 / 25.4, layout["offset_y"] * 72 / 25.4
    with fitz.open(source_path) as source, fitz.open() as result:
        indices = selected_pages(source.page_count, options.get("page-ranges", ""), options.get("page-set", "all"), options.get("outputorder") == "reverse")
        for start in range(0, len(indices), number_up):
            page = result.new_page(width=width, height=height)
            for index, (column, row) in zip(indices[start:start+number_up], cell_order(columns, rows, options.get("number-up-layout", "lrtb"))):
                cell = fitz.Rect(column*width/columns, row*height/rows, (column+1)*width/columns, (row+1)*height/rows)
                clip = crop_white(source[index]) if layout["crop"] else source[index].rect
                content_width, content_height = (clip.height, clip.width) if rotation % 180 else (clip.width, clip.height)
                fit = min(cell.width/content_width, cell.height/content_height)
                scale = {"none": fit if number_up > 1 else 1, "fit": fit,
                         "fill": max(cell.width/content_width, cell.height/content_height),
                         "custom": layout["percent"]/100 * (fit if number_up > 1 else 1)}[layout["scale"]]
                target = fitz.Rect(cell.x0+(cell.width-content_width*scale)/2, cell.y0+(cell.height-content_height*scale)/2,
                                   cell.x0+(cell.width+content_width*scale)/2, cell.y0+(cell.height+content_height*scale)/2)
                target += (dx, dy, dx, dy)
                if source[index].get_contents() and not (target & cell).is_empty:
                    # A one-cell PDF clips fill/oversize content before placing it in an N-up sheet.
                    with fitz.open() as clipped:
                        tile = clipped.new_page(width=cell.width, height=cell.height)
                        tile.show_pdf_page(target - (cell.x0, cell.y0, cell.x0, cell.y0), source, index,
                                           rotate=rotation, clip=clip, keep_proportion=False)
                        page.show_pdf_page(cell, clipped, 0)
                if options.get("page-border", "none") != "none":
                    page.draw_rect(cell + (1, 1, -1, -1), color=(0, 0, 0), width=.5)
                    if options["page-border"] == "double":
                        page.draw_rect(cell + (3, 3, -3, -3), color=(0, 0, 0), width=.5)
        destination.write_bytes(result.tobytes(garbage=3, deflate=True))
        return result.page_count


def preview(path, page_number, color_mode="color"):
    with fitz.open(path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Preview page must be between 1 and {doc.page_count}.")
        page = doc[page_number-1]
        zoom = min(1.5, 1400 / max(page.rect.width, page.rect.height))
        colorspace = fitz.csRGB if color_mode == "color" else fitz.csGRAY
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=colorspace, alpha=False)
        if color_mode == "bi-level":
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            output = io.BytesIO()
            image.convert("1", dither=Image.Dither.NONE).save(output, format="PNG")
            return output.getvalue(), doc.page_count
        return pixmap.tobytes("png"), doc.page_count
