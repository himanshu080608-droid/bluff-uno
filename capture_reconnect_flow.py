from __future__ import annotations

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
HOST = "localhost"


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("Reconnect flow check timed out.")


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
        wait_http(f"http://{HOST}:{PORT}/")
        wait_http(f"http://127.0.0.1:{DRIVER_PORT}/status")

        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/"})
        wait_script(session_id, "return document.readyState === 'complete' && !!document.querySelector('#create');")
        execute_script(
            session_id,
            """
            localStorage.clear();
            document.cookie = "bluffUnoLastSession=; max-age=0; path=/; SameSite=Lax";
            document.cookie.split(";").forEach((part) => {
              const name = part.trim().split("=")[0];
              if (name.startsWith("bluffUnoPlayer_")) {
                document.cookie = `${name}=; max-age=0; path=/; SameSite=Lax`;
              }
            });
            """,
        )

        execute_script(
            session_id,
            """
            document.querySelector("#name").value = "Reconnect Tester";
            document.querySelector("#create").click();
            """,
        )
        wait_script(session_id, "return !!document.querySelector('.lobby-list, .game-shell');")
        first = execute_script(
            session_id,
            """
            return {
              href: location.href,
              last: localStorage.getItem("bluff-uno-last-session"),
              keys: Object.keys(localStorage).sort(),
              cookie: document.cookie
            };
            """,
        )

        wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/"})
        wait_script(session_id, "return document.readyState === 'complete';")
        wait_script(session_id, "return !!document.querySelector('#reconnect');")
        home = execute_script(
            session_id,
            """
            const button = document.querySelector("#reconnect");
            return {
              href: location.href,
              disabled: button.disabled,
              createDisabled: document.querySelector("#create").disabled,
              joinDisabled: document.querySelector("#join").disabled,
              codeDisabled: document.querySelector("#code").disabled,
              text: button.textContent.trim(),
              last: localStorage.getItem("bluff-uno-last-session"),
              keys: Object.keys(localStorage).sort(),
              cookie: document.cookie
            };
            """,
        )
        if home["disabled"]:
            print(json.dumps({"afterCreate": first, "afterPlainHome": home}, indent=2))
            raise RuntimeError("Reconnect button is disabled on the plain home page.")

        execute_script(session_id, "document.querySelector('#reconnect').click();")
        wait_script(session_id, "return !!document.querySelector('.lobby-list, .game-shell');")
        reconnected = execute_script(
            session_id,
            """
            return {
              href: location.href,
              title: document.querySelector(".brand, .room-title")?.textContent.trim() || "",
              body: document.body.innerText.slice(0, 160)
            };
            """,
        )

        print(json.dumps({"afterCreate": first, "afterPlainHome": home, "afterReconnect": reconnected}, indent=2))
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
