from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
PORT = 3099
DRIVER_PORT = 4444
OUTPUT = Path("/private/tmp/bluff_plus_four_alignment.png")


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("Plus-four alignment capture timed out.")


def wait_http(url: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.6) as response:
                if response.status < 500:
                    return
        except Exception as error:
            last_error = error
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def request(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        body = response.read()
    parsed = json.loads(body.decode() or "{}")
    value = parsed.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"WebDriver error: {value}")
    return parsed


def wd(session_id: str | None, method: str, path: str, payload: dict | None = None) -> dict:
    prefix = f"http://127.0.0.1:{DRIVER_PORT}"
    if session_id:
        prefix += f"/session/{session_id}"
    return request(method, prefix + path, payload)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wild_icon() -> str:
    return """
      <svg class="wild-icon wild-center" viewBox="0 0 100 150" aria-hidden="true" focusable="false">
        <defs>
          <clipPath id="alignment-wild-clip">
            <ellipse cx="50" cy="71" rx="33" ry="60" transform="rotate(28 50 71)"></ellipse>
          </clipPath>
        </defs>
        <g clip-path="url(#alignment-wild-clip)">
          <polygon points="-12,-10 80,-10 50,71 -12,71" fill="var(--red)"></polygon>
          <polygon points="80,-10 112,-10 112,71 50,71" fill="var(--blue)"></polygon>
          <polygon points="-12,71 50,71 17,160 -12,160" fill="var(--yellow)"></polygon>
          <polygon points="50,71 112,71 112,160 17,160" fill="var(--green)"></polygon>
        </g>
        <ellipse cx="50" cy="71" rx="33" ry="60" transform="rotate(28 50 71)" fill="none" stroke="#f2f2f2" stroke-width="3"></ellipse>
      </svg>
    """


def comparison_html() -> str:
    return f"""<!doctype html>
      <html>
        <head>
          <meta charset="utf-8">
          <link rel="stylesheet" href="http://127.0.0.1:{PORT}/styles.css">
          <style>
            body {{ margin: 0; background: #d8d5cd; }}
            main {{
              min-height: 100vh;
              display: grid;
              place-items: center;
              background: #d8d5cd;
            }}
            .card-row {{
              display: flex;
              gap: 54px;
              align-items: center;
              transform: scale(2.6);
              transform-origin: center;
            }}
          </style>
        </head>
        <body>
          <main>
            <div class="card-row">
              <button class="uno-card color-wild rank-wild" style="--card-color: var(--wild)">
                <span class="card-face action-face">
                  <span class="action-symbol wild-symbol" aria-label="Wild">{wild_icon()}</span>
                </span>
              </button>
              <button class="uno-card color-wild rank-wild-draw-four" style="--card-color: var(--wild)">
                <span class="card-corner text-corner top">+4</span>
                <span class="card-face action-face">
                  <span class="action-symbol wild-draw-symbol" aria-label="Wild Draw Four">
                    <span class="mini-card green"></span><span class="mini-card blue"></span>
                    <span class="mini-card red"></span><span class="mini-card yellow"></span>
                  </span>
                </span>
                <span class="card-corner text-corner bottom">+4</span>
              </button>
            </div>
          </main>
        </body>
      </html>"""


def main() -> int:
    signal.signal(signal.SIGALRM, stop_capture)
    signal.alarm(28)

    if not port_free(PORT):
        raise RuntimeError(f"Port {PORT} is already in use.")
    if not port_free(DRIVER_PORT):
        raise RuntimeError(f"Port {DRIVER_PORT} is already in use.")

    env = os.environ.copy()
    env["PORT"] = str(PORT)
    server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    driver = subprocess.Popen(["safaridriver", "-p", str(DRIVER_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session_id: str | None = None

    try:
        wait_http(f"http://127.0.0.1:{PORT}/")
        wait_http(f"http://127.0.0.1:{DRIVER_PORT}/status")
        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        wd(session_id, "POST", "/url", {"url": "data:text/html;charset=utf-8," + quote(comparison_html())})
        time.sleep(0.8)
        screenshot = wd(session_id, "GET", "/screenshot")["value"]
        OUTPUT.write_bytes(base64.b64decode(screenshot))
        print(OUTPUT)
        return 0
    finally:
        signal.alarm(0)
        if session_id:
            try:
                wd(session_id, "DELETE", "")
            except Exception:
                pass
        for process in (driver, server):
            process.terminate()
        for process in (driver, server):
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
