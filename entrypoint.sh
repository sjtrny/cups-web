#!/bin/sh
set -eu

: "${PRINT_HOST:?Set PRINT_HOST to the server's LAN IPv4 address}"
: "${PRINTER_URI:?Set PRINTER_URI to the printer's Ethernet socket endpoint}"
QUEUE_NAME=${QUEUE_NAME:-Brother_QL1110NWB}

case "$PRINT_HOST" in
  *[!0-9.]*|'') echo "PRINT_HOST must be an IPv4 address" >&2; exit 1 ;;
esac
case "$QUEUE_NAME" in
  *[!a-zA-Z0-9_-]*|'') echo "Invalid queue name" >&2; exit 1 ;;
esac
case "$PRINTER_URI" in
  socket://*) ;;
  *) echo "PRINTER_URI must start with socket://" >&2; exit 1 ;;
esac

mkdir -p /run/cups /var/spool/cups /var/cache/cups /tmp/cups
chown lp:lp /tmp/cups
chmod 1770 /tmp/cups
rm -f /run/cups/label-printer-ready

# Keep Ghostscript's scratch files in a location that works for the actual
# lp filter user. Update persisted configuration as well as fresh volumes.
sed -i '/^TempDir /d; /^ErrorLog /d; /^AccessLog /d; /^PageLog/d' /etc/cups/cups-files.conf
printf '\nTempDir /tmp/cups\nErrorLog stderr\nAccessLog stderr\nPageLog\n' >> /etc/cups/cups-files.conf

# Retain Debian's authentication policies. Clients can print and view jobs;
# administrative writes remain restricted to the local Unix socket.
sed \
  -e 's/^Listen localhost:631$/Listen 0.0.0.0:18631/' \
  -e 's/^Browsing .*/Browsing Off/' \
  -e '/^<Location \/>$/,/^<\/Location>$/ { /^  Order allow,deny$/a\
  Allow all
  }' \
  /usr/local/share/cupsd.original.conf > /etc/cups/cupsd.conf
printf '\nServerName %s\nServerAlias localhost 127.0.0.1\nPreserveJobFiles No\n' "$PRINT_HOST" >> /etc/cups/cupsd.conf

cupsd -f &
cups_pid=$!
stop_cups() {
  trap - TERM INT EXIT
  kill "$cups_pid" 2>/dev/null || true
  # Let CUPS flush printer/default/job configuration before PID 1 exits.
  wait "$cups_pid" 2>/dev/null || true
}
trap stop_cups TERM INT EXIT
export CUPS_SERVER=/run/cups/cups.sock

attempt=0
# lpstat -r returns success even when it reports "scheduler is not running".
# Check the response instead of treating its exit code as readiness.
until LC_ALL=C lpstat -r 2>/dev/null | grep -q '^scheduler is running'; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 30 ]; then echo "CUPS did not start" >&2; exit 1; fi
  sleep 1
done

# Clients keep using the shared IPP queue. The server renders through
# Brother's model-specific driver and sends native raster over Ethernet.
queue_new=0
if lpstat -p "$QUEUE_NAME" >/dev/null 2>&1; then
  # Keep driver choices saved through the web interface across restarts.
  lpadmin -p "$QUEUE_NAME" -v "$PRINTER_URI" \
    -D "Brother QL-1110NWB via Docker" -o printer-is-shared=true
else
  queue_new=1
  lpadmin -p "$QUEUE_NAME" -v "$PRINTER_URI" \
    -P /usr/local/share/brother-label-printer.ppd \
    -D "Brother QL-1110NWB via Docker" -o printer-is-shared=true
  lpadmin -p "$QUEUE_NAME" -o print-color-mode-default=monochrome
  lpadmin -p "$QUEUE_NAME" -o print-scaling-default=none
  # Honor the requested page length instead of trimming away blank rows.
  lpadmin -p "$QUEUE_NAME" -o BrTrimtape=OFF
  if [ -n "${LABEL_MEDIA:-}" ]; then
    lpadmin -p "$QUEUE_NAME" -o "PageSize=$LABEL_MEDIA"
  fi
fi
if [ "$queue_new" -eq 1 ]; then
  lpadmin -d "$QUEUE_NAME"
  cupsenable "$QUEUE_NAME"
  cupsaccept "$QUEUE_NAME"
fi
touch /run/cups/label-printer-ready
wait "$cups_pid"
