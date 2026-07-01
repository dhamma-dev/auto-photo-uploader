FROM node:20-slim

# rsync for verified copies, curl for ntfy pings, immich CLI for the upload.
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @immich/cli \
 && npm cache clean --force

COPY watch-import.sh /usr/local/bin/watch-import.sh
RUN chmod +x /usr/local/bin/watch-import.sh

ENTRYPOINT ["/usr/local/bin/watch-import.sh"]
