const app = document.querySelector("#app");
const storagePrefix = "bluff-uno-player:";
const lastSessionKey = "bluff-uno-last-session";
const cookiePrefix = "bluffUnoPlayer_";
const lastSessionCookie = "bluffUnoLastSession";

let session = null;
let state = null;
let events = null;
let statePollTimer = null;
let stateRefreshInFlight = false;
let realtimeConnected = false;
let realtimeRetryTimer = null;
let realtimeHeartbeatTimer = null;
let pageKeepaliveTimer = null;
let selected = new Set();
let errorText = "";
let actionSplashQueue = [];
let activeSplashAction = null;
let actionSplashTimer = null;
let receivedPileCardIds = new Set();
let receivedPileStartTimer = null;
let receivedPileTimer = null;
let svgIdCounter = 0;
let serverClockOffsetMs = 0;
let bluffWindowTimer = null;

const PILE_CARD_ANIMATION_MS = 680;
const PILE_CARD_STAGGER_MS = 42;
const PLAY_SPLASH_AFTER_PILE_GAP_MS = 90;
const ACTION_SPLASH_EXTRA_HOLD_MS = 450;
const RECEIVED_PILE_HIGHLIGHT_MS = 3600;
const RECEIVED_PILE_AFTER_REVEAL_GAP_MS = 180;
const BLUFF_WINDOW_RENDER_PAD_MS = 40;
const STATE_POLL_MS = 2500;
const REALTIME_RETRY_MS = 1200;
const REALTIME_HEARTBEAT_MS = 25000;
const PAGE_KEEPALIVE_MS = 60000;
const USE_WEB_SOCKET = true;
const USE_EVENT_STREAM = false;

const REVERSE_ARROW_PATH =
  "M -36,11 L 8,11 L 8,24 L 40,0 L 8,-24 L 8,-11 C -25,-11 -36,-11 -36,11 Z";
const REVERSE_JCURVE_PATH = "M 8,-11 C -25,-11 -36,-11 -36,11";
const REVERSE_ARROWHEAD_PATH = "M 8,24 L 40,0 L 8,-24";
const REVERSE_INNER_WINGS_PATH = "M 8,-24 L 8,-11 M 8,11 L 8,24";

const params = new URLSearchParams(window.location.search);
const initialRoom = (params.get("room") || "").toUpperCase();
let pendingJoinCode = initialRoom;

function loadStoredSession(code) {
  if (!code) return null;
  try {
    const stored = normalizeStoredSession(JSON.parse(localStorage.getItem(storagePrefix + code)));
    if (stored) return stored;
  } catch {
  }
  return loadSessionCookie(cookiePrefix + code);
}

function normalizeStoredSession(stored) {
  if (!stored || !stored.code || !stored.playerId) return null;
  return {
    code: String(stored.code).toUpperCase(),
    playerId: stored.playerId,
    name: stored.name || "",
    updatedAt: stored.updatedAt || 0
  };
}

function loadLastStoredSession() {
  try {
    const stored = normalizeStoredSession(JSON.parse(localStorage.getItem(lastSessionKey)));
    if (stored) return stored;
  } catch {
  }
  return loadSessionCookie(lastSessionCookie);
}

function loadAnyStoredSession() {
  try {
    let best = null;
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key || !key.startsWith(storagePrefix)) continue;
      const stored = normalizeStoredSession(JSON.parse(localStorage.getItem(key)));
      if (!stored) continue;
      if (!best || stored.updatedAt > best.updatedAt) best = stored;
    }
    return best;
  } catch {
    return null;
  }
}

function loadReconnectSession() {
  return loadLastStoredSession() || loadAnyStoredSession();
}

function cookieName(name) {
  return encodeURIComponent(name);
}

function saveSessionCookie(name, value) {
  document.cookie = `${cookieName(name)}=${encodeURIComponent(JSON.stringify(value))}; max-age=2592000; path=/; SameSite=Lax`;
}

function loadSessionCookie(name) {
  const target = `${cookieName(name)}=`;
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(target));
  if (!match) return null;
  try {
    return normalizeStoredSession(JSON.parse(decodeURIComponent(match.slice(target.length))));
  } catch {
    return null;
  }
}

function removeSessionCookie(name) {
  document.cookie = `${cookieName(name)}=; max-age=0; path=/; SameSite=Lax`;
}

function removeStoredSession(code) {
  if (!code) return;
  try {
    localStorage.removeItem(storagePrefix + code);
  } catch {
  }
  removeSessionCookie(cookiePrefix + code);
  const last = loadLastStoredSession();
  if (last && last.code === code) {
    try {
      localStorage.removeItem(lastSessionKey);
    } catch {
    }
    removeSessionCookie(lastSessionCookie);
  }
}

function clearAllStoredSessions() {
  try {
    const keys = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key === lastSessionKey || (key && key.startsWith(storagePrefix))) keys.push(key);
    }
    keys.forEach((key) => localStorage.removeItem(key));
  } catch {
  }

  removeSessionCookie(lastSessionCookie);
  document.cookie.split(";").forEach((part) => {
    const name = part.trim().split("=")[0];
    if (name.startsWith(cookiePrefix)) removeSessionCookie(name);
  });

  session = null;
  state = null;
  pendingJoinCode = "";
  errorText = "";
  const url = new URL(window.location.href);
  url.searchParams.delete("room");
  window.history.replaceState({}, "", url);
}

function saveSession(room) {
  if (!room || !room.you) return;
  session = { code: room.code, playerId: room.you.id, name: room.you.name, updatedAt: Date.now() };
  try {
    localStorage.setItem(storagePrefix + room.code, JSON.stringify(session));
    localStorage.setItem(lastSessionKey, JSON.stringify(session));
  } catch {
  }
  saveSessionCookie(cookiePrefix + room.code, session);
  saveSessionCookie(lastSessionCookie, session);
  const url = new URL(window.location.href);
  url.searchParams.set("room", room.code);
  window.history.replaceState({}, "", url);
}

function colorValue(color) {
  return {
    red: "var(--red)",
    yellow: "var(--yellow)",
    green: "var(--green)",
    blue: "var(--blue)",
    wild: "var(--wild)"
  }[color] || "var(--wild)";
}

function uniqueSvgId(prefix) {
  svgIdCounter += 1;
  return `${prefix}-${svgIdCounter}`;
}

function jCurvePoint(t) {
  return [8 - 99 * t + 66 * t * t - 11 * t * t * t, -11 + 22 * t * t * t];
}

function taperedJCurveMarkup(maxWidth) {
  const segments = [];
  const count = 16;
  for (let index = 0; index < count; index += 1) {
    const t0 = index / count;
    const t1 = (index + 1) / count;
    const [x0, y0] = jCurvePoint(t0);
    const [x1, y1] = jCurvePoint(t1);
    segments.push(
      `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="black" stroke-width="${t1 * maxWidth}" stroke-linecap="round"></line>`
    );
  }
  return segments.join("");
}

function rankText(rank) {
  return {
    "Draw Two": "+2",
    "Wild Draw Four": "+4",
    Reverse: "Rev",
    Skip: "Skip"
  }[rank] || rank;
}

function cardFace(card) {
  const label = escapeHtml(rankText(card.rank));
  if (card.rank === "Skip") {
    return `<span class="card-face action-face"><span class="action-symbol skip-symbol" aria-label="Skip">${skipIcon("skip-center")}</span></span>`;
  }
  if (card.rank === "Reverse") {
    return `<span class="card-face action-face"><span class="action-symbol reverse-symbol" aria-label="Reverse">${reverseIcon("reverse-center")}</span></span>`;
  }
  if (card.rank === "Draw Two") {
    return '<span class="card-face action-face"><span class="action-symbol draw-symbol draw-two-symbol" aria-label="Draw Two"><span class="mini-card one"></span><span class="mini-card two"></span></span></span>';
  }
  if (card.rank === "Wild") {
    return `<span class="card-face action-face"><span class="action-symbol wild-symbol" aria-label="Wild">${wildIcon("wild-center")}</span></span>`;
  }
  if (card.rank === "Wild Draw Four") {
    return '<span class="card-face action-face"><span class="action-symbol wild-draw-symbol" aria-label="Wild Draw Four"><span class="mini-card green"></span><span class="mini-card blue"></span><span class="mini-card red"></span><span class="mini-card yellow"></span></span></span>';
  }
  return `<span class="card-face">${label}</span>`;
}

function reverseIcon(className) {
  const innerClipId = uniqueSvgId("reverse-inner-wing-clip");
  const headClipId = uniqueSvgId("reverse-head-clip");
  const arrowOne = 'translate(6.2, -21.8) rotate(-45)';
  const arrowTwo = 'translate(-6.2, 21.8) rotate(135)';
  return `
    <svg class="reverse-icon ${className}" viewBox="-72 -72 144 144" aria-hidden="true" focusable="false">
      <defs>
        <clipPath id="${innerClipId}">
          <polygon points="-100,-100 8,-24 40,0 8,24 -100,100"></polygon>
        </clipPath>
        <clipPath id="${headClipId}">
          <polygon points="8,-24 100,-24 100,24 8,24"></polygon>
        </clipPath>
      </defs>
      <g transform="${arrowTwo}">
        <path d="${REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="9.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#${headClipId})"></path>
      </g>
      <g transform="${arrowOne}">
        ${taperedJCurveMarkup(12)}
        <path d="${REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="9.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#${innerClipId})"></path>
      </g>
      <g transform="${arrowTwo}">
        <path d="${REVERSE_ARROW_PATH}" fill="white" stroke="none"></path>
        <path d="M -36,11 L 8,11" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
        <path d="${REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="butt" clip-path="url(#${headClipId})"></path>
        <path d="${REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="butt"></path>
        <path d="${REVERSE_JCURVE_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linecap="round"></path>
      </g>
      <g transform="${arrowOne}">
        <path d="${REVERSE_ARROW_PATH}" fill="white" stroke="none"></path>
        <path d="M -36,11 L 8,11" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
        <path d="${REVERSE_INNER_WINGS_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="butt"></path>
        <path d="${REVERSE_JCURVE_PATH}" fill="none" stroke="black" stroke-width="3.5" stroke-linecap="round"></path>
        <path d="${REVERSE_ARROWHEAD_PATH}" fill="none" stroke="black" stroke-width="2.5" stroke-linejoin="miter" stroke-linecap="butt"></path>
      </g>
    </svg>
  `;
}

function skipIcon(className) {
  return `
    <svg class="skip-icon ${className}" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
      <g class="skip-shadow" transform="translate(-6 6)">
        <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
        <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
      </g>
      <g class="skip-shadow" transform="translate(-3 3)">
        <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
        <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
      </g>
      <g class="skip-outline">
        <path d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
        <path d="M26 66 L66 26 L75 35 L35 75 Z"></path>
      </g>
      <g class="skip-fill">
        <path class="skip-ring" d="M50 12a38 38 0 1 0 0 76a38 38 0 0 0 0-76zm0 15a23 23 0 1 1 0 46a23 23 0 0 1 0-46z" fill-rule="evenodd"></path>
        <path class="skip-bar" d="M26 66 L66 26 L75 35 L35 75 Z"></path>
      </g>
    </svg>
  `;
}

function wildIcon(className) {
  const clipId = uniqueSvgId("wild-oval-clip");
  return `
    <svg class="wild-icon ${className}" viewBox="0 0 100 150" aria-hidden="true" focusable="false">
      <defs>
        <clipPath id="${clipId}">
          <ellipse cx="50" cy="71" rx="33" ry="60" transform="rotate(28 50 71)"></ellipse>
        </clipPath>
      </defs>
      <g clip-path="url(#${clipId})">
        <polygon points="-12,-10 80,-10 50,71 -12,71" fill="var(--red)"></polygon>
        <polygon points="80,-10 112,-10 112,71 50,71" fill="var(--blue)"></polygon>
        <polygon points="-12,71 50,71 17,160 -12,160" fill="var(--yellow)"></polygon>
        <polygon points="50,71 112,71 112,160 17,160" fill="var(--green)"></polygon>
      </g>
      <ellipse cx="50" cy="71" rx="33" ry="60" transform="rotate(28 50 71)" fill="none" stroke="#f2f2f2" stroke-width="3"></ellipse>
    </svg>
  `;
}

function cardRankClass(card) {
  return `rank-${String(card.rank).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;
}

function cardCorner(card, position) {
  if (card.rank === "Reverse") {
    return `<span class="card-corner icon-corner ${position}">${reverseIcon("reverse-corner")}</span>`;
  }
  if (card.rank === "Skip") {
    return `<span class="card-corner icon-corner ${position}">${skipIcon("skip-corner")}</span>`;
  }
  if (card.rank === "Wild") {
    return `<span class="card-corner icon-corner wild-corner-wrap ${position}">${wildIcon("wild-corner-mark")}</span>`;
  }
  if (card.rank === "Draw Two" || card.rank === "Wild Draw Four") {
    return `<span class="card-corner text-corner ${position}">${escapeHtml(rankText(card.rank))}</span>`;
  }
  return `<span class="card-corner ${position}">${escapeHtml(rankText(card.rank))}</span>`;
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const json = await response.json();
  if (!response.ok || json.error) throw new Error(json.error || "Request failed.");
  if (json.room) {
    applyRoomState(json.room, true);
  }
  return json;
}

function serverNowMs() {
  return Date.now() - serverClockOffsetMs;
}

function bluffWindowRemainingMs() {
  if (!state || !state.bluffWindow || !state.bluffWindow.until) return 0;
  return Math.max(0, state.bluffWindow.until - serverNowMs());
}

function isBluffWindowActive() {
  return Boolean(
    state &&
      state.status === "playing" &&
      state.centerPile &&
      state.centerPile.lastPlay &&
      state.bluffWindow &&
      bluffWindowRemainingMs() > 0
  );
}

function canUseTurnActions() {
  return Boolean(state && state.canAct && !isBluffWindowActive());
}

function canUsePassAction() {
  return Boolean(state && state.canAct && (!isBluffWindowActive() || !state.canFinalPass));
}

function scheduleBluffWindowRender() {
  if (bluffWindowTimer) {
    window.clearTimeout(bluffWindowTimer);
    bluffWindowTimer = null;
  }
  const remaining = bluffWindowRemainingMs();
  if (remaining <= 0) return;
  bluffWindowTimer = window.setTimeout(() => {
    bluffWindowTimer = null;
    render();
  }, remaining + BLUFF_WINDOW_RENDER_PAD_MS);
}

function applyRoomState(room, forceRender = false) {
  const previousVersion = state ? state.version : null;
  const previousStatus = state ? state.status : null;
  const previousBluffWindowActive = isBluffWindowActive();
  const previousActionIds = state ? new Set(state.actions.map((action) => action.id)) : null;
  const newActions = previousActionIds
    ? room.actions
        .filter((action) => !previousActionIds.has(action.id))
        .sort((first, second) => first.id - second.id)
    : [];
  if (Number.isFinite(room.serverNow)) serverClockOffsetMs = Date.now() - room.serverNow;
  state = room;
  saveSession(room);
  connectEvents();
  startStatePolling();
  selected = new Set([...selected].filter((id) => state.you && state.you.hand.some((card) => card.id === id)));
  errorText = "";
  if (room.status === "closed" || room.status === "finished") {
    removeStoredSession(room.code);
  }
  if (room.you && !room.you.present) {
    clearLocalSession(room.code);
    render();
    return;
  }

  trackReceivedPile(newActions);
  enqueueActionSplashes(newActions);
  scheduleBluffWindowRender();
  const bluffWindowChanged = previousBluffWindowActive !== isBluffWindowActive();

  if (forceRender || previousVersion !== state.version || previousStatus !== state.status || bluffWindowChanged) {
    render();
  }
}

function trackReceivedPile(actions) {
  if (!state.you || !actions.length) return;
  const receivedAction = [...actions]
    .reverse()
    .find(
      (action) =>
        action.detail &&
        action.detail.receiverId === state.you.id &&
        Array.isArray(action.detail.receivedCardIds) &&
        action.detail.receivedCardIds.length
    );
  if (!receivedAction) return;

  clearReceivedPileTimer();
  receivedPileCardIds.clear();

  const receivedIds = new Set(receivedAction.detail.receivedCardIds);
  const delay = revealDuration(receivedAction) + RECEIVED_PILE_AFTER_REVEAL_GAP_MS;
  receivedPileStartTimer = window.setTimeout(() => {
    receivedPileCardIds = receivedIds;
    receivedPileStartTimer = null;
    render();
    scrollHandToEnd();
    receivedPileTimer = window.setTimeout(() => {
      receivedPileCardIds.clear();
      receivedPileTimer = null;
      document.querySelectorAll(".uno-card.received-pile").forEach((card) => card.classList.remove("received-pile"));
    }, RECEIVED_PILE_HIGHLIGHT_MS);
  }, delay);
}

function scrollHandToEnd() {
  window.requestAnimationFrame(() => {
    const hand = document.querySelector(".hand");
    if (!hand) return;
    try {
      hand.scrollTo({ left: hand.scrollWidth, behavior: "smooth" });
    } catch {
      hand.scrollLeft = hand.scrollWidth;
    }
  });
}

function clearReceivedPileTimer() {
  if (receivedPileStartTimer) {
    window.clearTimeout(receivedPileStartTimer);
    receivedPileStartTimer = null;
  }
  if (receivedPileTimer) {
    window.clearTimeout(receivedPileTimer);
    receivedPileTimer = null;
  }
}

function enqueueActionSplashes(actions) {
  if (!actions.length) return;
  const allPasses = actions.every((action) => action.type === "pass");
  const nextActions = allPasses ? [actions[actions.length - 1]] : actions;

  const hasInterruptingAction = nextActions.some(isInterruptibleSplash);
  actionSplashQueue = actionSplashQueue.filter((action) => !isInterruptibleSplash(action));
  if (activeSplashAction && hasInterruptingAction && isInterruptibleSplash(activeSplashAction)) {
    clearActionSplashTimer();
    activeSplashAction = null;
  }

  actionSplashQueue.push(...nextActions);
  actionSplashQueue = trimActionSplashQueue(actionSplashQueue);
  showNextActionSplash();
}

function showNextActionSplash() {
  if (activeSplashAction || !actionSplashQueue.length) return;
  activeSplashAction = actionSplashQueue.shift();
  render();
  clearActionSplashTimer();
  actionSplashTimer = window.setTimeout(() => {
    activeSplashAction = null;
    actionSplashTimer = null;
    if (actionSplashQueue.length) showNextActionSplash();
    else render();
  }, actionSplashDuration(activeSplashAction));
}

function clearActionSplashTimer() {
  if (!actionSplashTimer) return;
  window.clearTimeout(actionSplashTimer);
  actionSplashTimer = null;
}

function trimActionSplashQueue(queue) {
  const limit = 4;
  const trimmed = [...queue];
  while (trimmed.length > limit) {
    const removableIndex = trimmed.findIndex((action) => action.type === "pass");
    trimmed.splice(removableIndex >= 0 ? removableIndex : 0, 1);
  }
  return trimmed;
}

function revealDuration(action) {
  if (!action || !action.detail || !Array.isArray(action.detail.revealCards)) return 0;
  const total = action.detail.revealCards.length;
  if (!total) return 0;
  return Math.min(6500, Math.max(2800, 2200 + total * 240));
}

function pileThrowDuration(action) {
  if (!action || action.type !== "play") return 0;
  const playedCount = Number(action.detail && action.detail.count ? action.detail.count : 0);
  if (!playedCount) return 0;
  const visiblePileCount = state ? Math.min(6, Math.max(state.centerPile.count, 1)) : playedCount;
  const animatedCount = Math.min(visiblePileCount, Math.max(0, playedCount));
  if (!animatedCount) return 0;
  return PILE_CARD_ANIMATION_MS + (animatedCount - 1) * PILE_CARD_STAGGER_MS + PLAY_SPLASH_AFTER_PILE_GAP_MS;
}

function actionSplashDelay(action) {
  return pileThrowDuration(action);
}

function actionSplashVisibleDuration(action) {
  const revealMs = revealDuration(action);
  if (revealMs) return revealMs + 180;
  if (!action) return 0;
  if (action.type === "pass") return 900 + ACTION_SPLASH_EXTRA_HOLD_MS;
  if (action.type === "play") return 1350 + ACTION_SPLASH_EXTRA_HOLD_MS;
  if (action.type === "win" || action.type === "finish" || action.type === "close") return 1900 + ACTION_SPLASH_EXTRA_HOLD_MS;
  return 1550 + ACTION_SPLASH_EXTRA_HOLD_MS;
}

function actionSplashDuration(action) {
  return actionSplashDelay(action) + actionSplashVisibleDuration(action);
}

function actionHasReveal(action) {
  return Boolean(action && action.detail && Array.isArray(action.detail.revealCards) && action.detail.revealCards.length);
}

function isInterruptibleSplash(action) {
  return action && action.type !== "finish" && action.type !== "close" && !actionHasReveal(action);
}

function connectEvents() {
  if (USE_WEB_SOCKET) {
    connectWebSocket();
    return;
  }
  if (!USE_EVENT_STREAM) return;
  if (!session || events) return;
  events = new EventSource(`/events?code=${encodeURIComponent(session.code)}&playerId=${encodeURIComponent(session.playerId)}`);
  events.addEventListener("state", (event) => {
    applyRoomState(JSON.parse(event.data));
  });
  events.onerror = () => {
    errorText = "Live connection is retrying.";
    render();
  };
}

function connectWebSocket() {
  if (!session || events) return;
  if (realtimeRetryTimer) {
    window.clearTimeout(realtimeRetryTimer);
    realtimeRetryTimer = null;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/ws?code=${encodeURIComponent(session.code)}&playerId=${encodeURIComponent(session.playerId)}`;
  events = new WebSocket(url);

  events.addEventListener("open", () => {
    realtimeConnected = true;
    errorText = "";
    startRealtimeHeartbeat();
    sendRealtimeHeartbeat();
  });

  events.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "state" && message.room) {
        applyRoomState(message.room);
      } else if (message.type === "error" && message.error) {
        setError(message.error);
      }
    } catch {
    }
  });

  events.addEventListener("close", scheduleRealtimeReconnect);
  events.addEventListener("error", () => {
    if (events) events.close();
  });
}

function startRealtimeHeartbeat() {
  stopRealtimeHeartbeat();
  realtimeHeartbeatTimer = window.setInterval(sendRealtimeHeartbeat, REALTIME_HEARTBEAT_MS);
}

function sendRealtimeHeartbeat() {
  if (!events || events.readyState !== WebSocket.OPEN) return;
  events.send(JSON.stringify({ type: "ping", at: Date.now() }));
}

function stopRealtimeHeartbeat() {
  if (!realtimeHeartbeatTimer) return;
  window.clearInterval(realtimeHeartbeatTimer);
  realtimeHeartbeatTimer = null;
}

function startPageKeepalive() {
  if (pageKeepaliveTimer) return;
  reportKeepaliveOrigin();
  sendPageKeepalive();
  pageKeepaliveTimer = window.setInterval(sendPageKeepalive, PAGE_KEEPALIVE_MS);
}

function reportKeepaliveOrigin() {
  fetch("/api/keepalive-origin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ origin: window.location.origin })
  }).catch(() => {
    // The normal page keepalive will keep retrying even if this one misses.
  });
}

function sendPageKeepalive() {
  fetch("/health", {
    method: "GET",
    cache: "no-store",
    headers: { "Cache-Control": "no-cache" }
  }).catch(() => {
    // A missed keepalive should not interrupt gameplay or the entry screen.
  });
}

function scheduleRealtimeReconnect() {
  realtimeConnected = false;
  stopRealtimeHeartbeat();
  events = null;
  if (!session || realtimeRetryTimer) return;
  realtimeRetryTimer = window.setTimeout(() => {
    realtimeRetryTimer = null;
    connectEvents();
  }, REALTIME_RETRY_MS);
}

function startStatePolling() {
  if (statePollTimer || !session) return;
  statePollTimer = window.setInterval(refreshState, STATE_POLL_MS);
}

async function refreshState(force = false) {
  if (!session || stateRefreshInFlight || (realtimeConnected && !force)) return;
  stateRefreshInFlight = true;
  try {
    const response = await fetch("/api/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: session.code, playerId: session.playerId })
    });
    const json = await response.json();
    if (response.ok && json.room) {
      applyRoomState(json.room);
    }
  } catch {
    // Poll again on the next tick; transient misses should not disturb play.
  } finally {
    stateRefreshInFlight = false;
  }
}

window.addEventListener("focus", () => refreshState(true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshState(true);
});

function setError(message) {
  errorText = message;
  render();
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function entryArt() {
  return `
    <section class="entry-art" aria-hidden="true">
        <div class="table-preview">
          <div class="preview-card" data-rank="7" style="--x: 44%; --y: 36%; --r: -11deg; --c: var(--red)"></div>
          <div class="preview-card" data-rank="+2" style="--x: 51%; --y: 38%; --r: 10deg; --c: var(--blue)"></div>
          <div class="preview-card" data-rank="W" style="--x: 48%; --y: 28%; --r: 4deg; --c: var(--wild)"></div>
          <div class="preview-card" data-rank="5" style="--x: 20%; --y: 18%; --r: -24deg; --c: var(--green)"></div>
          <div class="preview-card" data-rank="9" style="--x: 74%; --y: 58%; --r: 21deg; --c: var(--yellow)"></div>
        </div>
      </section>`;
}

function renderJoinNameEntry() {
  app.innerHTML = `
    <main class="entry">
      <section class="entry-panel">
        <h1 class="brand">Join ${escapeHtml(pendingJoinCode)}</h1>
        <p class="subtitle">Choose the name that other players will see in the lobby, table order, move log, and turn banner.</p>
        <div class="form-stack">
          <label class="input-row">
            <span class="field-label">Display name</span>
            <input id="joinName" class="text-input" maxlength="24" placeholder="Himanshu" autofocus>
          </label>
          <div class="split">
            <button id="confirmJoin" class="primary" type="button">Join Room</button>
            <button id="changeCode" class="secondary" type="button">Change Code</button>
          </div>
          <div class="notice">You will join room ${escapeHtml(pendingJoinCode)} with this name.</div>
          <div class="error ${errorText ? "visible" : ""}">${escapeHtml(errorText)}</div>
        </div>
      </section>
      ${entryArt()}
    </main>
  `;

  document.querySelector("#confirmJoin").addEventListener("click", async () => {
    const name = document.querySelector("#joinName").value.trim();
    if (!name) {
      setError("Choose a display name before joining.");
      return;
    }
    try {
      await api("/api/join", {
        name,
        code: pendingJoinCode
      });
    } catch (error) {
      setError(error.message);
    }
  });

  document.querySelector("#changeCode").addEventListener("click", () => {
    pendingJoinCode = "";
    errorText = "";
    render();
  });
}

function renderEntry() {
  const roomStored = loadStoredSession(initialRoom);
  const reconnectSession = roomStored || (!initialRoom ? loadReconnectSession() : null);
  const hasReconnect = Boolean(reconnectSession);
  const reconnectLabel = reconnectSession ? `Reconnect to ${escapeHtml(reconnectSession.code)}` : "Reconnect";
  if (pendingJoinCode && !roomStored) {
    renderJoinNameEntry();
    return;
  }

  app.innerHTML = `
    <main class="entry">
      <section class="entry-panel">
        <h1 class="brand">Bluff UNO Table</h1>
        <p class="subtitle">Create a room, share the code, and play Bluff with one UNO deck. The first claim into a clear pile can be any rank, passes are legal, and the last play can still be challenged after a run of passes.</p>
        <div class="form-stack">
          <label class="input-row">
            <span class="field-label">Your name</span>
            <input id="name" class="text-input" maxlength="24" placeholder="Himanshu">
          </label>
          <div class="split">
            <button id="create" class="primary" type="button" ${hasReconnect ? "disabled" : ""}>Create Room</button>
            <button id="reconnect" class="secondary" type="button" ${hasReconnect ? "" : "disabled"}>${reconnectLabel}</button>
          </div>
          ${
            hasReconnect
              ? '<button id="clearSessions" class="secondary subtle" type="button">Clear saved rooms</button>'
              : ""
          }
          <label class="input-row">
            <span class="field-label">Room code</span>
            <input id="code" class="text-input" maxlength="8" placeholder="ABCDE" value="${escapeHtml(initialRoom)}" ${hasReconnect ? "disabled" : ""}>
          </label>
          <button id="join" class="secondary" type="button" ${hasReconnect ? "disabled" : ""}>Continue to Name</button>
          <div class="notice">${
            hasReconnect
              ? `Reconnect to ${escapeHtml(reconnectSession.code)} before creating or joining another room.`
              : "Enter a room code first. Then choose the name everyone will see during the game."
          }</div>
          <div class="error ${errorText ? "visible" : ""}">${escapeHtml(errorText)}</div>
        </div>
      </section>
      ${entryArt()}
    </main>
  `;

  document.querySelector("#create").addEventListener("click", async () => {
    const name = document.querySelector("#name").value.trim();
    if (!name) {
      setError("Choose a display name before creating a room.");
      return;
    }
    try {
      await api("/api/create", { name });
    } catch (error) {
      setError(error.message);
    }
  });

  document.querySelector("#join").addEventListener("click", async () => {
    const code = document.querySelector("#code").value.trim().toUpperCase();
    if (!code) {
      setError("Enter a room code first.");
      return;
    }
    pendingJoinCode = code;
    errorText = "";
    render();
  });

  document.querySelector("#reconnect").addEventListener("click", async () => {
    if (!reconnectSession) return;
    try {
      session = reconnectSession;
      await api("/api/state", { code: reconnectSession.code, playerId: reconnectSession.playerId });
    } catch (error) {
      session = null;
      setError(error.message);
    }
  });

  const clearSessionsButton = document.querySelector("#clearSessions");
  if (clearSessionsButton) {
    clearSessionsButton.addEventListener("click", () => {
      if (!window.confirm("Clear saved room sessions on this browser?")) return;
      clearAllStoredSessions();
      render();
    });
  }
}

function renderLobby() {
  const players = state.players
    .map(
      (player) => `
        <div class="lobby-player">
          <span>${escapeHtml(player.name)}${player.isYou ? " (you)" : ""}</span>
          ${player.host ? '<span class="tag">host</span>' : ""}
        </div>
      `
    )
    .join("");

  app.innerHTML = `
    <main class="entry">
      <section class="entry-panel">
        <h1 class="brand">Room ${escapeHtml(state.code)}</h1>
        <p class="subtitle">Share this room code or the current browser link with players before starting. Whoever presses Start plays first, then the server randomizes the rest of the table order.</p>
        <div class="lobby-list">${players}</div>
        <div class="lobby-actions">
          <button id="start" class="primary" type="button" ${state.canStart ? "" : "disabled"}>Start Game</button>
          <button id="leaveRoom" class="secondary" type="button" ${state.canLeave ? "" : "disabled"}>Leave Room</button>
          ${state.you && state.you.host ? `<button id="closeRoom" class="danger" type="button" ${state.canClose ? "" : "disabled"}>Close Room</button>` : ""}
        </div>
        <div class="notice">${state.canStart ? "Ready when you are." : "Waiting for at least two named players."}</div>
        <div class="error ${errorText ? "visible" : ""}">${escapeHtml(errorText)}</div>
      </section>
      <section class="entry-art" aria-hidden="true">
        <div class="table-preview">
          <div class="preview-card" data-rank="3" style="--x: 40%; --y: 34%; --r: -12deg; --c: var(--blue)"></div>
          <div class="preview-card" data-rank="3" style="--x: 48%; --y: 35%; --r: 8deg; --c: var(--red)"></div>
          <div class="preview-card" data-rank="3" style="--x: 55%; --y: 31%; --r: 18deg; --c: var(--green)"></div>
        </div>
      </section>
    </main>
  `;

  document.querySelector("#start").addEventListener("click", async () => {
    try {
      await api("/api/start", { code: session.code, playerId: session.playerId });
    } catch (error) {
      setError(error.message);
    }
  });

  const closeButton = document.querySelector("#closeRoom");
  if (closeButton) closeButton.addEventListener("click", closeRoom);
  document.querySelector("#leaveRoom").addEventListener("click", leaveRoom);
}

function renderClosedRoom() {
  const closer = state.actions.find((action) => action.type === "close");
  app.innerHTML = `
    <main class="entry">
      <section class="entry-panel">
        <h1 class="brand">Room Closed</h1>
        <p class="subtitle">${escapeHtml(closer ? closer.text : "The room creator closed this room.")}</p>
        <div class="lobby-list">
          ${state.players
            .map(
              (player) => `
                <div class="lobby-player">
                  <span>${escapeHtml(player.name)}${player.isYou ? " (you)" : ""}</span>
                  ${player.host ? '<span class="tag">creator</span>' : ""}
                </div>
              `
            )
            .join("")}
        </div>
        <button id="leaveClosed" class="primary" type="button">Back to Home</button>
      </section>
      ${entryArt()}
    </main>
  `;

  document.querySelector("#leaveClosed").addEventListener("click", () => {
    clearLocalSession(session ? session.code : state.code);
    render();
  });
}

function renderStack(band) {
  const cards = Array.from({ length: band.bars })
    .map((_, index) => `<span class="stack-card" style="--i: ${index}"></span>`)
    .join("");
  return `<div class="card-stack ${band.className}">${cards}<span class="stack-label">${escapeHtml(band.label)}</span></div>`;
}

function renderPlayers() {
  return state.players
    .map(
      (player, index) => `
        <article class="player-row ${player.isCurrent ? "current" : ""} ${player.isWinner ? "winner" : ""}">
          <div class="player-meta">
            <span class="player-name">${index + 1}. ${escapeHtml(player.name)}${player.isYou ? " (you)" : ""}</span>
            <span class="tag">${player.isWinner ? "out" : player.host ? "host" : player.isCurrent ? "turn" : "table"}</span>
          </div>
          ${renderStack(player.cardBand)}
        </article>
      `
    )
    .join("");
}

function ordinal(position) {
  const lastTwo = position % 100;
  if (lastTwo >= 11 && lastTwo <= 13) return `${position}th`;
  const last = position % 10;
  if (last === 1) return `${position}st`;
  if (last === 2) return `${position}nd`;
  if (last === 3) return `${position}rd`;
  return `${position}th`;
}

function renderRankings() {
  if (!state.winners.length) {
    return '<div class="ranking-empty">No finishes yet</div>';
  }

  return state.winners
    .map(
      (winner, index) => `
        <div class="ranking-row">
          <span class="ranking-place">${ordinal(index + 1)}</span>
          <span class="ranking-name">${escapeHtml(winner.name)}</span>
        </div>
      `
    )
    .join("");
}

function renderPile(action) {
  const count = state.centerPile.count;
  const last = state.centerPile.lastPlay;
  const visibleCount = Math.min(6, Math.max(count, 1));
  const playedCount = action && action.type === "play" ? Number(action.detail && action.detail.count ? action.detail.count : 0) : 0;
  const animatedCount = Math.min(visibleCount, Math.max(0, playedCount));
  const firstAnimatedIndex = visibleCount - animatedCount;
  const pileCards = Array.from({ length: visibleCount })
    .map((_, index) => {
      const r = [-10, 7, -2, 13, -15, 3][index] || 0;
      const x = [-8, 8, 0, 16, -15, 4][index] || 0;
      const y = [0, 5, -4, 10, 9, 2][index] || 0;
      const incoming = index >= firstAnimatedIndex && animatedCount > 0;
      const animationIndex = incoming ? index - firstAnimatedIndex : 0;
      return `<span class="pile-card ${incoming ? "incoming" : ""}" style="--i: ${index}; --a: ${animationIndex}; --r: ${r}deg; --x: ${x}px; --y: ${y}px"></span>`;
    })
    .join("");

  return `
    <div class="center-zone">
      <div class="pile-graphic">${count ? pileCards : ""}</div>
      <div class="pile-count">${count ? "Center pile" : "Center clear"}</div>
      <div class="claim">
        ${
          last
            ? `<span>${escapeHtml(last.playerName)} claimed</span><strong>${last.count} ${last.count === 1 ? "card" : "cards"} of ${escapeHtml(last.claimedRank)}</strong>`
            : '<strong>No active claim</strong><span>The next player may declare any rank.</span>'
        }
      </div>
    </div>
  `;
}

function renderActions() {
  return state.actions
    .map((action) => {
      return `
        <article class="log-item">
          <div class="log-text">${escapeHtml(action.text)}</div>
        </article>
      `;
    })
    .join("");
}

function renderReveal(action) {
  if (!action || !action.detail || !Array.isArray(action.detail.revealCards) || !action.detail.revealCards.length) {
    return "";
  }

  const duration = revealDuration(action);
  const cards = action.detail.revealCards
    .map(
      (card, index) => {
        return `
        <div class="uno-card reveal-card color-${escapeHtml(card.color)} ${cardRankClass(card)}" style="--card-color: ${colorValue(card.color)}; --i: ${index}">
          ${cardCorner(card, "top")}
          ${cardFace(card)}
          ${cardCorner(card, "bottom")}
        </div>
      `;
      }
    )
    .join("");

  return `
    <div class="reveal-showcase" aria-live="polite" style="--reveal-duration: ${duration}ms">
      <div class="reveal-strip">${cards}</div>
    </div>
  `;
}

function renderHand() {
  if (!state.you || state.you.hand.length === 0) {
    return '<div class="empty-state">Your hand is empty.</div>';
  }

  return state.you.hand
    .map(
      (card) => `
        <button class="uno-card color-${escapeHtml(card.color)} ${cardRankClass(card)} ${selected.has(card.id) ? "selected" : ""} ${receivedPileCardIds.has(card.id) ? "received-pile" : ""}" type="button" data-card-id="${escapeHtml(card.id)}" style="--card-color: ${colorValue(card.color)}">
          ${cardCorner(card, "top")}
          ${cardFace(card)}
          ${cardCorner(card, "bottom")}
        </button>
      `
    )
    .join("");
}

function renderControls() {
  const availableRanks = state.activeRank ? [state.activeRank] : state.ranks;
  const rankOptions = availableRanks.map((rank) => `<option value="${escapeHtml(rank)}">${escapeHtml(rank)}</option>`).join("");
  const rankPrompt = state.activeRank ? "" : '<option value="" selected>Choose rank to declare</option>';
  const isTurn = canUseTurnActions();
  const bluffWindowActive = isBluffWindowActive();
  const bluffWindowSeconds = Math.max(1, Math.ceil(bluffWindowRemainingMs() / 1000));
  const passAction = state.canFinalPass ? "final-pass" : "pass";
  const passLabel = state.canFinalPass ? "Final pass" : "Pass";
  const canPass = canUsePassAction();
  return `
    <div class="controls">
      <label class="input-row">
        <span class="field-label">Declare rank</span>
        <select id="rank" class="select-input" ${isTurn ? "" : "disabled"}>
          ${rankPrompt}
          ${rankOptions}
        </select>
      </label>
      <div class="selected-count">${selected.size} selected</div>
      <button id="play" class="primary" type="button" disabled>Play selected</button>
      <button id="challenge" class="danger" type="button" ${state.canChallenge ? "" : "disabled"}>Call bluff</button>
      <button id="pass" class="secondary" type="button" data-action="${passAction}" ${canPass ? "" : "disabled"}>${passLabel}</button>
      ${
        bluffWindowActive
          ? `<div class="reaction-status">Bluff calls are open for ${bluffWindowSeconds}s. The current player may pass, but play unlocks after this window.</div>`
          : ""
      }
    </div>
  `;
}

function updateSelectionControls() {
  const selectedCount = document.querySelector(".selected-count");
  if (selectedCount) selectedCount.textContent = `${selected.size} selected`;

  const playButton = document.querySelector("#play");
  const rankSelect = document.querySelector("#rank");
  const hasDeclaredRank = Boolean(rankSelect && rankSelect.value);
  if (playButton) playButton.disabled = !(canUseTurnActions() && selected.size && hasDeclaredRank);

  document.querySelectorAll("[data-card-id]").forEach((button) => {
    button.classList.toggle("selected", selected.has(button.dataset.cardId));
  });
}

function captureGameScrollPositions() {
  return {
    handLeft: document.querySelector(".hand")?.scrollLeft ?? null,
    logsTop: document.querySelector(".log-list")?.scrollTop ?? null,
    playersTop: document.querySelector(".player-order")?.scrollTop ?? null,
    rankingsTop: document.querySelector(".ranking-list")?.scrollTop ?? null
  };
}

function restoreGameScrollPositions(positions) {
  window.requestAnimationFrame(() => {
    const hand = document.querySelector(".hand");
    const logs = document.querySelector(".log-list");
    const players = document.querySelector(".player-order");
    const rankings = document.querySelector(".ranking-list");
    if (hand && positions.handLeft !== null) hand.scrollLeft = positions.handLeft;
    if (logs && positions.logsTop !== null) logs.scrollTop = positions.logsTop;
    if (players && positions.playersTop !== null) players.scrollTop = positions.playersTop;
    if (rankings && positions.rankingsTop !== null) rankings.scrollTop = positions.rankingsTop;
  });
}

function renderGame() {
  const splashAction = activeSplashAction;
  const scrollPositions = captureGameScrollPositions();
  const title =
    state.status === "finished"
      ? "Game finished"
      : state.currentPlayerName
        ? `${state.currentPlayerName}'s turn`
        : "Waiting";

  app.innerHTML = `
    <main class="game-shell">
      <header class="topbar">
        <div class="room-title">
          <span class="room-code">${escapeHtml(state.code)}</span>
          <strong>Bluff UNO Table</strong>
        </div>
        <div class="turn-pill">${escapeHtml(title)}</div>
        <div class="top-actions">
          <button id="copyLink" class="ghost" type="button">Copy link</button>
          ${
            state.status === "finished"
              ? '<button id="backHome" class="secondary" type="button">Back to Home</button>'
              : `<button id="leaveRoom" class="secondary" type="button" ${state.canLeave ? "" : "disabled"}>Leave room</button>`
          }
          ${state.you && state.you.host ? `<button id="closeRoom" class="danger" type="button" ${state.canClose ? "" : "disabled"}>Close room</button>` : ""}
        </div>
      </header>
      <section class="table-layout">
        <aside class="side-panel">
          <h2 class="panel-title">Table order</h2>
          <div class="player-order">${renderPlayers()}</div>
          <h2 class="panel-title ranking-title">Rankings</h2>
          <div class="ranking-list">${renderRankings()}</div>
        </aside>
        <section class="felt-area">
          ${
            splashAction
              ? `<div class="action-splash" style="--splash-duration: ${actionSplashVisibleDuration(splashAction)}ms; --splash-delay: ${actionSplashDelay(splashAction)}ms">${escapeHtml(splashAction.text)}</div>`
              : ""
          }
          ${renderReveal(splashAction)}
          ${renderPile(splashAction)}
        </section>
        <aside class="log-panel">
          <h2 class="panel-title">Moves</h2>
          <div class="log-list">${renderActions()}</div>
        </aside>
      </section>
      <section class="hand-panel">
        ${renderControls()}
        <div class="hand">${renderHand()}</div>
        <div class="error ${errorText ? "visible" : ""}">${escapeHtml(errorText)}</div>
      </section>
    </main>
  `;

  document.querySelector("#copyLink").addEventListener("click", async () => {
    await navigator.clipboard.writeText(window.location.href);
  });

  const closeButton = document.querySelector("#closeRoom");
  if (closeButton) closeButton.addEventListener("click", closeRoom);
  const leaveButton = document.querySelector("#leaveRoom");
  if (leaveButton) leaveButton.addEventListener("click", leaveRoom);
  const backHomeButton = document.querySelector("#backHome");
  if (backHomeButton) backHomeButton.addEventListener("click", goHome);

  document.querySelectorAll("[data-card-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.dataset.cardId;
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      updateSelectionControls();
    });
  });

  const rankSelect = document.querySelector("#rank");
  if (rankSelect) rankSelect.addEventListener("change", updateSelectionControls);

  document.querySelector("#play").addEventListener("click", async () => {
    const cardIds = [...selected];
    const claimedRank = document.querySelector("#rank").value;
    if (!claimedRank) {
      setError("Choose a rank to declare before playing.");
      return;
    }
    try {
      selected.clear();
      await api("/api/action", {
        code: session.code,
        playerId: session.playerId,
        type: "play",
        cardIds,
        claimedRank
      });
    } catch (error) {
      selected = new Set(cardIds);
      setError(error.message);
    }
  });

  document.querySelector("#pass").addEventListener("click", async () => {
    try {
      await api("/api/action", { code: session.code, playerId: session.playerId, type: document.querySelector("#pass").dataset.action });
    } catch (error) {
      setError(error.message);
    }
  });

  document.querySelector("#challenge").addEventListener("click", async () => {
    try {
      await api("/api/action", { code: session.code, playerId: session.playerId, type: "challenge" });
    } catch (error) {
      setError(error.message);
    }
  });

  updateSelectionControls();
  restoreGameScrollPositions(scrollPositions);
}

async function closeRoom() {
  if (!state.canClose) return;
  if (!window.confirm("Close this room for everyone?")) return;
  try {
    await api("/api/close", { code: session.code, playerId: session.playerId });
  } catch (error) {
    setError(error.message);
  }
}

function clearLocalSession(code) {
  if (events) {
    events.close();
    events = null;
  }
  if (statePollTimer) {
    window.clearInterval(statePollTimer);
    statePollTimer = null;
  }
  if (realtimeRetryTimer) {
    window.clearTimeout(realtimeRetryTimer);
    realtimeRetryTimer = null;
  }
  stopRealtimeHeartbeat();
  realtimeConnected = false;
  stateRefreshInFlight = false;
  removeStoredSession(code);
  session = null;
  state = null;
  selected.clear();
  receivedPileCardIds.clear();
  clearReceivedPileTimer();
  actionSplashQueue = [];
  activeSplashAction = null;
  clearActionSplashTimer();
  if (bluffWindowTimer) {
    window.clearTimeout(bluffWindowTimer);
    bluffWindowTimer = null;
  }
  errorText = "";
  pendingJoinCode = "";
  const url = new URL(window.location.href);
  url.searchParams.delete("room");
  window.history.replaceState({}, "", url);
}

function goHome() {
  clearLocalSession(session ? session.code : null);
  render();
}

async function leaveRoom() {
  if (!state.canLeave) return;
  if (!window.confirm("Leave this room?")) return;
  const code = session.code;
  try {
    await api("/api/leave", { code, playerId: session.playerId });
    clearLocalSession(code);
    render();
  } catch (error) {
    setError(error.message);
  }
}

function render() {
  if (!state) {
    renderEntry();
  } else if (state.status === "lobby") {
    renderLobby();
  } else if (state.status === "closed") {
    renderClosedRoom();
  } else {
    renderGame();
  }
}

async function boot() {
  const stored = loadStoredSession(initialRoom);
  if (stored) {
    try {
      session = stored;
      await api("/api/state", { code: stored.code, playerId: stored.playerId });
      return;
    } catch {
      session = null;
    }
  }
  render();
}

startPageKeepalive();
boot();
