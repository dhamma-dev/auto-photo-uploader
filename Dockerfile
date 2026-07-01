# photo-import watcher image: immich-go (single static binary) + a small bash
# loop. Debian base (glibc) since we run a prebuilt immich-go release binary.
FROM debian:stable-slim
ARG IMMICH_GO_VERSION=0.32.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://github.com/simulot/immich-go/releases/download/v${IMMICH_GO_VERSION}/immich-go_Linux_x86_64.tar.gz" \
      | tar -xz -C /usr/local/bin immich-go \
 && chmod +x /usr/local/bin/immich-go

COPY watch-import.sh /usr/local/bin/watch-import.sh
RUN chmod +x /usr/local/bin/watch-import.sh

ENTRYPOINT ["/usr/local/bin/watch-import.sh"]
