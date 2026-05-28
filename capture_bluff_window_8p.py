from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PORT = 3099
DRIVER_PORT = 4444
HOST = "localhost"


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("8-player bluff window check timed out.")


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
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            parsed_error = json.loads(body.decode() or "{}")
        except Exception:
            parsed_error = {}
        raise RuntimeError(parsed_error.get("error") or f"HTTP {error.code}") from error
    parsed = json.loads(body.decode() or "{}")
    value = parsed.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"WebDriver error: {value}")
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    return parsed


def api(path: str, payload: dict) -> dict:
    return request("POST", f"http://{HOST}:{PORT}{path}", payload)


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
        time.sleep(0.12)
    raise RuntimeError(f"Timed out waiting for browser condition: {last_error}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def setup_room() -> dict:
    created = api("/api/create", {"name": "p1"})["room"]
    code = created["code"]
    players = [{"name": "p1", "id": created["you"]["id"]}]
    for index in range(2, 9):
        joined = api("/api/join", {"code": code, "name": f"p{index}"})["room"]
        players.append({"name": f"p{index}", "id": joined["you"]["id"]})
    started = api("/api/start", {"code": code, "playerId": players[0]["id"]})["room"]
    order_names = [player["name"] for player in started["players"]]
    return {"code": code, "players": players, "room": started, "order": order_names}


def main() -> int:
    signal.signal(signal.SIGALRM, stop_capture)
    signal.alarm(34)

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
        wait_http(f"http://{HOST}:{PORT}/")
        wait_http(f"http://127.0.0.1:{DRIVER_PORT}/status")

        room_data = setup_room()
        code = room_data["code"]
        starter_id = room_data["players"][0]["id"]
        state = room_data["room"]
        viewer_id = state["players"][1]["id"]
        viewer_name = state["players"][1]["name"]
        p1_state = api("/api/state", {"code": code, "playerId": starter_id})["room"]
        starter_card = p1_state["you"]["hand"][0]

        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/"})
        wait_script(session_id, "return document.readyState === 'complete';")
        execute_script(
            session_id,
            """
            const [code, playerId, name] = arguments;
            const saved = { code, playerId, name, updatedAt: Date.now() };
            localStorage.clear();
            localStorage.setItem(`bluff-uno-player:${code}`, JSON.stringify(saved));
            localStorage.setItem("bluff-uno-last-session", JSON.stringify(saved));
            document.cookie = `bluffUnoPlayer_${code}=${encodeURIComponent(JSON.stringify(saved))}; max-age=2592000; path=/; SameSite=Lax`;
            document.cookie = `bluffUnoLastSession=${encodeURIComponent(JSON.stringify(saved))}; max-age=2592000; path=/; SameSite=Lax`;
            """,
            [code, viewer_id, viewer_name],
        )
        wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/?room={code}"})
        wait_script(session_id, "return !!document.querySelector('.game-shell');")

        api(
            "/api/action",
            {
                "code": code,
                "playerId": starter_id,
                "type": "play",
                "cardIds": [starter_card["id"]],
                "claimedRank": starter_card["claimRank"],
            },
        )
        wait_script(session_id, "return !!document.querySelector('.reaction-status');", timeout=3.0)
        during = execute_script(
            session_id,
            """
            const splash = document.querySelector(".action-splash");
            const rank = document.querySelector("#rank");
            const pass = document.querySelector("#pass");
            const challenge = document.querySelector("#challenge");
            return {
              title: document.querySelector(".turn-pill")?.textContent.trim(),
              reaction: document.querySelector(".reaction-status")?.textContent.trim(),
              rankDisabled: rank?.disabled,
              passDisabled: pass?.disabled,
              challengeDisabled: challenge?.disabled,
              incomingCards: document.querySelectorAll(".pile-card.incoming").length,
              splashText: splash?.textContent.trim() || "",
              splashDelay: splash ? getComputedStyle(splash).animationDelay : "",
              splashDuration: splash ? getComputedStyle(splash).animationDuration : ""
            };
            """,
        )

        try:
            next_state = api("/api/state", {"code": code, "playerId": viewer_id})["room"]
            viewer_card = next_state["you"]["hand"][0]
            api(
                "/api/action",
                {
                    "code": code,
                    "playerId": viewer_id,
                    "type": "play",
                    "cardIds": [viewer_card["id"]],
                    "claimedRank": next_state["activeRank"],
                },
            )
            fast_play_error = "accepted unexpectedly"
        except Exception as error:
            fast_play_error = str(error)

        wait_script(session_id, "return !document.querySelector('.reaction-status');", timeout=5.0)
        after = execute_script(
            session_id,
            """
            const firstCard = document.querySelector(".hand .uno-card");
            if (firstCard) firstCard.click();
            return {
              title: document.querySelector(".turn-pill")?.textContent.trim(),
              rankDisabled: document.querySelector("#rank")?.disabled,
              passDisabled: document.querySelector("#pass")?.disabled,
              playDisabledAfterSelect: document.querySelector("#play")?.disabled,
              challengeDisabled: document.querySelector("#challenge")?.disabled,
              selected: document.querySelectorAll(".uno-card.selected").length
            };
            """,
        )

        print(
            json.dumps(
                {
                    "room": code,
                    "players": 8,
                    "order": room_data["order"],
                    "starter": "p1",
                    "viewer": viewer_name,
                    "duringWindow": during,
                    "fastNextPlay": fast_play_error,
                    "afterWindow": after,
                },
                indent=2,
            )
        )
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
