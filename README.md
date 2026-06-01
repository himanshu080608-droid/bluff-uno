# Bluff UNO Table

A multiplayer Bluff website using one UNO deck, served by FastAPI with WebSocket room updates.

## Run

Install the server dependencies once:

```sh
python3 -m pip install -r requirements.txt
```

Start the server:

```sh
python3 server.py
```

Then open:

```text
http://localhost:8000
```

## Deployment

The app includes a tracked `fly.toml` deployment config. The Python server reads the host-provided `PORT` environment variable automatically and starts Uvicorn with WebSocket ping/pong enabled.

Keep the app on one running instance unless room state is moved out of process memory.

The app exposes `GET /health` for platform health checks. Browser tabs also call `/health` every 60 seconds while the page is open.

For hosts that sleep inactive containers, the browser reports its public origin to `POST /api/keepalive-origin` when the page loads. The server then uses that public origin for its background `/health` keepalive loop, so the keepalive URL can follow a changed deployment domain without hardcoding it in `server.py`.

Optional keepalive environment variables:

```text
KEEPALIVE_URL=https://your-app-url.example/health
PUBLIC_KEEPALIVE_URL=https://your-app-url.example/health
KEEPALIVE_INTERVAL_SECONDS=45
```

`KEEPALIVE_URL` or `PUBLIC_KEEPALIVE_URL` overrides the browser-learned URL if your host supports environment variables. The keepalive loop defaults to 45 seconds and sends one immediate ping when the browser reports the public origin. It can keep an already-running container warm by making a public `/health` request every interval. If the platform has already put the container to sleep, an outside visitor or external uptime monitor still has to wake it.

Every player joins with a name. A joining player enters the room code first, then chooses the display name shown in the lobby, table order, move log, and turn banner. Any player in the lobby can start the game once at least two players are present. Share the room link or room code with other players before starting.

## Rules Implemented

- One standard UNO deck is shuffled.
- `0` and `Skip` cards keep different faces, but share one bluff rank: `0/Skip`.
- Cards are dealt equally to all players.
- Leftover cards are put away silently.
- Whoever presses Start plays first; the remaining players are shuffled randomly after that player to create the fixed table order.
- The room creator can close the room before the game ends; closing broadcasts the room closure to every player and stops further actions.
- Players can leave before the game ends. If the room creator leaves, host control passes to the earliest still-present player who joined after them.
- If a player who has not ranked yet leaves during the game, their hand is shuffled and dealt equally to remaining unranked players; leftover cards are put away silently.
- If a leave leaves only one active player in the game, the game finishes immediately and the remaining player is ranked.
- If a ranked player leaves, rankings and gameplay are unchanged. That player may rejoin the running game only with the exact same display name.
- The first play into an empty center pile declares the active rank. Later players may play any physical cards as a bluff, but must keep declaring that same active rank until the pile is cleared by a bluff call or final pass.
- On a turn, a player may play selected cards, pass, or use final pass when eligible.
- Any player except the last player who played cards may call bluff while a last play exists, even after later passes.
- After cards are played, bluff calls stay open for 6 seconds before the next player may play cards.
- If the call is correct, the bluffing player takes the center pile and the next turn starts from the challenger.
- If the call is wrong, the challenger takes the center pile and the next turn starts from the challenged player.
- Final pass is normally offered to the last player who played cards when everyone else has passed back to them. If that last player has emptied their hand and is waiting to be ranked, final pass is offered to the next active player instead.
- Other players' card totals are shown only as rough stack graphics, not exact counts.
- A live rankings list shows who finished first, second, and so on as players empty their hands.
- If a player's final cards are still challengeable, that player is skipped for turns but is not ranked yet. The finish is confirmed after a wrong bluff call, after another player makes the next play, or when the next active player uses final pass after everyone else has passed.
