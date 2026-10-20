#!/bin/sh
# Extension-active runner.
#  - Keeps a virtual screen (Xvfb) running, restarts it if it dies.
#  - Starts keeper.sh, which keeps a fresh stream link ready.
#  - Runs the app and restarts it automatically if it crashes.
set -u

DISPLAY_NUM=99
DISP=":${DISPLAY_NUM}"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"
export DISPLAY="$DISP"
export PYTHONUNBUFFERED=1

start_xvfb() {
  if [ -S "$SOCKET" ]; then
    return 0
  fi
  rm -f "$SOCKET" "/tmp/.X${DISPLAY_NUM}-lock"
  Xvfb "$DISP" -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  i=0
  while [ ! -S "$SOCKET" ] && [ "$i" -lt 40 ]; do
    sleep 0.5
    i=$((i + 1))
  done
  echo "[ext] Xvfb ready on $DISP"
}

echo "[ext] extensions folder:"
ls /opt/extensions 2>/dev/null || echo "[ext] no extensions folder found"

# Keeper runs in the background for the whole life of the container.
sh /app/keeper.sh &

while true; do
  start_xvfb
  rm -rf /tmp/vle-* 2>/dev/null
  echo "[ext] starting app"
  python app.py
  code=$?
  echo "[ext] app exited with code $code, restarting in 3 seconds"
  sleep 3
done
