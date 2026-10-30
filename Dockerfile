# Container image for Rammonte USA.
#
# Used by Northflank (and works unchanged on Render, Railway, Fly.io and any
# Docker host). ffmpeg is installed from the distribution because the static
# binary bundled by imageio-ffmpeg crashes on network input here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# A real, dynamically linked ffmpeg. The static binary that imageio-ffmpeg
# ships segfaults on network input inside this image, so reading media straight
# from R2 (length probes, MP3 extraction) only works with the system package.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are copied first so the layer is reused when only code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The Django project lives in the rammonte_usa/ subdirectory.
WORKDIR /app/rammonte_usa

# Collect the static files at build time. WhiteNoise then serves them from
# inside the container, so no separate static-file service is needed.
#
# The variable must be DEBUG: that is the name rammonte_usa/settings.py reads.
# With it left at 1, collectstatic uses the plain storage backend and writes no
# staticfiles.json. Production then serves with the manifest backend and every
# page fails with "Missing staticfiles manifest entry". The guard below turns
# that into a failed build instead of a broken deployment.
RUN DJANGO_SETTINGS_MODULE=rammonte_usa.settings \
    DEBUG=0 \
    SECRET_KEY=build-only-not-used-at-runtime \
    python manage.py collectstatic --noinput \
 && test -f staticfiles/staticfiles.json \
 && python manage.py compiletranslations

EXPOSE 8000

# Applied on every start: migrate, then serve. gunicorn runs several workers so
# the app stays responsive while a background conversion is running.
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn rammonte_usa.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 180 --access-logfile -"]