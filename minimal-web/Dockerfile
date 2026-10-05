FROM debian:bookworm-slim AS dependencies
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates cups-client python3-cups python3-flask python3-fitz \
       python3-pil libheif-examples gunicorn \
    && rm -rf /var/lib/apt/lists/*

FROM dependencies
WORKDIR /app
COPY app.py printers.py documents.py ./
COPY templates ./templates
COPY static ./static
ENV CUPS_SERVER=/run/cups/cups.sock STATE_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--preload", "--timeout", "120", "--access-logfile", "-", "app:app"]
