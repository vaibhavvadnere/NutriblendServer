# Nutriblend API — production image.
# Contains Python, the app, LibreOffice (Word/PowerPoint -> PDF) and ffmpeg/ffprobe
# (automatic video optimization, video duration). Built on the server itself (EC2 t3 = x86_64):
#     docker compose up -d --build
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# LibreOffice (headless) for DOCX/PPTX -> PDF, ffprobe for video metadata,
# fonts so converted pages (incl. Hindi/Marathi) render correctly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-impress-nogui \
        ffmpeg fonts-dejavu-core fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
# Videos waiting for optimization (a volume in docker-compose.yml, so they survive restarts).
RUN mkdir -p /staging && chown appuser:appuser /staging
USER appuser

EXPOSE 8000
# One worker: background document conversion runs in-process, and 2 GiB RAM is
# shared with LibreOffice and ffmpeg (video optimization). --proxy-headers: requests arrive via Cloudflare Tunnel.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
