# syntax=docker/dockerfile:1
# DOP Agent container image.
#
#   docker build -t dop-agent .                                  # core only (no PDF/PNG rendering, no voice)
#   docker build --build-arg INSTALL_RENDER=1 -t dop-agent .     # + WeasyPrint/Pillow (cairo, pango, gdk-pixbuf)
#   docker build --build-arg INSTALL_VOICE=1  -t dop-agent .     # + openai-whisper + ffmpeg (large: pulls torch)
#
# Copilot CLI: github-copilot-sdk is a pure-Python wheel and does NOT bundle the CLI runtime. On first use it
# downloads a checksum-verified runtime bundle from GitHub Releases into ~/.cache/github-copilot-sdk. No Node.js
# or `npm i -g @github/copilot` is required. This image pre-fetches it at build time (set PREFETCH_COPILOT_CLI=0
# to skip, e.g. for offline builds; then set COPILOT_CLI_PATH or let the first run download it).
#
# Runtime requirements: WEB_UI_PASSWORD (or a secret file, see docker-entrypoint.sh) is mandatory and the
# container refuses to start without it. WEB_UI_HOST=0.0.0.0 is set INSIDE the container only; publish the port
# on 127.0.0.1 (see docker-compose.yml). The Web UI's Host-header allow-list accepts 127.0.0.1/localhost/[::1]
# on :10818; for any other hostname/port mapping add it to WEB_UI_ALLOWED_HOSTS (comma separated host:port).
FROM python:3.12-slim

ARG INSTALL_RENDER=0
ARG INSTALL_VOICE=0
ARG PREFETCH_COPILOT_CLI=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=10818

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && if [ "$INSTALL_RENDER" = "1" ]; then \
      apt-get install -y --no-install-recommends libcairo2 libpango-1.0-0 libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 libharfbuzz0b shared-mime-info fonts-dejavu-core fonts-noto-color-emoji; \
    fi \
 && if [ "$INSTALL_VOICE" = "1" ]; then \
      apt-get install -y --no-install-recommends ffmpeg; \
    fi \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin dop \
 && mkdir -p /home/dop/.dop-agent /home/dop/.copilot /app \
 && chown -R dop:dop /home/dop /app

WORKDIR /app
COPY requirements*.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_RENDER" = "1" ]; then pip install -r requirements-render.txt; fi \
 && if [ "$INSTALL_VOICE" = "1" ]; then pip install -r requirements-voice.txt; fi

COPY --chown=dop:dop . /app

USER dop
RUN if [ "$PREFETCH_COPILOT_CLI" = "1" ]; then python -m copilot download-runtime; fi

VOLUME ["/home/dop/.dop-agent"]
EXPOSE 10818

# /login is the unauthenticated page of the Web UI (/api/* requires a session).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/login' % os.environ.get('WEB_UI_PORT','10818'), timeout=4)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "dop_agent.py"]
