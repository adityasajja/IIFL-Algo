"""Time `atr serve` from launch to the first response, then watch it stay responsive.

    .venv/Scripts/python scripts/time_startup.py [port] [seconds-to-watch]

The second half matters as much as the first: a blocking call on the event loop
(a hung Telegram post, a slow file read) shows up as a request that takes seconds
once the server is "ready".
"""

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
exe = ROOT / ".venv" / "Scripts" / "atr.exe"
port = sys.argv[1] if len(sys.argv) > 1 else "8012"
watch = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
t = time.time()
proc = subprocess.Popen(
    [str(exe), "serve", "--no-browser", "--port", port],
    cwd=str(ROOT),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
url = f"http://127.0.0.1:{port}/"
try:
    while time.time() - t < 90:
        try:
            urllib.request.urlopen(url, timeout=1)
            print(f"ready after {time.time() - t:.1f}s")
            break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    else:
        print("not ready within 90s")
        raise SystemExit(1)

    worst = 0.0
    end = time.time() + watch
    n = 0
    while time.time() < end:
        s = time.time()
        try:
            urllib.request.urlopen(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print("request failed:", exc)
        worst = max(worst, time.time() - s)
        n += 1
        time.sleep(0.25)
    print(f"{n} requests over {watch:.0f}s, slowest {worst * 1000:.0f} ms")
finally:
    proc.terminate()
