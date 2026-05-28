from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PORT = 3099
DEBUG_PORT = 9222
HOST = "localhost"
BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
PLAYER_COUNT = 8
BLUFF_WINDOW_SECONDS = 10
SCREENSHOT = Path("/private/tmp/bluff_brave_incognito_8tab_stress.png")


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("Brave incognito 8-tab stress check timed out.")


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


def request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        raise RuntimeError(body.decode(errors="replace") or f"HTTP {error.code}") from error
    return json.loads(body.decode() or "{}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def read_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("WebSocket closed.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class WebSocket:
    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=8)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket handshake failed: {response[:120]!r}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in response:
            raise RuntimeError("WebSocket accept header did not match.")

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(payload)))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self, timeout: float = 8.0) -> str:
        self.sock.settimeout(timeout)
        while True:
            first, second = read_exact(self.sock, 2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", read_exact(self.sock, 8))[0]
            masked = bool(second & 0x80)
            mask = read_exact(self.sock, 4) if masked else b""
            payload = read_exact(self.sock, length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by browser.")
            if opcode == 0x9:
                continue
            if opcode == 0x1:
                return payload.decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class CDPPage:
    def __init__(self, websocket_url: str):
        self.ws = WebSocket(websocket_url)
        self.next_id = 1

    def send(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict:
        message_id = self.next_id
        self.next_id += 1
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv_text(max(0.2, deadline - time.time())))
            if data.get("id") != message_id:
                continue
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result", {})
        raise RuntimeError(f"Timed out waiting for CDP method {method}.")

    def eval(self, script: str, args: list | None = None, timeout: float = 8.0):
        arglist = ", ".join(json.dumps(arg) for arg in (args or []))
        expression = f"(function(){{ return (function(){{ {script} }}).apply(null, [{arglist}]); }})()"
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        value = result.get("result", {})
        return value.get("value")

    def wait(self, script: str, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                if self.eval(script, timeout=1.0):
                    return
            except Exception as error:
                last_error = error
            time.sleep(0.12)
        raise RuntimeError(f"Timed out waiting for browser condition: {last_error}")

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait("return document.readyState === 'complete';")

    def screenshot(self, path: Path) -> None:
        data = self.send("Page.captureScreenshot", {"format": "png"}, timeout=8).get("data", "")
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        self.ws.close()


def new_page(browser: CDPPage, url: str) -> CDPPage:
    target_id = browser.send("Target.createTarget", {"url": url}, timeout=10)["targetId"]
    target = None
    deadline = time.time() + 8
    while time.time() < deadline:
        targets = request_json("GET", f"http://127.0.0.1:{DEBUG_PORT}/json/list")
        target = next((item for item in targets if item.get("id") == target_id), None)
        if target and target.get("webSocketDebuggerUrl"):
            break
        time.sleep(0.12)
    if not target or not target.get("webSocketDebuggerUrl"):
        raise RuntimeError(f"Could not attach to created tab {target_id}.")
    page = CDPPage(target["webSocketDebuggerUrl"])
    page.send("Page.enable")
    page.send("Runtime.enable")
    page.wait("return document.readyState === 'complete';")
    return page


def connect_page(target: dict) -> CDPPage:
    page = CDPPage(target["webSocketDebuggerUrl"])
    page.send("Page.enable")
    page.send("Runtime.enable")
    page.wait("return document.readyState === 'complete';")
    return page


def collect_app_pages(count: int) -> list[CDPPage]:
    deadline = time.time() + 12
    targets = []
    while time.time() < deadline:
        all_targets = request_json("GET", f"http://127.0.0.1:{DEBUG_PORT}/json/list")
        targets = [
            target
            for target in all_targets
            if target.get("type") == "page"
            and target.get("webSocketDebuggerUrl")
            and target.get("url", "").startswith(f"http://{HOST}:{PORT}/")
        ]
        if len(targets) >= count:
            break
        time.sleep(0.2)
    if len(targets) < count:
        raise RuntimeError(f"Expected {count} app tabs, found {len(targets)}: {targets}")
    return [connect_page(target) for target in targets[:count]]


def reset_home(page: CDPPage) -> None:
    try:
        page.wait("return document.readyState === 'complete' && !!document.querySelector('#create');", timeout=3)
    except Exception:
        page.eval("location.href = arguments[0]; return true;", [f"http://{HOST}:{PORT}/"])
        try:
            page.wait("return document.readyState === 'complete' && !!document.querySelector('#create');", timeout=8)
        except Exception as error:
            details = page.eval(
                """
                return {
                  url: location.href,
                  ready: document.readyState,
                  title: document.title,
                  text: document.body ? document.body.innerText.slice(0, 500) : "",
                  html: document.documentElement ? document.documentElement.outerHTML.slice(0, 500) : ""
                };
                """
            )
            raise RuntimeError(f"Home controls not found: {details}") from error
    page.eval(
        """
        localStorage.clear();
        document.cookie.split(";").forEach((part) => {
          const name = part.trim().split("=")[0];
          if (name) document.cookie = `${name}=; max-age=0; path=/; SameSite=Lax`;
        });
        if (document.querySelector("#clearSessions")) {
          clearAllStoredSessions();
          render();
        }
        return true;
        """
    )
    page.wait("return !!document.querySelector('#join') && !document.querySelector('#join').disabled;")


def state(page: CDPPage) -> dict:
    value = page.eval(
        """
        try {
          return JSON.parse(JSON.stringify(state));
        } catch (error) {
          return null;
        }
        """
    )
    if not isinstance(value, dict):
        raise RuntimeError("Could not read client state.")
    return value


def fill_and_click(page: CDPPage, field: str, value: str, button: str) -> None:
    page.eval(
        """
        const [field, value, button] = arguments;
        const input = document.querySelector(field);
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        document.querySelector(button).click();
        return true;
        """,
        [field, value, button],
    )


def create_room(page: CDPPage) -> str:
    reset_home(page)
    fill_and_click(page, "#name", "p1", "#create")
    page.wait("return !!document.querySelector('.lobby-list');")
    return state(page)["code"]


def join_room(page: CDPPage, code: str, name: str) -> None:
    reset_home(page)
    fill_and_click(page, "#code", code, "#join")
    page.wait("return !!document.querySelector('#joinName');")
    fill_and_click(page, "#joinName", name, "#confirmJoin")
    try:
        page.wait("return !!document.querySelector('.lobby-list');", timeout=12)
    except Exception as error:
        if page.eval("return !!document.querySelector('#confirmJoin') && !document.querySelector('.error.visible');"):
            fill_and_click(page, "#joinName", name, "#confirmJoin")
            page.wait("return !!document.querySelector('.lobby-list');", timeout=12)
            return
        details = page.eval(
            """
            return {
              url: location.href,
              ready: document.readyState,
              text: document.body ? document.body.innerText.slice(0, 700) : "",
              error: document.querySelector(".error")?.textContent.trim() || "",
              hasJoinName: !!document.querySelector("#joinName"),
              hasLobby: !!document.querySelector(".lobby-list")
            };
            """
        )
        raise RuntimeError(f"Join failed for {name}: {details}") from error


def snapshot(page: CDPPage) -> dict:
    return page.eval(
        """
        const splash = document.querySelector(".action-splash");
        return {
          title: document.querySelector(".turn-pill")?.textContent.trim() || "",
          reaction: document.querySelector(".reaction-status")?.textContent.trim() || "",
          playDisabled: document.querySelector("#play")?.disabled ?? null,
          passDisabled: document.querySelector("#pass")?.disabled ?? null,
          challengeDisabled: document.querySelector("#challenge")?.disabled ?? null,
          incomingCards: document.querySelectorAll(".pile-card.incoming").length,
          splashText: splash?.textContent.trim() || "",
          splashDelay: splash ? getComputedStyle(splash).animationDelay : "",
          splashDuration: splash ? getComputedStyle(splash).animationDuration : "",
          logs: [...document.querySelectorAll(".log-item .log-text")].slice(0, 6).map((item) => item.textContent.trim()),
          selected: document.querySelectorAll(".uno-card.selected").length
        };
        """
    )


def choose_play(page: CDPPage, truthful: bool, count: int = 1) -> dict:
    return page.eval(
        """
        const [truthful, count] = arguments;
        [...document.querySelectorAll(".hand .uno-card")].slice(0, count).forEach((card) => card.click());
        const hand = state.you.hand;
        let claim = state.activeRank || hand[0].claimRank;
        if (!state.activeRank && !truthful) {
          claim = state.ranks.find((item) => item !== hand[0].claimRank) || hand[0].claimRank;
        }
        const rank = document.querySelector("#rank");
        rank.value = claim;
        rank.dispatchEvent(new Event("change", { bubbles: true }));
        return {
          claim,
          actual: hand.slice(0, count).map((card) => card.claimRank),
          playDisabled: document.querySelector("#play").disabled,
          selected: document.querySelectorAll(".uno-card.selected").length
        };
        """,
        [truthful, count],
    )


def current_page_index(pages: list[CDPPage], source: CDPPage | None = None) -> int:
    current_id = state(source or pages[0])["currentPlayerId"]
    for index, page in enumerate(pages):
        if state(page)["you"]["id"] == current_id:
            return index
    raise RuntimeError("Could not find current player's tab.")


def non_actor_index(pages: list[CDPPage], actor_id: str, start: int = 0) -> int:
    for offset in range(len(pages)):
        index = (start + offset) % len(pages)
        page_state = state(pages[index])
        winner_ids = {winner["id"] for winner in page_state["winners"]}
        if page_state["you"]["id"] != actor_id and page_state["you"]["id"] not in winner_ids:
            return index
    raise RuntimeError("Could not find non-actor tab.")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    signal.signal(signal.SIGALRM, stop_capture)
    signal.alarm(100)

    if not Path(BRAVE).exists():
        raise RuntimeError("Brave Browser was not found.")
    if not port_free(PORT):
        raise RuntimeError(f"Port {PORT} is already in use.")
    if not port_free(DEBUG_PORT):
        raise RuntimeError(f"Port {DEBUG_PORT} is already in use.")

    env = os.environ.copy()
    env["PORT"] = str(PORT)
    server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pages: list[CDPPage] = []

    with tempfile.TemporaryDirectory(prefix="bluff-brave-profile-", dir="/private/tmp") as profile:
        app_urls = [f"http://{HOST}:{PORT}/" for _ in range(PLAYER_COUNT)]
        brave = subprocess.Popen(
            [
                BRAVE,
                "--incognito",
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                *app_urls,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_http(f"http://{HOST}:{PORT}/")
            wait_http(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=10)

            pages.extend(collect_app_pages(PLAYER_COUNT))
            first = pages[0]
            code = create_room(first)
            summary: dict = {"browser": "Brave incognito", "room": code, "players": PLAYER_COUNT, "events": []}
            summary["events"].append({"step": "create", "room": code})

            for number in range(2, PLAYER_COUNT + 1):
                page = pages[number - 1]
                join_room(page, code, f"p{number}")
                summary["events"].append({"step": "join", "player": f"p{number}"})

            first.wait("return state && state.players && state.players.length === 8;")
            first.eval("document.querySelector('#start').click(); return true;")
            first.wait("return !!document.querySelector('.game-shell') && state.status === 'playing';")
            order = [player["name"] for player in state(first)["players"]]
            summary["order"] = order
            summary["events"].append({"step": "start", "order": order})

            first.wait("return state && state.canAct === true;")
            bluff_choice = choose_play(first, truthful=False)
            first.eval("document.querySelector('#play').click(); return true;")
            first.wait("return !!document.querySelector('.reaction-status') && !!document.querySelector('.action-splash');", timeout=3)
            during_window = snapshot(first)
            assert_true("10s" in during_window["reaction"], "Reaction window did not show 10s.")
            assert_true(during_window["incomingCards"] >= 1, "Pile throw animation did not mark incoming cards.")
            summary["events"].append({"step": "bluff-play", "choice": bluff_choice, "snapshot": during_window})

            next_index = current_page_index(pages, first)
            next_page = pages[next_index]
            next_page.wait("return state && state.status === 'playing' && state.you && state.you.hand && state.you.hand.length > 0;", timeout=5)
            choose_play(next_page, truthful=True)
            blocked = snapshot(next_page)
            next_page.eval("document.querySelector('#play').click(); return true;")
            time.sleep(0.4)
            still_blocked = snapshot(next_page)
            assert_true(blocked["playDisabled"] is True, "Next player Play button was enabled during the reaction window.")
            assert_true(bool(still_blocked["reaction"]), "Reaction window disappeared after blocked Play click.")
            summary["events"].append({"step": "fast-next-play-blocked", "tab": next_index + 1, "snapshot": still_blocked})

            actor_id = state(first)["centerPile"]["lastPlay"]["playerId"]
            challenge_index = non_actor_index(pages, actor_id, start=next_index + 2)
            challenge_page = pages[challenge_index]
            assert_true(snapshot(challenge_page)["challengeDisabled"] is False, "Call Bluff was disabled during reaction window.")
            challenge_page.eval("document.querySelector('#challenge').click(); return true;")
            challenge_page.wait("return state && state.centerPile.count === 0;", timeout=5)
            summary["events"].append({"step": "challenge-during-window", "tab": challenge_index + 1, "snapshot": snapshot(challenge_page)})

            current_index = current_page_index(pages, challenge_page)
            current_page = pages[current_index]
            current_page.wait("return state && state.canAct === true;")
            current_page.wait("return state.you && state.you.hand && state.you.hand.length > 0;", timeout=5)
            true_choice = choose_play(current_page, truthful=True)
            current_page.eval("document.querySelector('#play').click(); return true;")
            current_page.wait("return !!document.querySelector('.reaction-status');", timeout=3)
            locked_index = current_page_index(pages, current_page)
            locked_page = pages[locked_index]
            locked_page.wait("return !!document.querySelector('.reaction-status');", timeout=3)
            true_window = snapshot(locked_page)
            assert_true("10s" in true_window["reaction"], "Second reaction window did not show 10s.")
            summary["events"].append({"step": "truthful-play-window", "actorTab": current_index + 1, "lockedTurnTab": locked_index + 1, "choice": true_choice, "snapshot": true_window})

            locked_page.wait("return !document.querySelector('.reaction-status');", timeout=BLUFF_WINDOW_SECONDS + 5)
            unlocked = snapshot(locked_page)
            if unlocked["passDisabled"] is not False:
                debug = locked_page.eval(
                    """
                    return {
                      snapshot: arguments[0],
                      canUseTurnActions: canUseTurnActions(),
                      bluffActive: isBluffWindowActive(),
                      remaining: bluffWindowRemainingMs(),
                      canAct: state.canAct,
                      you: state.you?.name,
                      currentPlayerName: state.currentPlayerName,
                      currentPlayerId: state.currentPlayerId,
                      youId: state.you?.id
                    };
                    """,
                    [unlocked],
                )
                raise RuntimeError(f"Pass did not unlock after the 10-second window: {debug}")
            locked_page.eval("document.querySelector('#pass').click(); return true;")
            locked_page.wait("return state && state.actions[0] && state.actions[0].type === 'pass';", timeout=5)
            summary["events"].append({"step": "pass-after-window", "tab": locked_index + 1, "snapshot": snapshot(locked_page)})

            actor_id = state(locked_page)["centerPile"]["lastPlay"]["playerId"]
            late_challenge_index = non_actor_index(pages, actor_id, start=locked_index + 3)
            late_challenge_page = pages[late_challenge_index]
            assert_true(snapshot(late_challenge_page)["challengeDisabled"] is False, "Call Bluff was disabled after a pass.")
            late_challenge_page.eval("document.querySelector('#challenge').click(); return true;")
            late_challenge_page.wait("return state && state.centerPile.count === 0;", timeout=5)
            summary["events"].append({"step": "challenge-after-pass", "tab": late_challenge_index + 1, "snapshot": snapshot(late_challenge_page)})

            late_challenge_page.screenshot(SCREENSHOT)
            summary["screenshot"] = str(SCREENSHOT)
            summary["result"] = "passed"
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            signal.alarm(0)
            for page in pages:
                page.close()
            brave.terminate()
            try:
                brave.wait(timeout=2)
            except subprocess.TimeoutExpired:
                brave.kill()
                brave.wait(timeout=2)
            server.terminate()
            try:
                server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
