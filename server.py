from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import game_logic as game

try:
    import redis
except ImportError:
    redis = None


PORT = int(os.environ.get("PORT", "8000"))
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "").strip()
PUBLIC_KEEPALIVE_URL = os.environ.get("PUBLIC_KEEPALIVE_URL", "").strip()
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
REDIS_ROOM_KEY_PREFIX = os.environ.get("REDIS_ROOM_KEY_PREFIX", "bluff:room:")
REDIS_ROOM_TTL_SECONDS = int(os.environ.get("REDIS_ROOM_TTL_SECONDS", "7200"))
origins_env = os.environ.get("CORS_ALLOW_ORIGINS", "")
CORS_ALLOW_ORIGINS = [origin.strip() for origin in origins_env.split(",") if origin.strip()]
DEFAULT_CORS_ALLOW_ORIGINS = ["https://bluff-uno.netlify.app"]
browser_keepalive_url = ""
learned_keepalive_url = ""
KEEPALIVE_MIN_INTERVAL_SECONDS = 30
KEEPALIVE_DEFAULT_INTERVAL_SECONDS = 45
redis_client = None
redis_import_warning_printed = False
redis_error_printed = False


def keepalive_interval_seconds() -> int:
    try:
        interval = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", str(KEEPALIVE_DEFAULT_INTERVAL_SECONDS)))
    except ValueError:
        interval = KEEPALIVE_DEFAULT_INTERVAL_SECONDS
    return max(KEEPALIVE_MIN_INTERVAL_SECONDS, interval)


def active_keepalive_url() -> str:
    return KEEPALIVE_URL or PUBLIC_KEEPALIVE_URL or browser_keepalive_url or learned_keepalive_url


def get_redis_client():
    global redis_client, redis_import_warning_printed
    if not REDIS_URL:
        return None
    if redis is None:
        if not redis_import_warning_printed:
            print("Redis persistence disabled: install the redis package or remove REDIS_URL.", flush=True)
            redis_import_warning_printed = True
        return None
    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    return redis_client


def redis_room_key(code: str) -> str:
    return f"{REDIS_ROOM_KEY_PREFIX}{code}"


def normalize_recovery_code(value: str | None) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def recovery_code_used(room: dict, code: str, player_id: str | None = None) -> bool:
    normalized = normalize_recovery_code(code)
    return any(
        normalize_recovery_code(player.get("recoveryCode")) == normalized
        for player in room.get("players", [])
        if player.get("id") != player_id
    )


def ensure_player_recovery_code(room: dict, player: dict) -> bool:
    current = player.get("recoveryCode")
    if current and not recovery_code_used(room, current, player.get("id")):
        return False
    while True:
        candidate = game.recovery_code()
        if not recovery_code_used(room, candidate, player.get("id")):
            player["recoveryCode"] = candidate
            return True


def ensure_room_recovery_codes(room: dict) -> bool:
    changed = False
    seen = set()
    for player in room.get("players", []):
        normalized = normalize_recovery_code(player.get("recoveryCode"))
        if not normalized or normalized in seen:
            changed = ensure_player_recovery_code(room, player) or changed
            normalized = normalize_recovery_code(player.get("recoveryCode"))
        seen.add(normalized)
    return changed


def active_room_player_count(room: dict) -> int:
    return sum(1 for player in room.get("players", []) if player.get("present", True))


def should_delete_room(room: dict) -> bool:
    return room.get("status") == "closed" or active_room_player_count(room) == 0


def remember_redis_error(action: str, error: Exception) -> None:
    global redis_error_printed
    if redis_error_printed:
        return
    print(f"Redis persistence {action} failed: {error}", flush=True)
    redis_error_printed = True


def save_room(room: dict) -> None:
    client = get_redis_client()
    if not client:
        return
    try:
        ensure_room_recovery_codes(room)
        key = redis_room_key(room["code"])
        if should_delete_room(room):
            client.delete(key)
            return
        room["updatedAt"] = game.now_ms()
        client.setex(key, REDIS_ROOM_TTL_SECONDS, json.dumps(room))
    except Exception as error:
        remember_redis_error("save", error)


def load_persisted_rooms() -> None:
    client = get_redis_client()
    if not client:
        return
    loaded = 0
    try:
        for key in client.scan_iter(f"{REDIS_ROOM_KEY_PREFIX}*"):
            raw = client.get(key)
            if not raw:
                continue
            room = json.loads(raw)
            code = str(room.get("code") or "").strip().upper()
            if not code or not isinstance(room.get("players"), list):
                continue
            room["code"] = code
            changed = ensure_room_recovery_codes(room)
            game.rooms[code] = room
            if changed and not should_delete_room(room):
                room["updatedAt"] = game.now_ms()
                client.setex(redis_room_key(code), REDIS_ROOM_TTL_SECONDS, json.dumps(room))
            loaded += 1
        print(f"Redis persistence loaded {loaded} room(s).", flush=True)
    except Exception as error:
        remember_redis_error("load", error)


def redis_status() -> str:
    if not REDIS_URL:
        return "disabled"
    client = get_redis_client()
    if not client:
        return "unavailable"
    try:
        client.ping()
        return "connected"
    except Exception as error:
        remember_redis_error("ping", error)
        return "unavailable"


def remember_keepalive_url(request: Request) -> None:
    global learned_keepalive_url
    if KEEPALIVE_URL or PUBLIC_KEEPALIVE_URL or learned_keepalive_url:
        return
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    learned_keepalive_url = f"{proto}://{host}/health"
    print(f"Keepalive URL learned: {learned_keepalive_url}", flush=True)


def remember_public_keepalive_origin(origin: str | None) -> bool:
    global browser_keepalive_url
    if KEEPALIVE_URL or PUBLIC_KEEPALIVE_URL or browser_keepalive_url or not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    browser_keepalive_url = f"{parsed.scheme}://{parsed.netloc}/health"
    print(f"Keepalive URL learned from browser: {browser_keepalive_url}", flush=True)
    return True


def ping_keepalive_url() -> None:
    url = active_keepalive_url()
    if not url:
        return
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "bluff-uno-keepalive/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


async def send_keepalive_ping(reason: str) -> None:
    url = active_keepalive_url()
    if not url:
        return
    try:
        await asyncio.to_thread(ping_keepalive_url)
        print(f"Keepalive ping ok ({reason}): {url}", flush=True)
    except Exception as error:
        print(f"Keepalive ping failed ({reason}): {error}", flush=True)


async def keepalive_loop() -> None:
    interval = keepalive_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        await send_keepalive_ping("scheduled")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with game.state_lock:
        load_persisted_rooms()
    keepalive_task = asyncio.create_task(keepalive_loop())
    try:
        yield
    finally:
        if keepalive_task:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS if CORS_ALLOW_ORIGINS else DEFAULT_CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
socket_lock = asyncio.Lock()
room_sockets: dict[str, list[dict]] = {}


@app.middleware("http")
async def learn_keepalive_url(request: Request, call_next):
    remember_keepalive_url(request)
    return await call_next(request)


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
        "updatedAt": game.now_ms(),
    }
    ensure_room_recovery_codes(room)
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
        ensure_player_recovery_code(room, player)
        touch(room)
        return room, player["id"]
    if room["status"] != "lobby":
        raise ValueError("This game has already started.")
    if len(room["players"]) >= 12:
        raise ValueError("This room is full.")

    player = game.make_player(body.get("name"), False)
    room["players"].append(player)
    ensure_player_recovery_code(room, player)
    game.add_log(room, "join", player["id"], f"{player['name']} joined the room.")
    touch(room)
    return room, player["id"]


def handle_recover(body: dict) -> tuple[dict, str]:
    room = require_room(body.get("code"))
    ensure_room_recovery_codes(room)
    recovery_code = normalize_recovery_code(body.get("recoveryCode"))
    if not recovery_code:
        raise ValueError("Enter a recovery code.")
    player = next(
        (
            item
            for item in room["players"]
            if normalize_recovery_code(item.get("recoveryCode")) == recovery_code
        ),
        None,
    )
    if not player:
        raise ValueError("Recovery code not found for this room.")
    if not player.get("present", True):
        player["present"] = True
        player["leftAt"] = None
        game.add_log(room, "rejoin", player["id"], f"{player['name']} recovered their seat.")
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
            save_room(room)
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
            save_room(room)
            return {"room": game.sanitize_room(room, player["id"])}
    except Exception as error:
        return api_error(error)


@app.post("/api/join")
async def join_room(body: dict = Body(default_factory=dict)):
    return await mutate_and_broadcast(lambda: handle_join(body))


@app.post("/api/recover")
async def recover_room(body: dict = Body(default_factory=dict)):
    return await mutate_and_broadcast(lambda: handle_recover(body))


@app.post("/api/state")
async def room_state(body: dict = Body(default_factory=dict)):
    try:
        with game.state_lock:
            room = require_room(body.get("code"))
            if ensure_room_recovery_codes(room):
                save_room(room)
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


@app.post("/api/keepalive-origin")
async def keepalive_origin(body: dict = Body(default_factory=dict)):
    learned_from_browser = remember_public_keepalive_origin(body.get("origin"))
    if learned_from_browser:
        asyncio.create_task(send_keepalive_ping("browser-origin"))
    return {"ok": True, "keepaliveReady": bool(active_keepalive_url())}


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
        if ensure_room_recovery_codes(room):
            save_room(room)

    client = {"playerId": playerId, "websocket": websocket, "lock": asyncio.Lock()}
    async with socket_lock:
        room_sockets.setdefault(code, []).append(client)

    try:
        with game.state_lock:
            room = require_room(code)
            if ensure_room_recovery_codes(room):
                save_room(room)
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
                    if ensure_room_recovery_codes(room):
                        save_room(room)
                await send_socket_state(client, room)
    except WebSocketDisconnect:
        pass
    finally:
        async with socket_lock:
            room_sockets[code] = [item for item in room_sockets.get(code, []) if item is not client]


@app.get("/health")
async def health():
    return {"ok": True, "serverNow": game.now_ms(), "rooms": len(game.rooms), "redis": redis_status()}


def run_local_server() -> None:
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False, ws_ping_interval=45, ws_ping_timeout=120)


if __name__ == "__main__":
    run_local_server()
