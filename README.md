# CUPS web print interface

A minimal web interface for CUPS. Upload documents, preview and adjust their
layout, select printer settings, and manage print jobs from a browser.

The application uses Python, basic HTML, vanilla JavaScript and vendored
Bootstrap CSS. CUPS supplies printer drivers and queues. Controls come from
each queue's PPD and IPP capabilities, including paper sizes, colour, duplex,
quality, trays and finishing where available. Device-specific controls appear
only when that queue exposes them.

- Upload PDFs, raster images including HEIC/AVIF, and UTF-8 text.
- Preview scaling, rotation, cropping, content offsets and pages per sheet.
- Download the prepared PDF used for printing.
- Manage jobs and save printer and layout defaults per queue.
- Optionally choose prominent, advanced, hidden or disabled controls per printer.

The repository also includes a Docker print server for the Brother QL-1110NWB.
That deployment installs Brother's driver and supplies label-roll defaults.
See the [Brother setup guide](docs/brother.md). The web application can also
connect to an existing CUPS server with other configured printers. Hardware
support depends on the queue and driver; vendor functions absent from CUPS
are not available through the application.

## Connect to an existing CUPS server

Install and configure the printers in CUPS first. Then clone this repository
and build the standalone web service:

```bash
git clone https://github.com/sjtrny/label-printer.git
cd label-printer
CUPS_SERVER=192.168.1.20:631 docker compose -f compose.web.yaml up --detach --build
```

Replace the example CUPS address with your server's address. Open
`http://localhost:18680/` on the Docker host. For LAN access, also set `WEB_BIND`
to the host's LAN address. Set `WEB_PORT` to change the port. Use the same
environment values for subsequent Compose commands, or save them in `.env`.
The Docker container needs network access to CUPS, and CUPS must allow printing
from it. The service lists queues already configured on that CUPS server.

Printing requires no web login. The generated web administrator password
protects saving defaults, queue administration and other browsers' jobs:

```bash
CUPS_SERVER=192.168.1.20:631 docker compose -f compose.web.yaml exec web cat /data/admin-password
```

The web password does not authenticate to a remote CUPS server. Administrative
operations need CUPS permission; the bundled Brother deployment uses a shared
local Unix socket. For CUPS running on the Docker host, an explicit override
can mount its socket directory:

```yaml
# compose.socket.yaml
services:
  web:
    volumes:
      - /run/cups:/run/cups:ro
```

Run with `CUPS_SERVER=/run/cups/cups.sock` and both
`-f compose.web.yaml -f compose.socket.yaml`. CUPS socket permissions and local
policy must allow access. No remote CUPS credential support is added by this
mount.

The `print-ui-data` volume preserves the administrator password, uploads,
job records and layout defaults. Uploads and app records expire after seven
days. The [browser guide](minimal-web/README.md) describes formats, layout,
queue controls and the preview's limits.

## Per-printer controls

Normal operation needs no printer profiles. Settings and available options
come from CUPS. Custom-paper dimensions start from the queue's default paper
and follow the selected fixed size when switching to custom paper.

To customise a queue, create `/data/printers.json` in the web service's
persistent volume, or set `PRINTER_CONFIG` to a mounted JSON file. Queue names
and option keys must match CUPS; `GET /api/printers` lists names, and
`GET /api/printers/NAME` lists option keys and defaults.

```json
{
  "Office": {
    "controls": {
      "Duplex": "primary",
      "Resolution": "advanced",
      "job-priority": "hidden",
      "finishings": "disabled"
    }
  }
}
```

| Setting | Behaviour |
| --- | --- |
| `primary` | Put an advertised control beside paper size and copies. |
| `advanced` | Put it under Other print settings. |
| `hidden` | Omit the form control. API clients and Other print settings can still set it. |
| `disabled` | Disable the form control and reject changes through this app's API, including recognised driver/IPP aliases. Use the queue default. |

Unlisted options keep their normal placement. Profiles cannot add unsupported
capabilities. A disabled option does not turn off the hardware feature: it
prevents this application from changing its queue default. It does not affect
other CUPS clients. Saved CUPS defaults remain the source of default values.
Scaling, cropping, rotation and content offsets remain available for all
printers because the app applies them to the document.

Start from [the example](minimal-web/printers.example.json). With the same
Compose file and environment used to start your service, copy your edited file:

```bash
CUPS_SERVER=192.168.1.20:631 docker compose -f compose.web.yaml cp printers.json web:/data/printers.json
```

Preferences are read on each capability or submission request; refresh the
printer selection to update an open form. Removing an entry restores automatic
behaviour. An absent default file means no overrides. Invalid configuration,
or a missing explicitly configured `PRINTER_CONFIG` file, reports an error
instead of ignoring restrictions. Custom filenames can be mounted read-only
and supplied through a Compose environment override.

## Docker images

Every push to `main`, including a PR merge, runs
[CI](.github/workflows/ci.yaml) and then publishes two private images:

| Image | Purpose |
| --- | --- |
| `ghcr.io/sjtrny/label-printer-web` | General CUPS web interface |
| `ghcr.io/sjtrny/label-printer-cups` | CUPS with the Brother QL-1110NWB driver |

Images are currently published for `linux/amd64` with `latest` and
`sha-FULL_COMMIT_SHA` tags. The Brother image requires its 32-bit driver runtime;
the web application does not include that driver. Package names are retained
for existing deployments. Publishing an image does not update a running service.

Authenticate the Docker host to GHCR with a token that has `read:packages` and
access to this private repository. Keep the token out of `.env` and Git. For
the standalone service, run `pull` and then `up --detach --no-build` using
`compose.web.yaml` and your saved environment. `IMAGE_TAG` selects a specific
published revision. The [Brother guide](docs/brother.md) covers the complete
stack, existing IPP clients and optional HTTPS at `printer.local`.
