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
PLAYER_COUNT = 8
BLUFF_WINDOW_SECONDS = 10


class CaptureTimeout(RuntimeError):
    pass


def stop_capture(_signum, _frame) -> None:
    raise CaptureTimeout("8-tab UI stress check timed out.")


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
        with urllib.request.urlopen(req, timeout=24) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            parsed_error = json.loads(body.decode() or "{}")
        except Exception:
            parsed_error = {}
        raise RuntimeError(parsed_error.get("error") or parsed_error.get("value") or body.decode(errors="replace") or f"HTTP {error.code}") from error
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
        time.sleep(0.12)
    raise RuntimeError(f"Timed out waiting for browser condition: {last_error}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def switch_to(session_id: str, handle: str) -> None:
    wd(session_id, "POST", "/window", {"handle": handle})


def open_tab(session_id: str, url: str) -> str:
    time.sleep(0.35)
    before = set(wd(session_id, "GET", "/window/handles")["value"])
    execute_script(session_id, "window.open(arguments[0], '_blank');", [url])
    deadline = time.time() + 8
    handle = None
    while time.time() < deadline:
        current = set(wd(session_id, "GET", "/window/handles")["value"])
        new_handles = list(current - before)
        if new_handles:
            handle = new_handles[0]
            break
        time.sleep(0.15)
    if not handle:
        raise RuntimeError("Safari did not create a new tab.")
    switch_to(session_id, handle)
    time.sleep(0.25)
    wait_script(session_id, "return document.readyState === 'complete';")
    return handle


def clear_browser_room_storage(session_id: str) -> None:
    execute_script(
        session_id,
        """
        localStorage.clear();
        document.cookie.split(";").forEach((part) => {
          const name = part.trim().split("=")[0];
          if (name) document.cookie = `${name}=; max-age=0; path=/; SameSite=Lax`;
        });
        """,
    )


def reset_home(session_id: str) -> None:
    clear_browser_room_storage(session_id)
    wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/"})
    try:
        wait_script(session_id, "return document.readyState === 'complete' && !!document.querySelector('#create');", timeout=5.0)
    except Exception:
        execute_script(session_id, "location.href = arguments[0];", [f"http://{HOST}:{PORT}/"])
        wait_script(session_id, "return document.readyState === 'complete' && !!document.querySelector('#create');", timeout=8.0)
    execute_script(
        session_id,
        """
        if (document.querySelector("#clearSessions")) {
          clearAllStoredSessions();
          render();
        }
        """,
    )
    try:
        wait_script(session_id, "return !!document.querySelector('#join') && !document.querySelector('#join').disabled;")
    except Exception as error:
        snapshot = execute_script(
            session_id,
            """
            return {
              url: location.href,
              ready: document.readyState,
              title: document.title,
              text: document.body ? document.body.innerText.slice(0, 500) : "",
              hasJoin: !!document.querySelector("#join"),
              joinDisabled: document.querySelector("#join")?.disabled ?? null,
              hasClear: !!document.querySelector("#clearSessions"),
              cookie: document.cookie,
              keys: Object.keys(localStorage)
            };
            """,
        )
        raise RuntimeError(f"Home reset failed: {snapshot}") from error


def current_state(session_id: str) -> dict:
    state = execute_script(
        session_id,
        """
        try {
          return JSON.parse(JSON.stringify(state));
        } catch (error) {
          return null;
        }
        """,
    )
    if not isinstance(state, dict):
        raise RuntimeError("Could not read client state from the active tab.")
    return state


def fill_and_click(session_id: str, field: str, value: str, button: str) -> None:
    execute_script(
        session_id,
        """
        const [field, value, button] = arguments;
        const input = document.querySelector(field);
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        document.querySelector(button).click();
        """,
        [field, value, button],
    )


def create_room(session_id: str) -> str:
    reset_home(session_id)
    fill_and_click(session_id, "#name", "p1", "#create")
    wait_script(session_id, "return !!document.querySelector('.lobby-list');")
    state = current_state(session_id)
    return state["code"]


def join_room(session_id: str, code: str, name: str) -> None:
    reset_home(session_id)
    fill_and_click(session_id, "#code", code, "#join")
    wait_script(session_id, "return !!document.querySelector('#joinName');")
    fill_and_click(session_id, "#joinName", name, "#confirmJoin")
    wait_script(session_id, "return !!document.querySelector('.lobby-list');")


def start_game(session_id: str) -> None:
    execute_script(session_id, "document.querySelector('#start').click();")
    wait_script(session_id, "return !!document.querySelector('.game-shell') && !!document.querySelector('.hand-panel');")


def wait_for_turn(session_id: str, timeout: float = 14.0) -> None:
    wait_script(session_id, "return !!state && state.canAct === true;", timeout=timeout)


def choose_play(session_id: str, truthful: bool = True, count: int = 1) -> dict:
    return execute_script(
        session_id,
        """
        const [truthful, count] = arguments;
        const cards = [...document.querySelectorAll(".hand .uno-card")].slice(0, count);
        cards.forEach((card) => card.click());
        const rank = document.querySelector("#rank");
        const hand = state.you.hand;
        let claim = state.activeRank || hand[0].claimRank;
        if (!state.activeRank && !truthful) {
          claim = state.ranks.find((item) => item !== hand[0].claimRank) || hand[0].claimRank;
        }
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


def click_play(session_id: str) -> None:
    execute_script(session_id, "document.querySelector('#play').click();")


def click_pass(session_id: str) -> None:
    execute_script(session_id, "document.querySelector('#pass').click();")


def click_challenge(session_id: str) -> None:
    execute_script(session_id, "document.querySelector('#challenge').click();")


def tab_snapshot(session_id: str) -> dict:
    return execute_script(
        session_id,
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
        """,
    )


def current_tab_index(handles: list[str], session_id: str) -> int:
    room = current_state(session_id)
    player_id = room.get("currentPlayerId")
    for index, handle in enumerate(handles):
        switch_to(session_id, handle)
        if current_state(session_id)["you"]["id"] == player_id:
            return index
    raise RuntimeError("Could not find current player's tab.")


def find_non_actor_tab(handles: list[str], session_id: str, actor_id: str, prefer_offset: int = 1) -> int:
    for step in range(prefer_offset, prefer_offset + len(handles)):
        index = step % len(handles)
        switch_to(session_id, handles[index])
        state_value = current_state(session_id)
        if state_value["you"]["id"] != actor_id and not state_value["you"]["id"] in [winner["id"] for winner in state_value["winners"]]:
            return index
    raise RuntimeError("Could not find challenge tab.")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    signal.signal(signal.SIGALRM, stop_capture)
    signal.alarm(90)

    if not port_free(PORT):
        raise RuntimeError(f"Port {PORT} is already in use.")
    if not port_free(DRIVER_PORT):
        raise RuntimeError(f"Port {DRIVER_PORT} is already in use.")

    env = os.environ.copy()
    env["PORT"] = str(PORT)
    server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    driver = subprocess.Popen(["safaridriver", "-p", str(DRIVER_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session_id: str | None = None
    summary: dict = {"players": PLAYER_COUNT, "events": []}

    try:
        wait_http(f"http://{HOST}:{PORT}/")
        wait_http(f"http://127.0.0.1:{DRIVER_PORT}/status")

        session = wd(None, "POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        session_id = session["value"]["sessionId"]
        wd(session_id, "POST", "/window/rect", {"width": 1500, "height": 1000, "x": 40, "y": 40})
        wd(session_id, "POST", "/url", {"url": f"http://{HOST}:{PORT}/"})
        wait_script(session_id, "return document.readyState === 'complete' && !!document.querySelector('#create');")

        first_handle = wd(session_id, "GET", "/window")["value"]
        handles = [first_handle]
        code = create_room(session_id)
        summary["room"] = code
        summary["events"].append({"step": "create", "room": code})

        for number in range(2, PLAYER_COUNT + 1):
            handle = open_tab(session_id, f"http://{HOST}:{PORT}/")
            handles.append(handle)
            join_room(session_id, code, f"p{number}")
            summary["events"].append({"step": "join", "player": f"p{number}"})

        switch_to(session_id, handles[0])
        wait_script(session_id, "return state && state.players && state.players.length === 8;")
        start_game(session_id)
        order = [player["name"] for player in current_state(session_id)["players"]]
        summary["order"] = order
        summary["events"].append({"step": "start", "order": order})

        switch_to(session_id, handles[0])
        wait_for_turn(session_id)
        first_choice = choose_play(session_id, truthful=False)
        click_play(session_id)
        wait_script(session_id, "return !!document.querySelector('.action-splash') && !!document.querySelector('.reaction-status');", timeout=3.0)
        first_window = tab_snapshot(session_id)
        assert_true("10s" in first_window["reaction"], "First reaction window did not show 10s.")
        assert_true(first_window["incomingCards"] >= 1, "Pile throw animation did not mark incoming cards.")
        summary["events"].append({"step": "bluff-play", "choice": first_choice, "snapshot": first_window})

        next_index = current_tab_index(handles, session_id)
        switch_to(session_id, handles[next_index])
        choose_play(session_id, truthful=True)
        blocked = tab_snapshot(session_id)
        click_play(session_id)
        time.sleep(0.35)
        still_blocked = tab_snapshot(session_id)
        assert_true(blocked["playDisabled"] is True, "Next player play button was enabled during bluff window.")
        assert_true(still_blocked["reaction"], "Reaction window disappeared too early after disabled play click.")
        summary["events"].append({"step": "fast-next-play-blocked", "playerTab": next_index + 1, "snapshot": still_blocked})

        actor_id = current_state(session_id)["centerPile"]["lastPlay"]["playerId"]
        challenge_index = find_non_actor_tab(handles, session_id, actor_id, prefer_offset=next_index + 2)
        switch_to(session_id, handles[challenge_index])
        before_challenge = tab_snapshot(session_id)
        assert_true(before_challenge["challengeDisabled"] is False, "Call Bluff was disabled during reaction window.")
        click_challenge(session_id)
        wait_script(session_id, "return state && state.centerPile.count === 0;", timeout=4.0)
        after_challenge = tab_snapshot(session_id)
        summary["events"].append({"step": "challenge-during-window", "playerTab": challenge_index + 1, "snapshot": after_challenge})

        current_index = current_tab_index(handles, session_id)
        switch_to(session_id, handles[current_index])
        wait_for_turn(session_id)
        second_choice = choose_play(session_id, truthful=True)
        click_play(session_id)
        wait_script(session_id, "return !!document.querySelector('.reaction-status');", timeout=3.0)
        second_window = tab_snapshot(session_id)
        assert_true("10s" in second_window["reaction"], "Second reaction window did not show 10s.")
        summary["events"].append({"step": "truthful-play-window", "playerTab": current_index + 1, "choice": second_choice, "snapshot": second_window})

        wait_script(session_id, "return !document.querySelector('.reaction-status');", timeout=BLUFF_WINDOW_SECONDS + 4)
        unlocked = tab_snapshot(session_id)
        assert_true(unlocked["passDisabled"] is False, "Pass did not unlock after 10-second window.")
        assert_true(unlocked["playDisabled"] in (True, False), "Play button missing after window.")
        click_pass(session_id)
        wait_script(session_id, "return state && state.actions[0] && state.actions[0].type === 'pass';", timeout=4.0)
        passed = tab_snapshot(session_id)
        summary["events"].append({"step": "pass-after-window", "snapshot": passed})

        pass_challenge_index = find_non_actor_tab(handles, session_id, current_state(session_id)["centerPile"]["lastPlay"]["playerId"], prefer_offset=current_index + 3)
        switch_to(session_id, handles[pass_challenge_index])
        assert_true(tab_snapshot(session_id)["challengeDisabled"] is False, "Call Bluff disabled after a pass.")
        click_challenge(session_id)
        wait_script(session_id, "return state && state.centerPile.count === 0;", timeout=4.0)
        wrong_call = tab_snapshot(session_id)
        summary["events"].append({"step": "challenge-after-pass", "playerTab": pass_challenge_index + 1, "snapshot": wrong_call})

        summary["result"] = "passed"
        print(json.dumps(summary, indent=2))
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
