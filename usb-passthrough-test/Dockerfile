# Minimal USB-passthrough probe. This image does NOTHING but report what it can
# see under the bound USB mount — no copying, no uploading. It exists solely to
# validate that a hot-plugged SD card propagates into a privileged container on
# this NAS before we build the real watcher on top of the same mount setup.
FROM alpine:3.20

# GNU find/ls/date for predictable output; busybox covers grep/sed/printf.
RUN apk add --no-cache coreutils findutils

COPY probe.sh /usr/local/bin/probe.sh
RUN chmod +x /usr/local/bin/probe.sh

ENTRYPOINT ["/usr/local/bin/probe.sh"]
