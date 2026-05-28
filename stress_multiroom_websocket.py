from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import websockets


PORT = int(os.environ.get("STRESS_PORT", "3102"))
BASE = f"http://127.0.0.1:{PORT}"
ROOM_SIZES = [6, 7, 8, 8]


def post(path: str, payload: dict, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        try:
            parsed = json.loads(error.read().decode() or "{}")
        except Exception:
            parsed = {}
        raise RuntimeError(parsed.get("error") or f"HTTP {error.code}") from error


def wait_http(timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/", timeout=0.6) as response:
                if response.status < 500:
                    return
        except Exception as error:
            last_error = error
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for server: {last_error}")


async def connect_player(code: str, player_id: str) -> websockets.ClientConnection:
    websocket = await websockets.connect(f"ws://127.0.0.1:{PORT}/ws?code={code}&playerId={player_id}")
    initial = json.loads(await asyncio.wait_for(websocket.recv(), timeout=4))
    if initial.get("type") != "state":
        raise RuntimeError(f"Unexpected initial websocket message: {initial}")
    return websocket


async def receive_until(websocket, predicate, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last_message = None
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=remaining))
        last_message = message
        if predicate(message):
            return message
    raise RuntimeError(f"Timed out waiting for websocket predicate. Last message: {last_message}")


def player_state(code: str, player_id: str) -> dict:
    return post("/api/state", {"code": code, "playerId": player_id})["room"]


def current_player_id(room: dict) -> str:
    if not room.get("currentPlayerId"):
        raise RuntimeError(f"Room has no current player: {room.get('code')}")
    return room["currentPlayerId"]


def card_for(player_room: dict, prefer_truthful: bool = True) -> dict:
    hand = player_room["you"]["hand"]
    if not hand:
        raise RuntimeError(f"{player_room['you']['name']} has no cards.")
    active = player_room.get("activeRank")
    if active and prefer_truthful:
        matching = next((card for card in hand if card["claimRank"] == active), None)
        if matching:
            return matching
    return hand[0]


def alternate_rank(room: dict, actual_rank: str) -> str:
    return next(rank for rank in room["ranks"] if rank != actual_rank)


def player_name(room: dict, player_id: str) -> str:
    return next(player["name"] for player in room["players"] if player["id"] == player_id)


def non_actor(room: dict, actor_id: str, offset: int = 1) -> str:
    players = room["players"]
    start = next((index for index, player in enumerate(players) if player["id"] == actor_id), 0)
    for step in range(offset, offset + len(players)):
        player = players[(start + step) % len(players)]
        if player["id"] != actor_id and not player["isWinner"]:
            return player["id"]
    raise RuntimeError("Could not find non-actor player.")


def action(code: str, player_id: str, payload: dict) -> dict:
    data = {"code": code, "playerId": player_id, **payload}
    return post("/api/action", data)["room"]


async def room_flow(room_index: int, player_count: int) -> dict:
    created = post("/api/create", {"name": f"r{room_index}-p1"})["room"]
    code = created["code"]
    players = [{"id": created["you"]["id"], "name": created["you"]["name"]}]

    for index in range(2, player_count + 1):
        joined = post("/api/join", {"code": code, "name": f"r{room_index}-p{index}"})["room"]
        players.append({"id": joined["you"]["id"], "name": joined["you"]["name"]})

    sockets = [await connect_player(code, player["id"]) for player in players]
    try:
        started = post("/api/start", {"code": code, "playerId": players[0]["id"]})["room"]
        await receive_until(sockets[-1], lambda message: message["room"]["status"] == "playing")

        order = [player["name"] for player in started["players"]]
        events: list[str] = [f"started:{'->'.join(order)}"]

        actor_id = current_player_id(started)
        actor_room = player_state(code, actor_id)
        bluff_card = card_for(actor_room)
        bluff_rank = alternate_rank(actor_room, bluff_card["claimRank"])
        after_bluff = action(code, actor_id, {"type": "play", "cardIds": [bluff_card["id"]], "claimedRank": bluff_rank})
        events.append(f"bluff:{player_name(after_bluff, actor_id)} as {bluff_rank}")

        next_id = current_player_id(after_bluff)
        next_room = player_state(code, next_id)
        next_card = card_for(next_room, prefer_truthful=False)
        try:
            action(code, next_id, {"type": "play", "cardIds": [next_card["id"]], "claimedRank": after_bluff["activeRank"]})
            raise RuntimeError("Fast next play was accepted during bluff window.")
        except RuntimeError as error:
            if "Wait for the bluff-call window" not in str(error):
                raise
        events.append("blocked-fast-next-play")

        challenger_id = non_actor(after_bluff, actor_id, offset=2)
        after_right_call = action(code, challenger_id, {"type": "challenge"})
        if after_right_call["centerPile"]["count"] != 0:
            raise RuntimeError("Challenge-right did not clear center pile.")
        events.append(f"challenge-right:{player_name(after_right_call, challenger_id)}")

        actor_id = current_player_id(after_right_call)
        actor_room = player_state(code, actor_id)
        truth_card = card_for(actor_room)
        truth_rank = truth_card["claimRank"] if not actor_room.get("activeRank") else actor_room["activeRank"]
        after_truth = action(code, actor_id, {"type": "play", "cardIds": [truth_card["id"]], "claimedRank": truth_rank})
        wrong_challenger_id = non_actor(after_truth, actor_id, offset=3)
        after_wrong_call = action(code, wrong_challenger_id, {"type": "challenge"})
        if after_wrong_call["centerPile"]["count"] != 0:
            raise RuntimeError("Challenge-wrong did not clear center pile.")
        events.append(f"challenge-wrong:{player_name(after_wrong_call, wrong_challenger_id)}")

        actor_id = current_player_id(after_wrong_call)
        actor_room = player_state(code, actor_id)
        pass_card = card_for(actor_room)
        pass_rank = pass_card["claimRank"] if not actor_room.get("activeRank") else actor_room["activeRank"]
        after_pass_play = action(code, actor_id, {"type": "play", "cardIds": [pass_card["id"]], "claimedRank": pass_rank})
        window_remaining = max(0, after_pass_play["bluffWindow"]["until"] - after_pass_play["serverNow"]) / 1000
        await asyncio.sleep(window_remaining + 0.15)

        after_pass = action(code, current_player_id(after_pass_play), {"type": "pass"})
        events.append("pass-after-window")

        late_challenger_id = non_actor(after_pass, actor_id, offset=4)
        after_late_call = action(code, late_challenger_id, {"type": "challenge"})
        if after_late_call["centerPile"]["count"] != 0:
            raise RuntimeError("Late challenge after pass did not clear center pile.")
        events.append("challenge-after-pass")

        actor_id = current_player_id(after_late_call)
        actor_room = player_state(code, actor_id)
        final_card = card_for(actor_room)
        final_rank = final_card["claimRank"] if not actor_room.get("activeRank") else actor_room["activeRank"]
        after_final_play = action(code, actor_id, {"type": "play", "cardIds": [final_card["id"]], "claimedRank": final_rank})
        window_remaining = max(0, after_final_play["bluffWindow"]["until"] - after_final_play["serverNow"]) / 1000
        await asyncio.sleep(window_remaining + 0.15)

        room = after_final_play
        guard = 0
        while room["status"] == "playing":
            guard += 1
            if guard > player_count + 2:
                raise RuntimeError("Final-pass setup took too many passes.")
            current_id = current_player_id(room)
            if room["canFinalPass"] and room["you"] and room["you"]["id"] == current_id:
                break
            current_room = player_state(code, current_id)
            if current_room["canFinalPass"]:
                room = current_room
                break
            room = action(code, current_id, {"type": "pass"})

        final_player_id = current_player_id(room)
        final_state = player_state(code, final_player_id)
        if not final_state["canFinalPass"]:
            raise RuntimeError("Final pass did not become available.")
        after_final_pass = action(code, final_player_id, {"type": "final-pass"})
        if after_final_pass["centerPile"]["count"] != 0:
            raise RuntimeError("Final pass did not clear center pile.")
        events.append("final-pass")

        await receive_until(sockets[0], lambda message: message["room"]["version"] >= after_final_pass["version"])

        return {
            "room": code,
            "players": player_count,
            "finalVersion": after_final_pass["version"],
            "events": events,
            "socketCount": len(sockets),
        }
    finally:
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)


async def run_stress() -> list[dict]:
    return await asyncio.gather(*(room_flow(index + 1, count) for index, count in enumerate(ROOM_SIZES)))


def main() -> int:
    env = os.environ.copy()
    env["PORT"] = str(PORT)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    try:
        wait_http()
        started_at = time.perf_counter()
        results = asyncio.run(run_stress())
        elapsed = time.perf_counter() - started_at
        print(json.dumps({"rooms": len(results), "players": sum(item["players"] for item in results), "elapsedSeconds": round(elapsed, 2), "results": results}, indent=2))
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
