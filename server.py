from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import queue
import random
import secrets
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "3000"))
ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
BLUFF_REACTION_MS = 10000

UNO_RANKS = [
    "0/Skip",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "Reverse",
    "Draw Two",
    "Wild",
    "Wild Draw Four",
]

rooms: dict[str, dict] = {}
streams: dict[str, list[dict]] = {}
state_lock = threading.RLock()
broadcast_queue: queue.Queue[str] = queue.Queue()
rng = random.SystemRandom()


def shuffle_many(items: list) -> None:
    for _ in range(3):
        rng.shuffle(items)


def now_ms() -> int:
    return int(time.time() * 1000)


def random_id(bytes_count: int = 8) -> str:
    return secrets.token_hex(bytes_count)


def room_code() -> str:
    while True:
        code = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))
        if code not in rooms:
            return code


def clean_name(name: str | None) -> str:
    cleaned = str(name or "Player").strip()[:24]
    return cleaned or "Player"


def make_player(name: str | None, host: bool = False) -> dict:
    return {
        "id": random_id(),
        "name": clean_name(name),
        "host": host,
        "present": True,
        "hand": [],
        "joinedAt": int(time.time() * 1000),
        "leftAt": None,
    }


def make_card(serial: int, color: str, rank: str, deck: int) -> dict:
    return {
        "id": f"c{serial}",
        "color": color,
        "rank": rank,
        "claimRank": claim_rank(rank),
        "deck": deck,
        "label": rank,
    }


def claim_rank(rank: str) -> str:
    return "0/Skip" if rank in {"0", "Skip"} else rank


def make_uno_deck() -> list[dict]:
    colors = ["red", "yellow", "green", "blue"]
    cards: list[dict] = []
    serial = 1
    deck = 1
    for color in colors:
        cards.append(make_card(serial, color, "0", deck))
        serial += 1
        for rank in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Skip", "Reverse", "Draw Two"]:
            cards.append(make_card(serial, color, rank, deck))
            serial += 1
            cards.append(make_card(serial, color, rank, deck))
            serial += 1
    for rank in ["Wild", "Wild Draw Four"]:
        for _ in range(4):
            cards.append(make_card(serial, "wild", rank, deck))
            serial += 1
    shuffle_many(cards)
    return cards


def sort_hand(hand: list[dict]) -> list[dict]:
    color_order = {"red": 0, "yellow": 1, "green": 2, "blue": 3, "wild": 4}
    rank_order = {rank: index for index, rank in enumerate(UNO_RANKS)}
    hand.sort(
        key=lambda card: (
            rank_order.get(card.get("claimRank", claim_rank(card["rank"])), 99),
            card["rank"],
            color_order.get(card["color"], 9),
            card["id"],
        )
    )
    return hand


def get_room(code: str | None) -> dict | None:
    return rooms.get(str(code or "").strip().upper())


def player_by_id(room: dict, player_id: str | None) -> dict | None:
    return next((player for player in room["players"] if player["id"] == player_id), None)


def player_name(room: dict, player_id: str | None) -> str:
    player = player_by_id(room, player_id)
    return player["name"] if player else "Someone"


def present_players(room: dict) -> list[dict]:
    return [player for player in room["players"] if player.get("present", True)]


def pending_empty_player_id(room: dict) -> str | None:
    last_play = room.get("lastPlay")
    if not last_play:
        return None
    player = player_by_id(room, last_play["playerId"])
    if not player or not player.get("present", True) or player["id"] in room["winners"]:
        return None
    return player["id"] if len(player["hand"]) == 0 else None


def active_players(room: dict) -> list[dict]:
    pending_id = pending_empty_player_id(room)
    return [
        player_by_id(room, player_id)
        for player_id in room["order"]
        if player_id != pending_id
        and player_id not in room["winners"]
        and player_by_id(room, player_id)
        and player_by_id(room, player_id).get("present", True)
    ]


def current_player(room: dict) -> dict | None:
    if room["status"] != "playing" or room["currentIndex"] < 0:
        return None
    if room["currentIndex"] >= len(room["order"]):
        return None
    player = player_by_id(room, room["order"][room["currentIndex"]])
    if not player or not player.get("present", True) or player["id"] in room["winners"] or player["id"] == pending_empty_player_id(room):
        return None
    return player


def set_current_to(room: dict, player_id: str) -> None:
    room["currentIndex"] = room["order"].index(player_id)


def advance_turn(room: dict, from_player_id: str | None) -> None:
    active = active_players(room)
    if room["status"] != "playing" or not active:
        room["currentIndex"] = -1
        return

    base = room["order"].index(from_player_id) if from_player_id in room["order"] else room["currentIndex"]
    pending_id = pending_empty_player_id(room)
    for offset in range(1, len(room["order"]) + 1):
        next_index = (base + offset) % len(room["order"])
        next_id = room["order"][next_index]
        next_player = player_by_id(room, next_id)
        if next_player and next_player.get("present", True) and next_id not in room["winners"] and next_id != pending_id:
            room["currentIndex"] = next_index
            return


def can_final_pass(room: dict, player_id: str | None) -> bool:
    last_play = room.get("lastPlay")
    current = current_player(room)
    if not last_play:
        return False
    if not current or current["id"] != player_id:
        return False

    pending_id = pending_empty_player_id(room)
    if pending_id and pending_id != player_id:
        players_who_can_decline = active_players(room)
        return bool(players_who_can_decline) and all(player["id"] in room["passedSinceLastPlay"] for player in players_who_can_decline)

    if last_play["playerId"] != player_id:
        return False
    others = [player for player in active_players(room) if player["id"] != player_id]
    return bool(others) and all(player["id"] in room["passedSinceLastPlay"] for player in others)


def burn_center_pile(room: dict) -> int:
    cleared = len(room["centerPile"])
    room["burnedPile"].extend(room["centerPile"])
    room["centerPile"] = []
    room["lastPlay"] = None
    room["activeRank"] = None
    room["passedSinceLastPlay"] = []
    room["bluffWindowUntil"] = 0
    return cleared


def bluff_window_active(room: dict) -> bool:
    return bool(
        room["status"] == "playing"
        and room.get("lastPlay")
        and room.get("bluffWindowUntil", 0) > now_ms()
    )


def confirm_pending_empty_player(room: dict) -> bool:
    pending_id = pending_empty_player_id(room)
    if not pending_id:
        return False
    player = player_by_id(room, pending_id)
    if not player:
        return False
    room["winners"].append(player["id"])
    add_log(room, "win", player["id"], f"{player['name']} is out of cards.")
    return True


def add_log(room: dict, action_type: str, actor_id: str | None, text: str, detail: dict | None = None) -> None:
    room["actionId"] += 1
    room["actions"].insert(
        0,
        {
            "id": room["actionId"],
            "type": action_type,
            "actorId": actor_id,
            "text": text,
            "detail": detail or {},
            "at": int(time.time() * 1000),
        },
    )
    room["actions"] = room["actions"][:16]


def settle_winners(room: dict) -> None:
    if room["status"] != "playing":
        return
    for player in present_players(room):
        pending_own_play = room.get("lastPlay") and room["lastPlay"]["playerId"] == player["id"]
        if len(player["hand"]) == 0 and player["id"] not in room["winners"] and not pending_own_play:
            room["winners"].append(player["id"])
            add_log(room, "win", player["id"], f"{player['name']} is out of cards.")

    if pending_empty_player_id(room):
        return

    remaining = active_players(room)
    if len(remaining) <= 1 and len(room["players"]) > 1:
        if remaining and remaining[0]["id"] not in room["winners"]:
            room["winners"].append(remaining[0]["id"])
        room["status"] = "finished"
        room["currentIndex"] = -1
        room["bluffWindowUntil"] = 0
        add_log(room, "finish", None, "The game is finished.")


def card_band(count: int) -> dict:
    if count <= 0:
        return {"label": "empty", "bars": 0, "className": "empty"}
    if count <= 4:
        return {"label": "thin", "bars": 1, "className": "thin"}
    if count <= 10:
        return {"label": "light", "bars": 2, "className": "light"}
    if count <= 22:
        return {"label": "steady", "bars": 3, "className": "steady"}
    if count <= 45:
        return {"label": "heavy", "bars": 4, "className": "heavy"}
    return {"label": "towering", "bars": 5, "className": "towering"}


def public_card(card: dict) -> dict:
    return {"id": card["id"], "color": card["color"], "rank": card["rank"], "claimRank": card.get("claimRank", claim_rank(card["rank"])), "label": card["label"]}


def sanitize_room(room: dict, viewer_id: str | None) -> dict:
    server_now = now_ms()
    viewer = player_by_id(room, viewer_id)
    ordered_ids = room["order"] if room["order"] else [player["id"] for player in room["players"]]
    current = current_player(room)
    last_play = None
    if room.get("lastPlay"):
        last_play = {
            "playerId": room["lastPlay"]["playerId"],
            "playerName": player_name(room, room["lastPlay"]["playerId"]),
            "claimedRank": room["lastPlay"]["claimedRank"],
            "count": len(room["lastPlay"]["cardIds"]),
        }

    players = []
    for player_id in ordered_ids:
        player = player_by_id(room, player_id)
        if not player or not player.get("present", True):
            continue
        players.append(
            {
                "id": player["id"],
                "name": player["name"],
                "host": player["host"],
                "present": player.get("present", True),
                "isYou": player["id"] == viewer_id,
                "isCurrent": bool(current and current["id"] == player["id"]),
                "isWinner": player["id"] in room["winners"],
                "cardBand": card_band(len(player["hand"])),
            }
        )

    bluff_until = room.get("bluffWindowUntil", 0) or 0
    bluff_active = bool(room["status"] == "playing" and room.get("lastPlay") and bluff_until > server_now)

    return {
        "code": room["code"],
        "status": room["status"],
        "serverNow": server_now,
        "ranks": UNO_RANKS,
        "activeRank": room.get("activeRank"),
        "you": {
            "id": viewer["id"],
            "name": viewer["name"],
            "host": viewer["host"],
            "present": viewer.get("present", True),
            "hand": [public_card(card) for card in viewer["hand"]],
        }
        if viewer
        else None,
        "players": players,
        "hostId": room["hostId"],
        "currentPlayerId": current["id"] if current else None,
        "currentPlayerName": current["name"] if current else None,
        "centerPile": {"count": len(room["centerPile"]), "lastPlay": last_play},
        "bluffWindow": {
            "until": bluff_until,
            "durationMs": BLUFF_REACTION_MS,
            "active": bluff_active,
            "lastPlayerName": player_name(room, room["lastPlay"]["playerId"]) if room.get("lastPlay") else None,
        },
        "canStart": bool(viewer and viewer.get("present", True) and room["status"] == "lobby" and len(present_players(room)) >= 2),
        "canClose": bool(viewer and viewer.get("present", True) and viewer["id"] == room["hostId"] and room["status"] in {"lobby", "playing"}),
        "canLeave": bool(viewer and viewer.get("present", True) and room["status"] in {"lobby", "playing"}),
        "canAct": bool(viewer and current and current["id"] == viewer["id"] and room["status"] == "playing"),
        "canFinalPass": can_final_pass(room, viewer_id) if viewer else False,
        "canChallenge": bool(
            viewer
            and room["status"] == "playing"
            and room.get("lastPlay")
            and len(room["centerPile"]) > 0
            and room["lastPlay"]["playerId"] != viewer["id"]
            and viewer["id"] not in room["winners"]
            and viewer.get("present", True)
        ),
        "winners": [{"id": player_id, "name": player_name(room, player_id)} for player_id in room["winners"]],
        "actions": room["actions"],
        "version": room["version"],
    }


def write_sse_payload(client: dict, payload: bytes) -> bool:
    try:
        with client["lock"]:
            client["handler"].wfile.write(b"event: state\n")
            client["handler"].wfile.write(b"data: ")
            client["handler"].wfile.write(payload)
            client["handler"].wfile.write(b"\n\n")
            client["handler"].wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def broadcast_room(code: str) -> None:
    with state_lock:
        room = get_room(code)
        if not room:
            return
        messages = [
            (
                client,
                json.dumps(sanitize_room(room, client["playerId"]), separators=(",", ":")).encode(),
            )
            for client in list(streams.get(code, []))
        ]

    failed_clients = []
    for client, payload in messages:
        if not write_sse_payload(client, payload):
            failed_clients.append(client)

    if failed_clients:
        failed_ids = {id(client) for client in failed_clients}
        with state_lock:
            streams[code] = [client for client in streams.get(code, []) if id(client) not in failed_ids]


def broadcast_worker() -> None:
    while True:
        code = broadcast_queue.get()
        codes = {code}
        task_count = 1
        while True:
            try:
                codes.add(broadcast_queue.get_nowait())
                task_count += 1
            except queue.Empty:
                break
        for room_code in codes:
            broadcast_room(room_code)
        for _ in range(task_count):
            broadcast_queue.task_done()


def bump(room: dict) -> None:
    room["version"] += 1
    broadcast_queue.put(room["code"])


def deal(room: dict) -> None:
    deck = make_uno_deck()
    players = present_players(room)
    per_player = len(deck) // len(players)
    for player in players:
        player["hand"] = []
    for _ in range(per_player):
        for player in players:
            player["hand"].append(deck.pop())
    for player in players:
        sort_hand(player["hand"])
    room["burnedPile"] = deck
    room["centerPile"] = []
    room["activeRank"] = None
    room["bluffWindowUntil"] = 0


def start_game(room: dict, player_id: str) -> None:
    if room["status"] != "lobby":
        raise ValueError("This room has already started.")
    if not player_by_id(room, player_id) or not player_by_id(room, player_id).get("present", True):
        raise ValueError("Only a player in this room can start this game.")
    if len(present_players(room)) < 2:
        raise ValueError("At least two players are needed.")

    deal(room)
    starter = player_by_id(room, player_id)
    remaining = [player for player in present_players(room) if player["id"] != player_id]
    shuffle_many(remaining)
    ordered = [starter] + remaining
    room["order"] = [player["id"] for player in ordered]
    room["currentIndex"] = 0
    room["status"] = "playing"
    room["lastPlay"] = None
    room["activeRank"] = None
    room["passedSinceLastPlay"] = []
    room["winners"] = []
    room["bluffWindowUntil"] = 0
    add_log(room, "start", room["order"][0], f"{player_name(room, room['order'][0])} starts the table order.")


def close_room(room: dict, player_id: str) -> None:
    player = player_by_id(room, player_id)
    if room["hostId"] != player_id or not player or not player.get("present", True):
        raise ValueError("Only the room creator can close this room.")
    if room["status"] == "finished":
        raise ValueError("This game has already finished.")
    if room["status"] == "closed":
        raise ValueError("This room is already closed.")

    room["status"] = "closed"
    room["currentIndex"] = -1
    room["lastPlay"] = None
    room["activeRank"] = None
    room["passedSinceLastPlay"] = []
    room["bluffWindowUntil"] = 0
    add_log(room, "close", player_id, f"{player_name(room, player_id)} closed the room.")


def assign_host_after_leave(room: dict, leaving_player: dict) -> None:
    if leaving_player["id"] != room["hostId"]:
        return

    leaving_player["host"] = False
    replacement = next((player for player in room["players"] if player["id"] != leaving_player["id"] and player.get("present", True)), None)
    if replacement:
        replacement["host"] = True
        room["hostId"] = replacement["id"]
        add_log(room, "host", replacement["id"], f"{replacement['name']} is now the room host.")
    else:
        room["hostId"] = None


def redistribute_departing_hand(room: dict, player: dict) -> int:
    cards = player["hand"][:]
    player["hand"] = []
    shuffle_many(cards)
    receivers = [item for item in present_players(room) if item["id"] != player["id"] and item["id"] not in room["winners"]]
    if not receivers:
        room["burnedPile"].extend(cards)
        return len(cards)

    per_player = len(cards) // len(receivers)
    for receiver in receivers:
        receiver["hand"].extend(cards[:per_player])
        del cards[:per_player]

    room["burnedPile"].extend(cards)

    for receiver in receivers:
        sort_hand(receiver["hand"])
    return len(cards)


def leave_room(room: dict, player_id: str) -> None:
    player = player_by_id(room, player_id)
    if not player or not player.get("present", True):
        raise ValueError("Player not found in this room.")
    if room["status"] not in {"lobby", "playing"}:
        raise ValueError("You cannot leave this room now.")

    already_ranked = player["id"] in room["winners"]
    player["present"] = False
    player["leftAt"] = int(time.time() * 1000)
    player["host"] = False
    burned = 0

    if room["status"] == "playing" and not already_ranked:
        burned = redistribute_departing_hand(room, player)
        if room.get("lastPlay") and room["lastPlay"]["playerId"] == player["id"]:
            burn_center_pile(room)
        else:
            room["passedSinceLastPlay"] = [item for item in room["passedSinceLastPlay"] if item != player["id"]]

    add_log(
        room,
        "leave",
        player["id"],
        f"{player['name']} left the room."
        if already_ranked
        else f"{player['name']} left the room. Their hand was shuffled into the remaining players' hands.",
        {"burned": burned, "ranked": already_ranked},
    )
    assign_host_after_leave(room, player)

    if room["status"] == "playing":
        settle_winners(room)
        if room["status"] == "playing" and current_player(room) is None:
            advance_turn(room, player["id"])
            settle_winners(room)


def rejoin_ranked_player(room: dict, name: str) -> dict | None:
    cleaned = clean_name(name)
    player = next(
        (
            item
            for item in room["players"]
            if item["name"] == cleaned and not item.get("present", True) and item["id"] in room["winners"]
        ),
        None,
    )
    if not player:
        return None

    player["present"] = True
    player["leftAt"] = None
    add_log(room, "rejoin", player["id"], f"{player['name']} rejoined the room.")
    return player


def absent_non_winner_named_player(room: dict, name: str) -> dict | None:
    cleaned = clean_name(name)
    return next(
        (
            item
            for item in room["players"]
            if item["name"] == cleaned and not item.get("present", True) and item["id"] not in room["winners"]
        ),
        None,
    )


def play_cards(room: dict, player: dict, card_ids: list[str], claimed_rank: str) -> None:
    if claimed_rank not in UNO_RANKS:
        raise ValueError("Choose a valid rank to declare.")
    active_rank = room.get("activeRank")
    if active_rank and claimed_rank != active_rank:
        raise ValueError(f"The current pile rank is {active_rank}. You must declare {active_rank}.")
    unique_ids = list(dict.fromkeys(card_ids or []))
    if not unique_ids:
        raise ValueError("Select at least one card.")
    if len(unique_ids) > len(player["hand"]):
        raise ValueError("You cannot play more cards than you hold.")

    played = []
    for card_id in unique_ids:
        index = next((i for i, card in enumerate(player["hand"]) if card["id"] == card_id), -1)
        if index < 0:
            raise ValueError("One of those cards is not in your hand.")
        played.append(player["hand"].pop(index))

    pending_id = pending_empty_player_id(room)
    if pending_id and pending_id != player["id"]:
        confirm_pending_empty_player(room)

    play_id = random_id(4)
    for card in played:
        table_card = card.copy()
        table_card["ownerId"] = player["id"]
        table_card["playId"] = play_id
        room["centerPile"].append(table_card)

    room["lastPlay"] = {
        "playId": play_id,
        "playerId": player["id"],
        "claimedRank": claimed_rank,
        "cardIds": [card["id"] for card in played],
        "actualRanks": [card.get("claimRank", claim_rank(card["rank"])) for card in played],
    }
    if not active_rank:
        room["activeRank"] = claimed_rank
    room["passedSinceLastPlay"] = []
    room["bluffWindowUntil"] = now_ms() + BLUFF_REACTION_MS
    card_word = "card" if len(played) == 1 else "cards"
    add_log(
        room,
        "play",
        player["id"],
        f"{player['name']} played {len(played)} {card_word} of {claimed_rank}.",
        {"count": len(played), "claimedRank": claimed_rank},
    )
    settle_winners(room)
    if room["status"] == "playing":
        advance_turn(room, player["id"])


def pass_turn(room: dict, player: dict) -> None:
    if room.get("lastPlay") and player["id"] not in room["passedSinceLastPlay"]:
        room["passedSinceLastPlay"].append(player["id"])
    add_log(room, "pass", player["id"], f"{player['name']} passed.")
    advance_turn(room, player["id"])


def final_pass(room: dict, player: dict) -> None:
    if not can_final_pass(room, player["id"]):
        raise ValueError("Final pass is available only after every other active player has passed back to you.")

    confirm_pending_empty_player(room)
    cleared = burn_center_pile(room)
    add_log(room, "final-pass", player["id"], f"{player['name']} declared final pass and put away {cleared} center cards.", {"cleared": cleared})
    settle_winners(room)
    if room["status"] == "playing":
        if player["id"] in room["winners"]:
            advance_turn(room, player["id"])
        else:
            set_current_to(room, player["id"])


def challenge(room: dict, challenger: dict) -> None:
    if not room.get("lastPlay") or not room["centerPile"]:
        raise ValueError("There is no active play to challenge.")
    if room["lastPlay"]["playerId"] == challenger["id"]:
        raise ValueError("You cannot challenge your own play.")

    last_player = player_by_id(room, room["lastPlay"]["playerId"])
    if not last_player:
        raise ValueError("The last player is no longer at the table.")

    challenged_cards = [card for card in room["centerPile"] if card["id"] in room["lastPlay"]["cardIds"]]
    claim_was_true = all(card.get("claimRank", claim_rank(card["rank"])) == room["lastPlay"]["claimedRank"] for card in challenged_cards)
    receiver = challenger if claim_was_true else last_player
    reveal_cards = [public_card(card) for card in challenged_cards]
    received_card_ids = [card["id"] for card in room["centerPile"]]
    received_count = len(received_card_ids)

    for card in room["centerPile"]:
        clean_card = {key: value for key, value in card.items() if key not in {"ownerId", "playId"}}
        receiver["hand"].append(clean_card)
    room["centerPile"] = []
    room["lastPlay"] = None
    room["activeRank"] = None
    room["passedSinceLastPlay"] = []
    room["bluffWindowUntil"] = 0

    if claim_was_true:
        add_log(
            room,
            "challenge-wrong",
            challenger["id"],
            f"{challenger['name']}'s call was wrong. {challenger['name']} takes the center pile.",
            {"receiverId": receiver["id"], "revealCards": reveal_cards, "receivedCardIds": received_card_ids, "receivedCount": received_count},
        )
    else:
        add_log(
            room,
            "challenge-right",
            challenger["id"],
            f"{challenger['name']} caught {last_player['name']}'s bluff. {last_player['name']} takes the center pile.",
            {"receiverId": receiver["id"], "revealCards": reveal_cards, "receivedCardIds": received_card_ids, "receivedCount": received_count},
        )

    settle_winners(room)
    if room["status"] == "playing":
        next_player_id = challenger["id"] if not claim_was_true else last_player["id"]
        if next_player_id in room["winners"]:
            advance_turn(room, next_player_id)
        else:
            set_current_to(room, next_player_id)


class BluffHandler(SimpleHTTPRequestHandler):
    server_version = "BluffUNO/1.0"

    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request body is too large.")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            body = self.read_json()
            with state_lock:
                result = self.handle_api(parsed.path, body)
            self.send_json(HTTPStatus.OK, result)
        except Exception as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def handle_api(self, path: str, body: dict) -> dict:
        if path == "/api/create":
            player = make_player(body.get("name"), True)
            code = room_code()
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
                "createdAt": int(time.time() * 1000),
            }
            rooms[code] = room
            add_log(room, "join", player["id"], f"{player['name']} created the room.")
            return {"room": sanitize_room(room, player["id"])}

        if path == "/api/join":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            if room["status"] == "closed":
                raise ValueError("This room has been closed.")
            if absent_non_winner_named_player(room, body.get("name")):
                raise ValueError("You left this room before ranking and cannot rejoin it.")
            if room["status"] == "playing":
                player = rejoin_ranked_player(room, body.get("name"))
                if not player:
                    raise ValueError("This game has already started. Only players who already ranked may rejoin under their previous name.")
                bump(room)
                return {"room": sanitize_room(room, player["id"])}
            if room["status"] != "lobby":
                raise ValueError("This game has already started.")
            if len(room["players"]) >= 12:
                raise ValueError("This room is full.")
            player = make_player(body.get("name"), False)
            room["players"].append(player)
            add_log(room, "join", player["id"], f"{player['name']} joined the room.")
            bump(room)
            return {"room": sanitize_room(room, player["id"])}

        if path == "/api/state":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            return {"room": sanitize_room(room, body.get("playerId"))}

        if path == "/api/start":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            start_game(room, body.get("playerId"))
            bump(room)
            return {"room": sanitize_room(room, body.get("playerId"))}

        if path == "/api/close":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            close_room(room, body.get("playerId"))
            bump(room)
            return {"room": sanitize_room(room, body.get("playerId"))}

        if path == "/api/leave":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            leave_room(room, body.get("playerId"))
            bump(room)
            return {"room": sanitize_room(room, body.get("playerId"))}

        if path == "/api/action":
            room = get_room(body.get("code"))
            if not room:
                raise ValueError("Room not found.")
            player = player_by_id(room, body.get("playerId"))
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
                challenge(room, player)
            else:
                if bluff_window_active(room) and action_type != "pass":
                    raise ValueError("Wait for the bluff-call window to finish.")
                current = current_player(room)
                if not current or current["id"] != player["id"]:
                    raise ValueError("It is not your turn.")
                if action_type == "play":
                    play_cards(room, player, body.get("cardIds") or [], body.get("claimedRank"))
                elif action_type == "pass":
                    pass_turn(room, player)
                elif action_type == "final-pass":
                    final_pass(room, player)
                else:
                    raise ValueError("Unknown action.")

            bump(room)
            return {"room": sanitize_room(room, player["id"])}

        raise ValueError("Not found.")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/events":
            self.handle_events(parsed)
            return
        self.serve_static(parsed.path)

    def handle_events(self, parsed) -> None:
        query = parse_qs(parsed.query)
        code = (query.get("code", [""])[0] or "").upper()
        player_id = query.get("playerId", [""])[0]

        with state_lock:
            room = get_room(code)
            if not room or not player_by_id(room, player_id):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            client = {"playerId": player_id, "handler": self, "lock": threading.Lock()}
            streams.setdefault(code, []).append(client)
            initial_payload = json.dumps(sanitize_room(room, player_id), separators=(",", ":")).encode()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if not write_sse_payload(client, initial_payload):
            with state_lock:
                streams[code] = [item for item in streams.get(code, []) if item is not client]
            return

        try:
            while True:
                time.sleep(15)
                with client["lock"]:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            with state_lock:
                streams[code] = [item for item in streams.get(code, []) if item is not client]

    def serve_static(self, path: str) -> None:
        requested = "/index.html" if path == "/" else path
        requested = posixpath.normpath(requested).lstrip("/")
        file_path = (PUBLIC_DIR / requested).resolve()
        if PUBLIC_DIR not in file_path.parents and file_path != PUBLIC_DIR:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "application/javascript"
        if file_path.suffix == ".css":
            content_type = "text/css"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    threading.Thread(target=broadcast_worker, name="broadcast-worker", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BluffHandler)
    print(f"Bluff table running at http://localhost:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
