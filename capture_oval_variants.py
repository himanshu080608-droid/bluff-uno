from __future__ import annotations

import base64
import json
import os
import signal
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
OUTPUT = Path("/private/tmp/bluff_oval_variants.png")
VARIANTS = [
    ("60x102 r29", 60, 102, 29),
    ("68x104 r39", 68, 104, 39),
    ("69x104 r39", 69, 104, 39),
]


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("Oval screenshot capture timed out.")

REVERSE_ARROW_PATH = "M -36,11 L 8,11 L 8,24 L 40,0 L 8,-24 L 8,-11 C -25,-11 -36,-11 -36,11 Z"
REVERSE_JCURVE_PATH = "M 8,-11 C -25,-11 -36,-11 -36,11"
REVERSE_ARROWHEAD_PATH = "M 8,24 L 40,0 L 8,-24"
REVERSE_INNER_WINGS_PATH = "M 8,-24 L 8,-11 M 8,11 L 8,24"


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


def j_curve_point(t: float) -> tuple[float, float]:
    return 8 - 99 * t + 66 * t * t - 11 * t * t * t, -11 + 22 * t * t * t


def tapered_j_curve_markup(max_width: int) -> str:
    segments = []
    count = 16
    for index in range(count):
        t0 = index / count
        t1 = (index + 1) / count
        x0, y0 = j_curve_point(t0)
        x1, y1 = j_curve_point(t1)
        segments.append(
            f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="black" '
            f'stroke-width="{t1 * max_width}" stroke-linecap="round"></line>'
        )
    return "".join(segments)


def reverse_icon(class_name: str, prefix: str) -> str:
    inner_clip_id = f"{prefix}-reverse-inner"
    head_clip_id = f"{prefix}-reverse-head"
    arrow_one = "translate(6.2, -21.8) rotate(-45)"
    arrow_two = "translate(-6.2, 21.8) rotate(135)"
    return f"""
      <svg class="reverse-icon {class_name}" viewBox="-72 -72 144 144" aria-hidden="true" focusable="false">
        <defs>
          <clipPath id="{inner_clip_id}">
            <polygon points="-100,-100 8,-24 40,0 8,24 -100,100"></polygon>
          </clipPath>
          <clipPath id="{head_clip_id}">
            <polygon points="8,-24 100,-24 100,24 8,24"></polygon>
          </clipPath>
        </defs>
        <g transform="{arrow_two}">
          <path d="{REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="9.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#{head_clip_id})"></path>
        </g>
        <g transform="{arrow_one}">
          {tapered_j_curve_markup(12)}
          <path d="{REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="9.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#{inner_clip_id})"></path>
        </g>
        <g transform="{arrow_two}">
          <path d="{REVERSE_ARROW_PATH}" fill="white" stroke="none"></path>
          <path d="M -36,11 L 8,11" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
          <path d="{REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#{head_clip_id})"></path>
          <path d="{REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="butt"></path>
          <path d="{REVERSE_JCURVE_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linecap="round"></path>
        </g>
        <g transform="{arrow_one}">
          <path d="{REVERSE_ARROW_PATH}" fill="white" stroke="none"></path>
          <path d="M -36,11 L 8,11" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
          <path d="{REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="butt"></path>
          <path d="{REVERSE_JCURVE_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
          <path d="{REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linejoin="miter" stroke-linecap="butt"></path>
        </g>
      </svg>
    """


def skip_icon(class_name: str) -> str:
    return f"""
      <svg class="skip-icon {class_name}" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
        <g class="skip-shadow" transform="translate(-6 6)">
          <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
          <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
        </g>
        <g class="skip-shadow" transform="translate(-3 3)">
          <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
          <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
        </g>
        <g class="skip-outline">
          <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
          <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
        </g>
        <g class="skip-fill">
          <path class="skip-ring" d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
          <path class="skip-bar" d="M26 66 L66 26 L75 35 L35 75 Z"></path>
        </g>
      </svg>
    """


def number_card() -> str:
    return f"""
      <button class="uno-card color-red rank-5" style="--card-color: var(--red)">
        <span class="card-corner top">5</span>
        <span class="card-face">5</span>
        <span class="card-corner bottom">5</span>
      </button>
    """


def skip_card(index: int) -> str:
    return f"""
      <button class="uno-card color-yellow rank-skip" style="--card-color: var(--yellow)">
        <span class="card-corner icon-corner top">{skip_icon("skip-corner")}</span>
        <span class="card-face action-face">
          <span class="action-symbol skip-symbol" aria-label="Skip">{skip_icon("skip-center")}</span>
        </span>
        <span class="card-corner icon-corner bottom">{skip_icon("skip-corner")}</span>
      </button>
    """


def reverse_card(index: int) -> str:
    return f"""
      <button class="uno-card color-green rank-reverse" style="--card-color: var(--green)">
        <span class="card-corner icon-corner top">{reverse_icon("reverse-corner", f"reverse-{index}-top")}</span>
        <span class="card-face action-face">
          <span class="action-symbol reverse-symbol" aria-label="Reverse">{reverse_icon("reverse-center", f"reverse-{index}-center")}</span>
        </span>
        <span class="card-corner icon-corner bottom">{reverse_icon("reverse-corner", f"reverse-{index}-bottom")}</span>
      </button>
    """


def draw_card() -> str:
    return f"""
      <button class="uno-card color-blue rank-draw-two" style="--card-color: var(--blue)">
        <span class="card-corner text-corner top">+2</span>
        <span class="card-face action-face">
          <span class="action-symbol draw-symbol draw-two-symbol" aria-label="Draw Two">
            <span class="mini-card one"></span><span class="mini-card two"></span>
          </span>
        </span>
        <span class="card-corner text-corner bottom">+2</span>
      </button>
    """


def wild_draw_four_card() -> str:
    return f"""
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
    """


def comparison_html() -> str:
    sections = []
    for index, (label, width, height, rotation) in enumerate(VARIANTS):
        sections.append(
            f"""
              <section class="variant variant-{index}">
                <div class="card-row">
                  {number_card()}
                  {skip_card(index)}
                  {reverse_card(index)}
                  {draw_card()}
                  {wild_draw_four_card()}
                </div>
                <span class="variant-label">{label}</span>
              </section>
            """
        )
    overrides = "\n".join(
        f"""
          .variant-{index} .uno-card::before {{
            width: {width}px;
            height: {height}px;
            transform: translate(-50%, -50%) rotate({rotation}deg);
          }}
          .variant-{index} .uno-card.rank-wild-draw-four::before {{
            width: 60.7px;
            height: 111.2px;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%) rotate(28deg);
          }}
        """
        for index, (_, width, height, rotation) in enumerate(VARIANTS)
    )
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
              gap: 24px;
              align-items: center;
              justify-content: center;
              background: #d8d5cd;
              padding: 28px;
              font: 900 13px system-ui, sans-serif;
              color: #111;
            }}
            .card-row {{
              display: flex;
              gap: 12px;
              align-items: center;
              transform: scale(1);
              transform-origin: center;
            }}
            .variant {{
              position: relative;
              min-width: 470px;
              display: grid;
              justify-content: center;
              justify-items: center;
              gap: 36px;
            }}
            .variant-label {{
              font-size: 18px;
              white-space: nowrap;
            }}
            {overrides}
          </style>
        </head>
        <body><main>{"".join(sections)}</main></body>
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
