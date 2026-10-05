FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates cups cups-client cups-ipp-utils cups-filters ghostscript \
       curl libc6-i386 perl \
    && rm -rf /var/lib/apt/lists/* \
    && cp /etc/cups/cupsd.conf /usr/local/share/cupsd.original.conf

COPY install-driver.sh /usr/local/bin/install-label-printer-driver
RUN sh /usr/local/bin/install-label-printer-driver

COPY entrypoint.sh /usr/local/bin/label-printer-entrypoint
RUN chmod 755 /usr/local/bin/label-printer-entrypoint

EXPOSE 18631
ENTRYPOINT ["/usr/local/bin/label-printer-entrypoint"]
