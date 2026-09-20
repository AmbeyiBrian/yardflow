# The backend image (design §12.2).
#
# One image runs all three processes — web, Celery worker, Celery beat — because
# they are the same code with different entrypoints, and three images would mean
# three things to keep in step.
#
# Two things here are not boilerplate:
#
# **Pango.** WeasyPrint renders the gate passes, GRNs and waybills (§11), and it
# needs Pango and its GObject libraries at *runtime*, not just at build time.
# Without them the app silently serves HTML where a PDF was asked for — which is
# exactly how a wrong pin went unnoticed for weeks in development.
#
# **The source is copied, not installed.** `pip install .` would place a second
# copy of the apps in site-packages, and the day one of them is missing from the
# packaging list the container would run a stale copy of it. Dependencies come
# from the project metadata; the code comes from the filesystem.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# WeasyPrint's runtime libraries (§11), plus curl for the container healthcheck.
RUN apt-get update && apt-get install --no-install-recommends -y \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libffi8 \
      libjpeg62-turbo \
      libopenjp2-7 \
      fonts-dejavu-core \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
#
# Read out of `pyproject.toml` rather than installed with `pip install .`:
# setuptools needs the package directories to exist, and they are not copied
# yet — which is the whole point of doing this before the source. Reading the
# pins keeps them in one place, with no second requirements file to drift.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . /app

# Collected here rather than at boot: it is the same every time the image is
# built, and doing it on start would run it once per process for no reason.
# A throwaway key and URL — `collectstatic` reads settings, and production
# settings refuse to import without them. Nothing from here reaches runtime.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY=build-only-not-a-secret \
    DJANGO_ALLOWED_HOSTS=localhost \
    DATABASE_URL=postgres://u:p@localhost/db \
    REDIS_URL=redis://localhost:6379/0 \
    AWS_STORAGE_BUCKET_NAME=build-only \
    python manage.py collectstatic --noinput

# Not root. The volume mounts the compose file makes are owned to match.
RUN useradd --create-home --uid 10001 yardflow \
    && chown -R yardflow:yardflow /app
USER yardflow

EXPOSE 8000

# Overridden by the worker and beat services; this is the web default.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
