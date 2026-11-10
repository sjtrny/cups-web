# Brother QL-1110NWB deployment

A Docker print server for an Ethernet Brother QL-1110NWB, with a minimal
Python browser interface. CUPS runs Brother's Linux driver on the server, so
clients can upload documents from any OS without installing that driver.

The browser interface uses basic HTML, vanilla JavaScript and vendored
Bootstrap CSS. It supports PDF and raster uploads, printer controls, quality,
colour previews, scaling, rotation, cropping and content offsets. CUPS also
shares a standard IPP queue. See [the browser guide](../minimal-web/README.md)
and the [CUPS web interface overview](../README.md).

## Run from source

The complete stack requires a Linux **amd64** Docker host and Docker Compose.
Brother's driver needs the 32-bit runtime installed in the CUPS image.

```bash
git clone https://github.com/sjtrny/cups-web.git
cd cups-web
cp .env.example .env
```

Edit `.env` for your host's LAN address and the printer's Ethernet address.
Reserve the printer's IP address in your router. Then build and start:

```bash
docker compose up --detach --build
docker compose ps
```

Open `http://HOST_IP:18680/`. The IPP queue is
`ipp://HOST_IP:18631/printers/Brother_QL1110NWB`.
The Docker host needs network access to the printer on TCP port 9100.

The default **4x6** preset is for a 152 mm label cut from a **102 mm continuous
roll**. Choose media that matches the roll actually loaded. Continuous lengths
require a preset in Brother's driver. `LABEL_MEDIA` sets the default for a new
queue; later defaults persist in the CUPS volume and can be changed in the
browser interface.

Printing on the LAN requires no login. Queue administration and saving
defaults require the app's generated administrator password:

```bash
docker compose exec web cat /data/admin-password
```

Named volumes preserve printer settings, the admin password, uploads, layout
defaults and job records. Uploads and app records expire after seven days.
CUPS does not retain completed job documents. Driver files are downloaded
from Brother during the image build and verified against a pinned SHA-256.

## Automatic Docker images

[Publish images](https://github.com/sjtrny/cups-web/actions/workflows/publish.yaml)
runs on **every push to `main`**, including PR merges. It first builds both
images and runs the checks from [CI](../.github/workflows/ci.yaml). Images are
published only when those checks pass. The workflow can also be run manually
for a selected branch.

| Image | Purpose |
| --- | --- |
| `ghcr.io/sjtrny/label-printer-cups` | CUPS and Brother's native driver |
| `ghcr.io/sjtrny/cups-web` | Python browser interface |

Each image has a `latest` tag and a `sha-FULL_COMMIT_SHA` tag. Both are
`linux/amd64`. The web image is public. The Brother CUPS image is private.
The publishing workflow checks their visibility. It uses GitHub's automatic
`GITHUB_TOKEN`; no Docker Hub account or repository secret is needed.

PRs run CI without publishing. CI checks JavaScript and shell syntax, starts
CUPS without networking, validates the PPD, and runs the Python document and
preview tests without a network or physical printer.

## Run the published images

The Brother CUPS image needs authentication. Authenticate the **Docker host** to
GHCR with a classic GitHub personal access token that has `read:packages` and
package access. Use
`docker login ghcr.io --username sjtrny` and enter the token at its password
prompt. Keep the token out of `.env` and Git.

Use the image override to pull and run the published containers:

```bash
docker compose -f compose.yaml -f compose.ghcr.yaml pull
docker compose -f compose.yaml -f compose.ghcr.yaml up --detach --no-build
```

Set `IMAGE_TAG=sha-FULL_COMMIT_SHA` in `.env` to use a specific revision.
The default is `latest`. The override uses the same services and volumes
as the source build. Publishing an image does not update a running deployment.

## HTTPS at printer.local

With an existing Caddy Docker proxy on the external `caddy_proxy` network,
add the rootful override. On the host's rootful Docker daemon:

```bash
docker --context default compose -f compose.yaml -f compose.rootful.yaml up --detach --build
```

Or use the published images:

```bash
docker --context default compose -f compose.yaml -f compose.ghcr.yaml -f compose.rootful.yaml pull
docker --context default compose -f compose.yaml -f compose.ghcr.yaml -f compose.rootful.yaml up --detach --no-build
```

This exposes `https://printer.local/` using Caddy's internal CA. Clients need
LAN name resolution for `printer.local` and trust in that CA. Direct web and
IPP ports remain available. USB passthrough, privileged mode and host
networking are not required.

## IPP clients

Windows can use its built-in IPP Class Driver:

```powershell
Add-Printer -Name "Brother Labels" -IppURL "ipp://HOST_IP:18631/printers/Brother_QL1110NWB"
```

On macOS, add an IP printer with Internet Printing Protocol, address
`HOST_IP:18631` and queue `printers/Brother_QL1110NWB`. On Linux with CUPS:

```bash
sudo lpadmin -p BrotherLabels -E -v ipp://HOST_IP:18631/printers/Brother_QL1110NWB -m everywhere
```

Automatic Bonjour discovery is disabled. Windows' generic system print dialog
may omit percentage scaling and cropping; the browser interface provides
these controls independently of the client driver.

References: [CUPS printer sharing](https://openprinting.github.io/cups/doc/sharing.html),
[Brother's Linux driver](https://support.brother.com/g/b/downloadend.aspx?c=us&dlid=dlfp100575_000&flang=178&lang=en&os=130&prod=lpql1110nwbeus&type3=10735),
[GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
