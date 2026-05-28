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


ROOT = Path(__file__).resolve().parent
PORT = 3099
DRIVER_PORT = 4444
OUTPUT = Path("/private/tmp/bluff_reveal_cards.png")


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("Reveal screenshot capture timed out.")


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
    with urllib.request.urlopen(req, timeout=8) as response:
        body = response.read()
    parsed = json.loads(body.decode() or "{}")
    value = parsed.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"WebDriver error: {value}")
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    return parsed


def api(path: str, payload: dict) -> dict:
    return request("POST", f"http://127.0.0.1:{PORT}{path}", payload)


def wd(session_id: str | None, method: str, path: str, payload: dict | None = None) -> dict:
    prefix = f"http://127.0.0.1:{DRIVER_PORT}"
    if session_id:
        prefix += f"/session/{session_id}"
    return request(method, prefix + path, payload)


def execute_script(session_id: str, script: str, args: list | None = None) -> object:
    return wd(session_id, "POST", "/execute/sync", {"script": script, "args": args or []})["value"]


def wait_script(session_id: str, script: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            if execute_script(session_id, script):
                return
        except Exception as error:
            last_error = error
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for browser condition: {last_error}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def setup_bluff() -> tuple[str, str, str, list[str], str]:
    for attempt in range(12):
        created = api("/api/create", {"name": f"P1-{attempt + 1}"})
        code = created["room"]["code"]
        p1_id = created["room"]["you"]["id"]
        joined = api("/api/join", {"code": code, "name": "P2"})
        p2_id = joined["room"]["you"]["id"]
        started = api("/api/start", {"code": code, "playerId": p1_id})["room"]

        hand = started["you"]["hand"]
        required_cards = []
        for rank in ("Wild", "Wild Draw Four"):
            card = next((card for card in hand if card["rank"] == rank), None)
            if card:
                required_cards.append(card)

        if len(required_cards) < 2:
            continue

        chosen_cards = required_cards[:]
        seen_ids = {card["id"] for card in chosen_cards}
        for card in hand:
            if card["id"] in seen_ids:
                continue
            chosen_cards.append(card)
            seen_ids.add(card["id"])
            if len(chosen_cards) >= 6:
                break

        claimed_rank = next(rank for rank in started["ranks"] if any(card["claimRank"] != rank for card in chosen_cards))
        return code, p1_id, p2_id, [card["id"] for card in chosen_cards], claimed_rank

    raise RuntimeError("Could not deal both Wild and Wild Draw Four to the starter.")


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
        code, p1_id, p2_id, card_ids, claimed_rank = setup_bluff()

        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        wd(session_id, "POST", "/url", {"url": f"http://127.0.0.1:{PORT}/"})
        wait_script(session_id, "return document.readyState === 'complete';")
        execute_script(
            session_id,
            """
            localStorage.setItem(arguments[0], JSON.stringify(arguments[1]));
            window.location.href = arguments[2];
            """,
            [
                f"bluff-uno-player:{code}",
                {"code": code, "playerId": p2_id, "name": "P2"},
                f"http://127.0.0.1:{PORT}/?room={code}",
            ],
        )
        wait_script(session_id, "return !!document.querySelector('.game-shell');")
        time.sleep(0.6)

        api("/api/action", {"code": code, "playerId": p1_id, "type": "play", "cardIds": card_ids, "claimedRank": claimed_rank})
        time.sleep(0.25)
        api("/api/action", {"code": code, "playerId": p2_id, "type": "challenge"})
        wait_script(session_id, "return !!document.querySelector('.reveal-showcase .reveal-card');", timeout=5)
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
