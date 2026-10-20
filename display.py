"""Makes sure a virtual screen exists. start.sh normally starts it before the app."""

import os
import shutil
import subprocess
import time
from pathlib import Path

DISPLAY = os.environ.get("XVFB_DISPLAY", ":99")
_proc = None


def ensure_display():
    global _proc
    os.environ.setdefault("DISPLAY", DISPLAY)
    socket_path = Path(f"/tmp/.X11-unix/X{DISPLAY.lstrip(':')}")
    if socket_path.exists():
        return

    if shutil.which("Xvfb") is None:
        raise RuntimeError("Xvfb is not installed")

    _proc = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if socket_path.exists():
            break
        time.sleep(0.5)
    os.environ["DISPLAY"] = DISPLAY
    print(f"[display] Xvfb started on {DISPLAY}", flush=True)
