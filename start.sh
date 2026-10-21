#!/bin/sh
# Starts a virtual screen (Xvfb) for Chromium, waits until it is ready,
# starts the keeper (keeps a fresh link ready), then runs the app.
# If the app crashes, it restarts after 2 seconds.

echo "Starting Stream Capture..."

if [ ! -S /tmp/.X11-unix/X99 ]; then
  Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  i=0
  while [ ! -S /tmp/.X11-unix/X99 ] && [ "$i" -lt 40 ]; do
    sleep 0.5
    i=$((i + 1))
  done
fi

if [ ! -S /tmp/.X11-unix/X99 ]; then
  echo "Xvfb did not start. See /tmp/xvfb.log"
  cat /tmp/xvfb.log
fi

export DISPLAY=:99
echo "DISPLAY=$DISPLAY"

# Keeper: refreshes the stream link before it expires. Waits for the app itself.
sh /app/keeper.sh &

while true; do
  python app.py
  echo "Server stopped. Restarting in 2 seconds..."
  sleep 2
done
