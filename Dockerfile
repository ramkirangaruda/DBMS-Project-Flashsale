# API image, used by the api1/api2/api3 replicas in docker-compose.yml.
#
# The app previously only ever ran on the host (`python -m app.main`) talking
# to Postgres and Redis through their published ports. Containerising it
# changes nothing about the code -- every difference is environment:
# POSTGRES_HOST/REDIS_HOST become compose service names and the internal
# ports (5432/6379) replace the remapped published ones.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so edits to app/ do not invalidate the layer.
COPY requirements.txt .
# playwright is a screenshot tool for the write-ups, not a runtime dependency,
# and it is the largest thing in the file. Dropped from the image only.
RUN grep -v -i '^playwright' requirements.txt > /tmp/req.txt \
    && pip install -r /tmp/req.txt

COPY app ./app
# `frontend/dist` is gitignored, so on a fresh clone it does not exist until
# `npm run build` has been run -- and `COPY frontend/dist` then failed the
# whole image build before it started. Copying the `frontend/` directory
# instead works either way (.dockerignore already strips node_modules and
# src, so only package files and -- when built -- dist go in). Without dist
# the API still runs; it just serves the "Storefront not built yet" hint.
COPY frontend ./frontend

EXPOSE 8010

# app/main.py's __main__ block already binds 0.0.0.0 and reads PORT.
CMD ["python", "-m", "app.main"]
