#!/bin/sh
# ---------------------------------------------------------------------------
# USB passthrough probe
#
# Prints, every few seconds, exactly what this container can see under the bound
# USB mount, plus the kernel mount info for that path (so you can tell a real
# propagated mount from an empty phantom directory). Insert / remove a card and
# WATCH THE CONTAINER LOGS.
#
# It copies nothing and uploads nothing. Safe to run and stop at any time.
# ---------------------------------------------------------------------------
set -u

USB_MOUNT="${USB_MOUNT:-/mnt/usb}"
INTERVAL="${INTERVAL:-3}"

echo "usb-probe: booted $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "usb-probe: watching '${USB_MOUNT}' every ${INTERVAL}s"
echo "usb-probe: for rshared to work, the '${USB_MOUNT}' line below must show a 'shared:' tag:"
grep " ${USB_MOUNT} " /proc/self/mountinfo 2>/dev/null | sed 's/^/    /' \
  || echo "    (no mount at ${USB_MOUNT} yet — Docker may have bound an empty dir; insert a card and re-check)"

while true; do
  echo "--------------------------------------------------------------------"
  echo "[$(date -u '+%H:%M:%S')] ${USB_MOUNT}:"

  # Kernel view: shows propagated child mounts (usbshareX) as they appear.
  minfo="$(grep " ${USB_MOUNT}" /proc/self/mountinfo 2>/dev/null)"
  if [ -n "$minfo" ]; then
    echo "  mounts seen by kernel:"
    printf '%s\n' "$minfo" | sed 's/^/    /'
  else
    echo "  kernel: no mount under ${USB_MOUNT}"
  fi

  # Userspace view: what files are actually readable right now.
  if [ -d "$USB_MOUNT" ]; then
    contents="$(ls -A "$USB_MOUNT" 2>/dev/null)"
    if [ -z "$contents" ]; then
      echo "  contents: EMPTY (no card visible)"
    else
      echo "  contents: $contents"
      for d in "$USB_MOUNT"/*; do
        [ -d "$d" ] || continue
        n="$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')"
        echo "    $d : ${n} files"
        find "$d" -type f 2>/dev/null | head -3 | sed 's/^/        /'
      done
    fi
  else
    echo "  contents: ${USB_MOUNT} does not exist"
  fi

  sleep "$INTERVAL"
done
