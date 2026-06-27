# Bluff UNO Table

A multiplayer Bluff website using one UNO deck, served by FastAPI with WebSocket room updates.

## Run

Install the server dependencies once:

```sh
python3 -m pip install -r requirements.txt
```

The Redis persistence dependency is the `redis` Python package, included in `requirements.txt`.

Start the server:

```sh
python3 server.py
```

In another terminal, serve the frontend:

```sh
python3 -m http.server 3000 --directory public
```

Then open `http://localhost:3000?backend=http://localhost:8000`. The backend URL is saved in the browser after the first visit.

## Deployment

Deploy the backend and frontend separately:

1. Deploy the FastAPI backend to Railway.
2. Add a Railway Redis database and connect it to the backend.
3. Copy the Railway backend URL into `public/config.js`.
4. Deploy the `public/` folder as the static frontend on Netlify, Vercel, or upload it to InfinityFree.

### Railway Backend

Create a Railway project from this GitHub repository. Use these backend settings if Railway asks for them:

```text
Build Command: pip install -r requirements.txt
Start Command: python3 server.py
```

The backend reads Railway's `PORT` environment variable automatically and starts Uvicorn with WebSocket ping/pong enabled. Keep the service on one running instance unless room state is moved out of process memory.

Add a Redis database to the same Railway project, then make sure the backend service has these variables:

```text
CORS_ALLOW_ORIGINS=https://bluff-uno.netlify.app,https://your-vercel-app.vercel.app
REDIS_URL=redis://your-redis-host:6379
REDIS_ROOM_TTL_SECONDS=7200
KEEPALIVE_INTERVAL_SECONDS=45
```

Railway should provide `REDIS_URL` from the Redis database. Do not set `PORT`; Railway provides it.

`CORS_ALLOW_ORIGINS` defaults to `https://bluff-uno.netlify.app`. Set it explicitly if you deploy the frontend to another domain, including Vercel. Separate multiple frontend origins with commas. Do not use `*` with credentials in production.

`REDIS_URL` enables room persistence. On startup, the backend loads saved rooms from Redis; after every create, join, start, leave, or player action, it saves the changed room back to Redis. Saved rooms expire after `REDIS_ROOM_TTL_SECONDS`, which defaults to 7200 seconds, or 2 hours. Closed rooms and rooms with no present players are deleted from Redis. This lets players resume from their browser-stored room code and player ID after a cold restart while avoiding stale rooms piling up forever.

Each player also gets a recovery code after creating or joining a room. If browser storage is cleared, the player can enter the room code and recovery code on the entry screen to recover the same seat. Anyone with that code can recover that seat, so treat it like a private invite.

Recovery codes are unique within each room. Older saved browser sessions are not cleared automatically; if they reconnect successfully, the backend assigns any missing recovery code and the frontend saves the upgraded session.

Use a persistent Redis-compatible database. On Railway, add a Redis database to the project and reference its `REDIS_URL` in the backend service.

After Railway deploys, copy the backend public URL, for example:

```text
https://your-app.up.railway.app
```

### Frontend Config

Open `public/config.js` and set the backend URL:

```js
window.BLUFF_BACKEND_URL = "https://your-app.up.railway.app";
window.BLUFF_BACKEND_WS_URL = "wss://your-app.up.railway.app";
```

Use your actual Railway URL. Do not include a trailing slash.

The frontend derives WebSocket connections from that backend URL. For a deployed HTTPS backend, it connects to:

```text
wss://your-backend-host/ws?code=ROOM&playerId=PLAYER_ID
```

If your backend host requires an explicit WebSocket URL, set it too:

```js
window.BLUFF_BACKEND_URL = "https://your-app.up.railway.app";
window.BLUFF_BACKEND_WS_URL = "wss://your-app.up.railway.app";
```

### Netlify Frontend

Deploy the frontend from GitHub with these settings:

```text
Base directory: leave empty
Build command: leave empty
Publish directory: public
```

### Vercel Frontend

This repository includes `vercel.json` so Vercel serves only the static frontend from `public/`. That prevents Vercel from detecting the Python backend and returning `{"detail":"Not Found"}` at the site root.

Deploy the same GitHub repository to Vercel with the default root directory. Leave the build command empty. After Vercel assigns a domain, add that exact origin to the Railway backend's `CORS_ALLOW_ORIGINS`, for example:

```text
CORS_ALLOW_ORIGINS=https://bluff-uno.netlify.app,https://your-vercel-app.vercel.app
```

Redeploy the Railway backend after changing that variable.

### InfinityFree Frontend

Upload the contents of the `public/` folder into your site's `htdocs` folder. Upload the files inside `public/`, not the `public` folder itself.

The app exposes `GET /health` for platform health checks. Browser tabs also call `/health` every 60 seconds while the page is open.

For hosts that sleep inactive containers, the browser reports the backend origin to `POST /api/keepalive-origin` when the page loads. The server then uses that backend origin for its background `/health` keepalive loop, so the keepalive URL can follow a changed deployment domain without hardcoding it in `server.py`.

The keepalive pings are not the persistence strategy. Redis is what lets a game resume after the backend sleeps or restarts. Keep the WebSocket ping/pong behavior because it detects broken live connections and helps the browser reconnect cleanly; treat HTTP keepalive as optional warming only.

Optional keepalive environment variables:

```text
KEEPALIVE_URL=https://your-app-url.example/health
PUBLIC_KEEPALIVE_URL=https://your-app-url.example/health
KEEPALIVE_INTERVAL_SECONDS=45
```

`KEEPALIVE_URL` or `PUBLIC_KEEPALIVE_URL` overrides the browser-learned URL if your host supports environment variables. The keepalive loop defaults to 45 seconds and sends one immediate ping when the browser reports the backend origin. It can keep an already-running container warm by making a public `/health` request every interval. If the platform has already put the container to sleep, an outside visitor or external uptime monitor still has to wake it.

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
