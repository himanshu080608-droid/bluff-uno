from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import game_logic as game


PORT = int(os.environ.get("PORT", "3000"))
ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"

app = FastAPI()
socket_lock = asyncio.Lock()
room_sockets: dict[str, list[dict]] = {}


def api_error(error: Exception) -> JSONResponse:
    return JSONResponse({"error": str(error)}, status_code=400)


def touch(room: dict) -> None:
    room["version"] += 1


def new_room(name: str | None) -> tuple[dict, dict]:
    player = game.make_player(name, True)
    code = game.room_code()
    room = {
        "code": code,
        "status": "lobby",
        "hostId": player["id"],
        "players": [player],
        "order": [],
        "currentIndex": -1,
        "centerPile": [],
        "burnedPile": [],
        "lastPlay": None,
        "activeRank": None,
        "passedSinceLastPlay": [],
        "winners": [],
        "actions": [],
        "actionId": 0,
        "version": 1,
        "bluffWindowUntil": 0,
        "createdAt": game.now_ms(),
    }
    game.rooms[code] = room
    game.add_log(room, "join", player["id"], f"{player['name']} created the room.")
    return room, player


def require_room(code: str | None) -> dict:
    room = game.get_room(code)
    if not room:
        raise ValueError("Room not found.")
    return room


def handle_join(body: dict) -> tuple[dict, str]:
    room = require_room(body.get("code"))
    if room["status"] == "closed":
        raise ValueError("This room has been closed.")
    if game.absent_non_winner_named_player(room, body.get("name")):
        raise ValueError("You left this room before ranking and cannot rejoin it.")
    if room["status"] == "playing":
        player = game.rejoin_ranked_player(room, body.get("name"))
        if not player:
            raise ValueError("This game has already started. Only players who already ranked may rejoin under their previous name.")
        touch(room)
        return room, player["id"]
    if room["status"] != "lobby":
        raise ValueError("This game has already started.")
    if len(room["players"]) >= 12:
        raise ValueError("This room is full.")

    player = game.make_player(body.get("name"), False)
    room["players"].append(player)
    game.add_log(room, "join", player["id"], f"{player['name']} joined the room.")
    touch(room)
    return room, player["id"]


def handle_action(body: dict) -> tuple[dict, str]:
    room = require_room(body.get("code"))
    player = game.player_by_id(room, body.get("playerId"))
    if not player:
        raise ValueError("Player not found in this room.")
    if not player.get("present", True):
        raise ValueError("You are no longer in this room.")
    if room["status"] != "playing":
        raise ValueError("The game is not currently active.")
    if player["id"] in room["winners"]:
        raise ValueError("You are already out of cards.")

    action_type = body.get("type")
    if action_type == "challenge":
        game.challenge(room, player)
    else:
        if game.bluff_window_active(room) and action_type != "pass":
            raise ValueError("Wait for the bluff-call window to finish.")
        current = game.current_player(room)
        if not current or current["id"] != player["id"]:
            raise ValueError("It is not your turn.")
        if action_type == "play":
            game.play_cards(room, player, body.get("cardIds") or [], body.get("claimedRank"))
        elif action_type == "pass":
            game.pass_turn(room, player)
        elif action_type == "final-pass":
            game.final_pass(room, player)
        else:
            raise ValueError("Unknown action.")

    touch(room)
    return room, player["id"]


async def send_socket_state(client: dict, room: dict) -> bool:
    payload = {"type": "state", "room": game.sanitize_room(room, client["playerId"])}
    try:
        async with client["lock"]:
            await client["websocket"].send_json(payload)
        return True
    except Exception:
        return False


async def broadcast_room(code: str) -> None:
    async with socket_lock:
        clients = list(room_sockets.get(code, []))

    if not clients:
        return

    with game.state_lock:
        room = game.get_room(code)
        if not room:
            return

    failed = []
    for client in clients:
        if not await send_socket_state(client, room):
            failed.append(client)

    if failed:
        failed_ids = {id(client) for client in failed}
        async with socket_lock:
            room_sockets[code] = [client for client in room_sockets.get(code, []) if id(client) not in failed_ids]


async def mutate_and_broadcast(operation) -> JSONResponse | dict:
    try:
        with game.state_lock:
            room, viewer_id = operation()
            response = {"room": game.sanitize_room(room, viewer_id)}
            code = room["code"]
        await broadcast_room(code)
        return response
    except Exception as error:
        return api_error(error)


@app.post("/api/create")
async def create_room(body: dict = Body(default_factory=dict)):
    try:
        with game.state_lock:
            room, player = new_room(body.get("name"))
            return {"room": game.sanitize_room(room, player["id"])}
    except Exception as error:
        return api_error(error)


@app.post("/api/join")
async def join_room(body: dict = Body(default_factory=dict)):
    return await mutate_and_broadcast(lambda: handle_join(body))


@app.post("/api/state")
async def room_state(body: dict = Body(default_factory=dict)):
    try:
        with game.state_lock:
            room = require_room(body.get("code"))
            return {"room": game.sanitize_room(room, body.get("playerId"))}
    except Exception as error:
        return api_error(error)


@app.post("/api/start")
async def start_game(body: dict = Body(default_factory=dict)):
    def operation() -> tuple[dict, str | None]:
        room = require_room(body.get("code"))
        game.start_game(room, body.get("playerId"))
        touch(room)
        return room, body.get("playerId")

    return await mutate_and_broadcast(operation)


@app.post("/api/close")
async def close_room(body: dict = Body(default_factory=dict)):
    def operation() -> tuple[dict, str | None]:
        room = require_room(body.get("code"))
        game.close_room(room, body.get("playerId"))
        touch(room)
        return room, body.get("playerId")

    return await mutate_and_broadcast(operation)


@app.post("/api/leave")
async def leave_room(body: dict = Body(default_factory=dict)):
    def operation() -> tuple[dict, str | None]:
        room = require_room(body.get("code"))
        game.leave_room(room, body.get("playerId"))
        touch(room)
        return room, body.get("playerId")

    return await mutate_and_broadcast(operation)


@app.post("/api/action")
async def player_action(body: dict = Body(default_factory=dict)):
    return await mutate_and_broadcast(lambda: handle_action(body))


@app.websocket("/ws")
async def websocket_state(websocket: WebSocket, code: str = Query(""), playerId: str = Query("")):
    code = str(code or "").upper()
    await websocket.accept()

    with game.state_lock:
        room = game.get_room(code)
        if not room or not game.player_by_id(room, playerId):
            await websocket.send_json({"type": "error", "error": "Room or player not found."})
            await websocket.close(code=1008)
            return

    client = {"playerId": playerId, "websocket": websocket, "lock": asyncio.Lock()}
    async with socket_lock:
        room_sockets.setdefault(code, []).append(client)

    try:
        with game.state_lock:
            room = require_room(code)
        await send_socket_state(client, room)

        while True:
            message = await websocket.receive_text()
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                data = {}
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong", "serverNow": game.now_ms()})
            elif data.get("type") == "state":
                with game.state_lock:
                    room = require_room(code)
                await send_socket_state(client, room)
    except WebSocketDisconnect:
        pass
    finally:
        async with socket_lock:
            room_sockets[code] = [item for item in room_sockets.get(code, []) if item is not client]


@app.get("/")
async def index():
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "serverNow": game.now_ms(), "rooms": len(game.rooms)}


@app.get("/{path:path}")
async def static_file(path: str):
    requested = (PUBLIC_DIR / path).resolve()
    if PUBLIC_DIR not in requested.parents and requested != PUBLIC_DIR:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if requested.is_file():
        return FileResponse(requested)
    return FileResponse(PUBLIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False, ws_ping_interval=30, ws_ping_timeout=30)
