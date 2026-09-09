# Mario Camera Streamer — Docker image (v3.5: gunicorn + non-root)
# Run with --device=/dev/video10 --network=host for v4l2 + PulseAudio access.
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg v4l-utils pulseaudio-utils ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -G video,audio mario

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=mario:mario . .

USER mario
ENV MARIO_HOST=0.0.0.0 \
    MARIO_PORT=5000 \
    MARIO_DEBUG=0 \
    MARIO_LOG_LEVEL=INFO \
    MARIO_THREADS=16
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health',timeout=3).status<500 else 1)" || exit 1

# tini reaps zombie ffmpeg children if the worker crashes
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["gunicorn","-c","gunicorn_conf.py","app:app"]
