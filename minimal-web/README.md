# Minimal print interface

Live: <https://printer.local/>. Direct access: <http://192.168.20.10:18680/>.

Python, plain HTML, vanilla JavaScript and Bootstrap 5.3.8 CSS. No frontend
framework, JavaScript build, external fonts, CDN requests or custom stylesheet.
CUPS remains the driver and queue backend. The application uses Flask, pycups,
PyMuPDF, Pillow and libheif from Debian packages and runs under Gunicorn.

The **Print** tab contains the controls and preview, separated by a vertical
line on desktop. They stack on smaller screens. The **Print queue** tab shows
jobs and their actions; switching tabs keeps the document and settings.
Long document and printer names wrap within the queue table without stretching
the other columns. Full filenames remain readable.
The **Info** button directly below the printer select opens its capabilities
in a modal. **Refresh printers** shares that row and aligns with the dropdown's
right edge; the printer state appears beside the dropdown.
Close the modal with the close button, Escape or a click outside it.
Tabs support arrow keys, Home and End. No additional JavaScript library is used.

## Printing

1. Select a configured CUPS printer and upload a PDF, image or UTF-8 text file.
2. Select the paper loaded in that printer. The Brother queue defaults to
   **4x6 on 102mm continuous roll**.
3. Adjust quality and the other driver controls. For the Brother, quality is
   **Give priority to print speed** or **Give priority to print quality**.
4. Choose actual size, fit, fill or a custom percentage (1–1000%). Use
   **Crop white margins before scaling** when the label artwork occupies
   only part of the source PDF page. Use **Horizontal offset (mm)** and
   **Vertical offset (mm)** to move the content within the print region.
   Positive values move right/down; negative values move left/up. Offsets
   apply after scaling and rotation, so a 5 mm shift stays 5 mm at any scale.
   Paper size stays fixed; content outside the region is clipped. For N-up,
   each page moves within its own cell. Rotation and page ranges are also
   supported.
5. Check the layout preview, then press **Print**. The download link returns
   the same prepared PDF submitted to CUPS.

Uploads, preview updates and print submission show a Bootstrap loading spinner.
The previous preview is dimmed during an update; Print and page navigation stay
disabled until the new preview image is decoded. Rapid changes discard stale
responses, errors clear the loading state, and background job polling is quiet.
Setting changes are grouped over 120 ms before requesting a preview.

Prepared PDFs and rendered previews are cached within each upload's existing
seven-day lifetime. Changes to paper, layout, page or colour invalidate the
relevant cache; copies, quality and other hardware controls reuse the unchanged
preview while keeping distinct job options and submission identities. File locks
and atomic writes share caches across the two worker processes. Ordinary RGB or
grayscale JPEGs retain their original encoding rather than being recompressed
as PNG; rotated/mirrored, malformed-metadata and other image types keep the
existing conversion path.

Printer controls are discovered from the queue's PPD and IPP attributes.
All 12 Brother driver controls are exposed: media size, feed, cut interval,
cut at end, trimming, mirroring, speed/quality, start-print timing, vertical
thin-line adjustment, halftoning, brightness and contrast. Numeric driver
choices become number inputs; other choices remain selects. Driver constraints
are checked on the server. Other printers expose their own installed driver's
options, including color, duplex, resolution, trays, finishing and custom
paper sizes where supported. Additional advertised job attributes can be
entered as `option=value` under **Other print settings**.

Capabilities are those of configured CUPS queues. Vendor functions not exposed
through CUPS are outside this MVP. Paper size choices must match the loaded
roll or sheet; Brother continuous lengths require a driver preset. The
preview reflects document layout and the selected colour mode, including
queue defaults: colour remains colour, grayscale/IPP monochrome removes
colour, and bi-level uses black/white thresholding. IPP and common driver
controls (`ColorModel`, `ColorMode`, `OutputMode`, `BRMonoColor`) are recognised.
Printer halftoning, thermal behavior, cutting and mirroring are applied when
printing. Colour conversion affects the preview image; the prepared PDF retains
its vector content and the driver applies the selected mode to the print job.
A completed CUPS job means the backend finished transferring it; physical
confirmation depends on the backend.

Image types are detected from their contents, so a correct filename extension
is not required. Tested formats include JPEG/JFIF/MPO, PNG, HEIC/HEIF, AVIF,
TIFF, GIF, WebP, BMP, JPEG 2000 and transparent or animated/multipage images;
the installed Pillow codecs accept additional raster formats. Broken optional
EXIF/MPO metadata is ignored when the primary pixels remain readable. Invalid
or unsupported files return a clear client error instead of an internal server
error. Limits: 50 MB per upload, 500 pages and 40 megapixels per image. Unlock
password-protected PDFs before uploading. Camera RAW, SVG, office files and
scanning are not included. Uploads and local job records are cleaned after
seven days when another file is uploaded.

## Jobs and defaults

The job table shows the latest 100 CUPS jobs. A browser can cancel, hold or
release jobs it submitted. The application stores the upload token in that
browser's local storage; managing other jobs requires the admin password.

Open **Printer defaults and queue controls** and enter the password from the
generated `/data/admin-password` file in the web container (see the parent
[setup guide](../README.md)). Printing itself requires no login
on the LAN. The admin can save the form as printer defaults, save the app's
layout defaults, pause/resume printing, accept/reject jobs and select the
default queue. Layout defaults include scaling and both content offsets.
CUPS driver defaults and application settings survive restart.
Page ranges and hold/PIN options are not saved as printer defaults.

## Docker

The parent [Compose bundle](../compose.yaml) and
[rootful override](../compose.rootful.yaml) run in the physical host's rootful
Docker daemon. Caddy serves `printer.local` on HTTPS using its existing local
CA; direct access stays on port 18680 and CUPS stays on port 18631.
`print-ui-data` preserves the app's
password, layout defaults, uploads and local job records. `cups-runtime`
shares CUPS's Unix socket read-only with the app, so queue administration
works without opening CUPS's remote admin interface.

From the project directory on the Docker host:

```bash
docker compose up --detach --build
```

The parent [setup guide](../README.md) covers the optional HTTPS override
and private GHCR images. After web source changes:

```bash
docker compose build web
docker compose up --detach --no-deps --no-build web
```

The image includes all browser assets. The uploaded document is normalized
to PDF; the app applies layout once and sends the prepared PDF with CUPS
scaling and N-up disabled. The installed CUPS driver performs the printer's
quality, brightness, halftoning and other device-specific operations.

For an existing external CUPS server, `CUPS_SERVER=host:port` enables printing
and capability discovery. Queue administration requires an authenticated local
CUPS socket as in this bundle; the web admin password is separate from CUPS
authentication.

## Source and checks

- `app.py`: HTTP routes, uploads, job management and defaults.
- `printers.py`: dynamic PPD/IPP controls, paper sizes and option validation.
- `documents.py`: image/text conversion and PDF layout/preview.
- `templates/index.html`, `static/app.js`: the complete browser interface.
- `static/bootstrap.min.css`: vendored Bootstrap, verified against its official
  SHA-384; its license is beside it.
- `test_documents.py`: rendered geometry, vector preservation, scaling,
  offsets, rotation, clipping, N-up, cropping, image codecs and metadata,
  direct JPEG encoding, ranges and encrypted PDFs.
- `test_preview.py`: colour modes, queue defaults, cache reuse/invalidation,
  matching preview/download content and unchanged native job options.
- `check_api.py`, `test-generic.ppd`: isolated integration checks. Run these
  only against capture queues in containers with `--network none`.

Run the unit checks in the production web image, with no network:

```bash
docker compose build web
docker run --rm --network none \\
  -e STATE_DIR=/tmp/print-preview-state -e PYTHONDONTWRITEBYTECODE=1 \\
  --mount "type=bind,source=$PWD/minimal-web/test_documents.py,target=/app/test_documents.py,readonly" \\
  --mount "type=bind,source=$PWD/minimal-web/test_preview.py,target=/app/test_preview.py,readonly" \\
  local/minimal-print-web:bookworm python3 -m unittest -v test_documents test_preview
```

The [GitHub workflows](../.github/workflows/) run these checks on PRs and
before publishing images for updates to `main`.

References: [CUPS options](https://openprinting.github.io/cups/doc/options.html),
[pycups](https://github.com/OpenPrinting/pycups),
[Bootstrap](https://getbootstrap.com/docs/5.3/getting-started/introduction/),
[PyMuPDF](https://pymupdf.readthedocs.io/en/latest/page.html),
[PWG IPP colour modes](https://ftp.pwg.org/pub/pwg/candidates/cs-ippnodriver20-20230301-5100.13.pdf).
