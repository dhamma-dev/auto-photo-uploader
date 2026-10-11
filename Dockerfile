# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# immich-go: pinned version, checksum-verified. DS923+/NUC are both x86_64.
ARG IMMICH_GO_VERSION=0.32.0
ADD --checksum=sha256:6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97 \
    https://github.com/simulot/immich-go/releases/download/v${IMMICH_GO_VERSION}/immich-go_Linux_x86_64.tar.gz /tmp/immich-go.tgz
RUN tar -xzf /tmp/immich-go.tgz -C /usr/local/bin immich-go && rm /tmp/immich-go.tgz

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Non-root. /data is the only writable path (a named volume; holds bounded import history).
RUN useradd --uid 1000 --create-home importer && mkdir /data && chown importer /data
COPY app/ /app/
USER importer
WORKDIR /app
ENV PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
