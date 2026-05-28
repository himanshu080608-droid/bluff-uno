from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
import urllib.request


ROOT = Path(__file__).resolve().parent
PORT = 3099
DRIVER_PORT = 4444
OUTPUT = Path("/private/tmp/bluff_wild_micro_variants.png")
VARIANTS = [
    ("33x60 r28 x-9.8", -9.8, 33, 60, 28, 80, 50, 17),
    ("31x56 r37 x-9.4", -9.4, 31, 56, 37, 66, 50, 34),
    ("31x56 r37 x-9.8", -9.8, 31, 56, 37, 66, 50, 34),
    ("31x55 r37 x-9.4", -9.4, 31, 55, 37, 66, 50, 34),
]


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


def wild_markup(
    class_name: str,
    clip_id: str,
    rx: int,
    ry: int,
    rotation: int,
    split_top: int,
    split_mid: int,
    split_bottom: int,
) -> str:
    cx, cy = 50, 71
    return f"""
      <svg class="wild-icon {class_name}" viewBox="0 0 100 150" aria-hidden="true" focusable="false">
        <defs>
          <clipPath id="{clip_id}">
            <ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rotation} {cx} {cy})"></ellipse>
          </clipPath>
        </defs>
        <g clip-path="url(#{clip_id})">
          <polygon points="-12,-10 {split_top},-10 {split_mid},{cy} -12,{cy}" fill="var(--red)"></polygon>
          <polygon points="{split_top},-10 112,-10 112,{cy} {split_mid},{cy}" fill="var(--blue)"></polygon>
          <polygon points="-12,{cy} {split_mid},{cy} {split_bottom},160 -12,160" fill="var(--yellow)"></polygon>
          <polygon points="{split_mid},{cy} 112,{cy} 112,160 {split_bottom},160" fill="var(--green)"></polygon>
        </g>
        <ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rotation} {cx} {cy})" fill="none" stroke="#f2f2f2" stroke-width="3"></ellipse>
      </svg>
    """


def test_card(index: int, variant: tuple[str, float, int, int, int, int, int, int]) -> str:
    label, x, rx, ry, rotation, split_top, split_mid, split_bottom = variant
    x_label = f"{x:.2f}px"
    top_corner_markup = wild_markup(
        "wild-corner-mark",
        f"wild-corner-{index}-top-clip",
        rx,
        ry,
        rotation,
        split_top,
        split_mid,
        split_bottom,
    )
    bottom_corner_markup = wild_markup(
        "wild-corner-mark",
        f"wild-corner-{index}-bottom-clip",
        rx,
        ry,
        rotation,
        split_top,
        split_mid,
        split_bottom,
    )
    center_markup = wild_markup(
        f"wild-center center-{index}",
        f"wild-center-{index}-clip",
        rx,
        ry,
        rotation,
        split_top,
        split_mid,
        split_bottom,
    )
    return f"""
      <section class="variant">
        <button class="uno-card color-wild rank-wild">
          <span class="card-corner icon-corner wild-corner-wrap top">{top_corner_markup}</span>
          <span class="card-face action-face">
            <span class="action-symbol wild-symbol" aria-label="Wild" style="transform:translate({x_label}, -5px)">
              {center_markup}
            </span>
          </span>
          <span class="card-corner icon-corner wild-corner-wrap bottom">{bottom_corner_markup}</span>
        </button>
        <span>{label}</span>
      </section>
    """


def comparison_html() -> str:
    cards = "\n".join(test_card(index, variant) for index, variant in enumerate(VARIANTS))
    return f"""<!doctype html>
      <html>
        <head>
          <meta charset="utf-8">
          <link rel="stylesheet" href="http://127.0.0.1:{PORT}/styles.css">
          <style>
            body {{ margin: 0; background: #d8d5cd; }}
            main {{
              min-height: 100vh;
              display: flex;
              gap: 20px;
              align-items: center;
              justify-content: center;
              background: #d8d5cd;
              padding: 40px;
              font: 900 18px system-ui, sans-serif;
              color: #111;
            }}
            .variant {{
              display: grid;
              gap: 80px;
              justify-items: center;
              width: 108px;
            }}
            .variant .uno-card {{
              transform: scale(1.32);
              transform-origin: center;
            }}
          </style>
        </head>
        <body>
          <main>{cards}</main>
        </body>
      </html>"""


def main() -> int:
    if not port_free(PORT):
        raise RuntimeError(f"Port {PORT} is already in use.")
    if not port_free(DRIVER_PORT):
        raise RuntimeError(f"Port {DRIVER_PORT} is already in use.")

    env = os.environ.copy()
    env["PORT"] = str(PORT)
    server = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    driver = subprocess.Popen(
        ["safaridriver", "-p", str(DRIVER_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session_id: str | None = None

    try:
        wait_http(f"http://127.0.0.1:{PORT}/")
        wait_http(f"http://127.0.0.1:{DRIVER_PORT}/status")
        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        url = "data:text/html;charset=utf-8," + quote(comparison_html())
        wd(session_id, "POST", "/url", {"url": url})
        time.sleep(0.8)
        screenshot = wd(session_id, "GET", "/screenshot")["value"]
        OUTPUT.write_bytes(base64.b64decode(screenshot))
        print(OUTPUT)
        return 0
    finally:
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
