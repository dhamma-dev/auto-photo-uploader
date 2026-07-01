# USB passthrough test

A throwaway probe to answer one question before we build the real pipeline:
**does a hot-plugged SD card propagate into a privileged container on this NAS?**

It copies and uploads nothing. It just prints what it can see under the bound USB
mount every few seconds, so you insert/remove a card and watch the container logs.

## 1. Publish the image

Default path is GHCR via GitHub Actions (`.github/workflows/usb-probe.yml`): push
this repo to GitHub and the image builds at `ghcr.io/<owner>/photo-usb-probe:latest`.
Make the package **public** (or add a pull credential in Portainer) so the NAS can
pull it.

Manual alternative:

```sh
cd usb-passthrough-test
docker build -t ghcr.io/dhamma-dev/photo-usb-probe:latest .
echo "$GHCR_TOKEN" | docker login ghcr.io -u dhamma-dev --password-stdin
docker push ghcr.io/dhamma-dev/photo-usb-probe:latest
```

## 2. Deploy in Portainer

Stacks -> Add stack -> paste `portainer-stack.yml`, set `OWNER`, deploy.

## 3. Run the test (watch the stack's logs)

- **Static:** insert a card, confirm `ls /volumeUSB1` on the NAS shows it, THEN
  deploy. Logs should list the card's files.
- **Hot-plug:** with the stack running, pull and re-insert the card. Logs should
  show it disappear and reappear.

| Result | Meaning |
|---|---|
| Both work | Passthrough is viable — build the watcher on it |
| Static works, hot-plug doesn't | rshared not honored — use Synology USB Copy for ingest |
| Even static shows nothing | Wrong port/path — check `ls /volumeUSB*` |

The probe also prints `/proc/self/mountinfo` for the mount: a `shared:` tag on the
`/mnt/usb` line means propagation is set up; no tag means it's private (hot-plug
won't work).
