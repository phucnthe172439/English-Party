// Word Party frontend. Plain JavaScript, no build step.
// On Render the server pushes the room state through a WebSocket.
// On Vercel (or if the socket breaks) the browser asks every second (polling).
// Either way, render(view) draws the screen from that state.

let AVATARS = ["🐶", "🐱", "🐼", "🦊", "🐸", "🐵", "🐰", "🐯", "🦁", "🐨", "🐷", "🐙"];

const S = {
  avatar: AVATARS[0], allTopics: [], homeTopics: [],
  code: null, pid: null, secret: null,
  view: null, lastStatus: null, after: 0,
  realtime: false, ws: null, wsFails: 0, polling: false,
  pollTimer: null, pollId: 0, lastPing: 0,
  stageKey: "", questionKey: "",
  tried: new Set(), rightPick: null, filled: {},
  deadline: 0, timeTotal: 1, busy: false, answering: false,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Only touch the DOM when the HTML really changed: no flicker, no lost clicks.
function setHTML(el, html) {
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
}

// ---------- small tools ----------

const storage = {
  get(area, key) { try { return JSON.parse(window[area].getItem(key)); } catch { return null; } },
  set(area, key, value) { try { window[area].setItem(key, JSON.stringify(value)); } catch {} },
  remove(area, key) { try { window[area].removeItem(key); } catch {} },
};

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch("/api" + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || "Something went wrong. Try again.");
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = "toast"), 2800);
}

let audioCtx;
function beep(notes) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    let t = audioCtx.currentTime;
    for (const [freq, dur] of notes) {
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.type = "triangle";
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.18, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + dur);
      o.connect(g).connect(audioCtx.destination);
      o.start(t);
      o.stop(t + dur);
      t += dur * 0.8;
    }
  } catch {}
}
const SOUND = {
  good: () => beep([[660, 0.12], [880, 0.2]]),
  bad: () => beep([[220, 0.28]]),
  go: () => beep([[520, 0.1], [780, 0.15]]),
  win: () => beep([[523, 0.15], [659, 0.15], [784, 0.15], [1046, 0.35]]),
};

function speak(word) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(word);
  u.lang = "en-US";
  u.rate = 0.85;
  speechSynthesis.speak(u);
}

function floatText(text, kind) {
  const box = $("#stage").getBoundingClientRect();
  const el = document.createElement("div");
  el.className = "float " + kind;
  el.textContent = text;
  el.style.left = box.left + box.width / 2 + "px";
  el.style.top = box.top + 40 + "px";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1000);
}

function sparkle() {
  const box = $("#stage").getBoundingClientRect();
  const colors = ["#FFD84D", "#6BD38A", "#FF6B8B", "#A68BFF", "#7FD3FF"];
  for (let i = 0; i < 14; i++) {
    const s = document.createElement("span");
    const angle = (Math.PI * 2 * i) / 14;
    s.className = "spark";
    s.style.background = colors[i % colors.length];
    s.style.left = box.left + box.width / 2 + "px";
    s.style.top = box.top + box.height / 2 + "px";
    s.style.setProperty("--dx", Math.cos(angle) * 160 + "px");
    s.style.setProperty("--dy", Math.sin(angle) * 120 + "px");
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 800);
  }
}

function shake(el) {
  el.classList.remove("shake");
  void el.offsetWidth;
  el.classList.add("shake");
}

// ---------- home screen ----------

function renderAvatars() {
  setHTML($("#avatars"), AVATARS.map((a) =>
    `<button class="avatar-btn" data-avatar="${a}" aria-pressed="${a === S.avatar}" aria-label="Avatar ${a}">${a}</button>`
  ).join(""));
}

function renderHomeTopics() {
  setHTML($("#home-topics"), S.allTopics.map((t) =>
    `<button class="chip ${S.homeTopics.includes(t.id) ? "on" : ""}" data-topic="${t.id}" aria-pressed="${S.homeTopics.includes(t.id)}">${t.icon} ${esc(t.name)}</button>`
  ).join(""));
}

function toggleHomeTopic(id) {
  const has = S.homeTopics.includes(id);
  if (has && S.homeTopics.length === 1) return toast("Pick at least one topic.");
  S.homeTopics = has ? S.homeTopics.filter((t) => t !== id) : [...S.homeTopics, id];
  renderHomeTopics();
}

function readProfile() {
  const name = $("#name").value.trim();
  $("#home-error").textContent = "";
  if (name.length < 2 || name.length > 12) {
    $("#home-error").textContent = "Your nickname needs 2 to 12 letters.";
    $("#name").focus();
    return null;
  }
  storage.set("localStorage", "wp:profile", { name, avatar: S.avatar });
  return { name, avatar: S.avatar };
}

async function busy(task) {
  if (S.busy) return;
  S.busy = true;
  try { await task(); } catch (e) { toast(e.message, "bad"); } finally { S.busy = false; }
}

function quickMatch() {
  const p = readProfile();
  if (p) busy(async () => enterRoom(await api("/quickmatch", p)));
}

function createRoom() {
  const p = readProfile();
  if (p) busy(async () => enterRoom(await api("/rooms", { ...p, topics: S.homeTopics })));
}

function joinRoom() {
  const code = $("#code").value.trim().toUpperCase();
  if (code.length !== 5) {
    $("#join-hint").textContent = "Room codes have 5 letters.";
    return $("#code").focus();
  }
  const p = readProfile();
  if (p) busy(async () => enterRoom(await api(`/rooms/${code}/join`, p)));
}

// ---------- entering and leaving a room ----------

function enterRoom(creds) {
  S.code = creds.code;
  S.pid = creds.pid;
  S.secret = creds.secret;
  S.after = 0;
  S.view = null;
  S.lastStatus = null;
  S.stageKey = S.questionKey = "";
  S.lastPing = 0;
  // sessionStorage = one player per browser tab, but a refresh keeps you in the room
  storage.set("sessionStorage", "wp:room:" + creds.code, creds);
  history.replaceState(null, "", "?room=" + creds.code);
  $("#chat-list").innerHTML = "";
  $("#home").hidden = true;
  $("#room").hidden = false;
  $("#room-code-small").textContent = creds.code;
  S.wsFails = 0;
  if (S.realtime) connectSocket();
  else startPolling();
}

function goHome(message) {
  stopPolling();
  if (S.ws) { const ws = S.ws; S.ws = null; ws.close(); }
  if (S.code) storage.remove("sessionStorage", "wp:room:" + S.code);
  S.code = null;
  history.replaceState(null, "", "/");
  $("#room").hidden = true;
  $("#home").hidden = false;
  if (message) toast(message, "bad");
}

async function leaveRoom() {
  try { await api(`/rooms/${S.code}/leave`, auth()); } catch {}
  goHome();
}

async function shareRoom() {
  const url = `${location.origin}/?room=${S.code}`;
  const text = `Come play Word Party with me! Room code: ${S.code}`;
  if (navigator.share) {
    try { await navigator.share({ title: "Word Party", text, url }); return; }
    catch (e) { if (e.name === "AbortError") return; }
  }
  try { await navigator.clipboard.writeText(url); toast("Link copied! Send it to your friends."); }
  catch { toast(url); }
}

const auth = () => ({ pid: S.pid, secret: S.secret });

// ---------- live updates: WebSocket first, polling as backup ----------

function connectSocket() {
  if (!S.code) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/rooms/${S.code}/ws?${new URLSearchParams(auth())}`);
  let opened = false;
  S.ws = ws;
  ws.onopen = () => { opened = true; S.wsFails = 0; stopPolling(); };
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.error) return goHome(data.error);
    render(data);
  };
  ws.onclose = () => {
    if (S.ws !== ws || !S.code) return;     // we closed it on purpose
    S.ws = null;
    if (!opened) S.wsFails++;
    startPolling();                         // keep the game going while we reconnect
    if (S.wsFails < 5) setTimeout(connectSocket, opened ? 1000 : 3000);
  };
}

// Render sleeps after 15 min without incoming messages, so say hi now and then
setInterval(() => { if (S.ws && S.ws.readyState === 1) S.ws.send("ping"); }, 25000);

function startPolling() {
  if (S.polling) return;
  S.polling = true;
  S.lastPing = 0;
  pollNow();
}

function stopPolling() {
  S.polling = false;
  clearTimeout(S.pollTimer);
  S.pollId++;
}

function pollNow() {
  if (S.ws && S.ws.readyState === 1) return;   // the server will push the change
  if (!S.polling) return;
  clearTimeout(S.pollTimer);
  poll();
}

async function poll() {
  if (!S.code) return;
  const myId = ++S.pollId;
  const ping = Date.now() - S.lastPing > 4000;   // tell the server "I'm still here" every 4s
  if (ping) S.lastPing = Date.now();
  try {
    const q = new URLSearchParams({ ...auth(), after: S.after, ping: ping ? 1 : 0 });
    const view = await api(`/rooms/${S.code}/state?${q}`);
    if (myId !== S.pollId) return;               // a newer poll already started
    render(view);
  } catch (e) {
    if (myId !== S.pollId) return;
    if (e.status === 403 || e.status === 404) return goHome(e.message);
  }
  if (!S.polling || myId !== S.pollId) return;
  const fast = S.view && ["countdown", "question", "reveal"].includes(S.view.status);
  S.pollTimer = setTimeout(poll, fast ? 1000 : 2000);
}

// ---------- drawing the room ----------

function render(v) {
  S.view = v;
  addChat(v.chat);

  const section = v.status === "lobby" ? "lobby"
    : v.status === "finished" ? "finished"
    : !v.playing ? "waiting"
    : v.status === "countdown" ? "countdown"
    : "play";
  for (const id of ["lobby", "waiting", "countdown", "play", "finished"]) $("#" + id).hidden = id !== section;
  $("#scoreboard-card").hidden = v.status === "lobby";

  renderScoreboard(v);
  if (section === "lobby") renderLobby(v);
  if (section === "countdown") renderCountdown(v);
  if (section === "play") renderPlay(v);
  if (section === "finished") renderFinished(v);
  S.lastStatus = v.status;
}

function renderScoreboard(v) {
  setHTML($("#scoreboard"), v.players.map((p, i) => `
    <li class="${p.pid === v.me ? "me" : ""} ${p.online ? "" : "off"}">
      <span class="rank">${p.playing ? i + 1 : ""}</span>
      <span class="bubble sm">${p.avatar}</span>
      <span class="pname">${esc(p.name)}</span>
      ${p.solved ? '<span title="Got it">✅</span>' : ""}
      <span class="pts">${p.score}</span>
    </li>`).join(""));
}

function renderLobby(v) {
  const isHost = v.host === v.me;
  setHTML($("#lobby-code"), [...v.code].map((c) => `<span class="tile">${c}</span>`).join(""));

  const seats = v.players.map((p) => `
    <li class="player ${p.pid === v.me ? "me" : ""}">
      <span class="bubble">${p.avatar}</span>
      <span class="pname">${esc(p.name)}</span>
      ${p.host ? '<span class="crown" title="Host">👑</span>' : ""}
    </li>`);
  for (let i = v.players.length; i < v.max_players; i++) {
    seats.push('<li class="player empty"><span class="bubble">?</span><span class="pname">Free seat</span></li>');
  }
  setHTML($("#lobby-players"), seats.join(""));

  setHTML($("#lobby-topics"), S.allTopics.map((t) => {
    const on = v.topics.includes(t.id);
    return isHost
      ? `<button class="chip ${on ? "on" : ""}" data-room-topic="${t.id}" aria-pressed="${on}">${t.icon} ${esc(t.name)}</button>`
      : on ? `<span class="chip on">${t.icon} ${esc(t.name)}</span>` : "";
  }).join(""));

  let html = "";
  if (v.public) {
    html += v.auto_start_in != null
      ? `<p class="big-note">Game starts in <b data-timer-text></b> seconds</p>`
      : `<p class="big-note">Waiting for more friends…</p>`;
  }
  if (isHost) {
    html += `<button class="btn btn-big" data-action="start">Start game</button>`;
    if (v.players.length < v.min_players) html += `<p class="note">You need ${v.min_players} players. Tap "Invite friends" to share the room!</p>`;
  } else if (!v.public) {
    html += `<p class="big-note">Waiting for the host to start…</p>`;
  }
  setHTML($("#lobby-start"), html);

  if (v.auto_start_in != null) setDeadline(v.auto_start_in, 15);
}

function renderCountdown(v) {
  setDeadline(v.time_left, 4);
  if (S.lastStatus !== "countdown") SOUND.go();
}

function setDeadline(secondsLeft, total) {
  S.deadline = performance.now() + secondsLeft * 1000;
  S.timeTotal = total;
}

// Smooth timer between polls
function tick() {
  const left = Math.max(0, (S.deadline - performance.now()) / 1000);
  document.querySelectorAll("[data-timer-bar]").forEach((el) => (el.style.width = (100 * left) / S.timeTotal + "%"));
  document.querySelectorAll("[data-timer-text]").forEach((el) => (el.textContent = Math.max(1, Math.ceil(left))));
  $("#timer").classList.toggle("warn", S.view?.status === "question" && left < 4);
  requestAnimationFrame(tick);
}

// ---------- gameplay ----------

const ASK = {
  pick: "Pick the English word",
  letters: "Tap the letters to fill the blanks",
  race: "Type the word in English",
};

function renderPlay(v) {
  const q = v.question;
  setDeadline(q.time_left, q.time_total);
  $("#q-count").textContent = `Question ${q.index + 1} of ${q.total}`;
  setHTML($("#q-level"), `<span class="pill lv${q.level}">Level ${q.level}: ${q.level_name}</span>`);
  $("#my-score").textContent = v.my_score;

  const questionKey = `${v.round}-${q.index}`;
  if (questionKey !== S.questionKey) {          // brand new question: clean up
    S.questionKey = questionKey;
    S.tried = new Set();
    S.rightPick = null;
    S.filled = {};
  }
  const stageKey = `${questionKey}-${v.status}-${q.hints ?? ""}`;
  if (stageKey !== S.stageKey) {               // something on screen must change
    S.stageKey = stageKey;
    buildStage(v, q);
    if (v.status === "reveal") speak(q.answer);
  }
  updateStageStatus(v, q);
}

function buildStage(v, q) {
  const stage = $("#stage");
  const head = `<div class="q-emoji">${q.emoji}</div><p class="q-vi">${esc(q.vi)}</p>`;

  if (v.status === "reveal") {
    stage.innerHTML = `${head}
      <div class="reveal">
        <p class="answer">${esc(q.answer)}</p>
        <button class="btn btn-soft sm" data-speak="${esc(q.answer)}">🔊 Listen again</button>
        <p class="msg">${q.my_solved ? "You got it! 🎉" : "Now you know this one! 💪"}</p>
      </div>`;
    return;
  }

  let body = "";
  if (q.kind === "pick") {
    body = `<div class="options">${q.options.map((o, i) => `<button class="option" data-option="${i}">${esc(o)}</button>`).join("")}</div>`;
  } else if (q.kind === "letters") {
    body = `<div class="slots" id="slots"></div><div class="tiles" id="tiles"></div>
      <div class="letters-tools"><button class="btn btn-soft sm" data-action="clear-letters">Clear</button></div>`;
  } else {
    body = `<div class="mask">${byWord(q.mask, (c) => (c === "_" ? '<span class="box"></span>' : `<span class="box fixed">${esc(c)}</span>`))}</div>
      <div class="race-row">
        <input id="race-input" maxlength="30" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Type in English" aria-label="Your answer">
        <button class="btn" data-action="send-race">Send</button>
      </div>`;
  }
  stage.innerHTML = `${head}<p class="q-ask">${ASK[q.kind]}</p>${body}`;

  if (q.kind === "letters") drawLetters(q);
  if (q.kind === "race" && !q.my_solved) $("#race-input").focus();
}

function updateStageStatus(v, q) {
  const stage = $("#stage");
  if (v.status !== "question") return setHTML($("#stage-status"), "");
  stage.classList.toggle("done", q.my_solved);
  if (q.kind === "pick") {
    document.querySelectorAll(".option").forEach((b, i) => {
      const word = q.options[i];
      b.classList.toggle("wrong", S.tried.has(word));
      b.classList.toggle("right", word === S.rightPick);
      b.disabled = S.tried.has(word) || q.my_solved;
    });
  }
  const playing = v.players.filter((p) => p.playing && p.online);
  const done = playing.filter((p) => p.solved).length;
  setHTML($("#stage-status"), q.my_solved
    ? `✅ Great job! Waiting for friends (${done}/${playing.length})`
    : q.kind === "letters"
      ? `Answer now: +${q.points} (each new hint −20), wrong try −${q.penalty}`
      : `Right answer +${q.points}, wrong try −${q.penalty}`);
}

// Draw letters grouped by word, so "sweet potato" never breaks inside a word.
function byWord(chars, draw) {
  let html = "", group = "";
  chars.forEach((c, i) => {
    if (c === " ") { html += `<span class="word">${group}</span>`; group = ""; }
    else group += draw(c, i);
  });
  return html + `<span class="word">${group}</span>`;
}

// Missing letters: S.filled = { slotIndex: "a" }
function drawLetters(q) {
  for (const k of Object.keys(S.filled)) if (q.pattern[k] !== null) delete S.filled[k];   // a hint filled it
  const used = Object.values(S.filled);
  const tileUsed = q.tiles.map((letter) => {
    const i = used.indexOf(letter);
    if (i >= 0) { used.splice(i, 1); return true; }
    return false;
  });
  $("#slots").innerHTML = byWord(q.pattern, (c, i) => {
    if (c !== null) return `<span class="slot fixed">${esc(c)}</span>`;
    const f = S.filled[i];
    return `<button class="slot ${f ? "filled" : ""}" data-slot="${i}" aria-label="Blank ${i + 1}">${f ? esc(f) : ""}</button>`;
  });
  $("#tiles").innerHTML = q.tiles.map((l, i) =>
    `<button class="tile-btn" data-tile="${i}" ${tileUsed[i] ? "disabled" : ""}>${esc(l)}</button>`
  ).join("");
}

function tapTile(i) {
  const q = S.view.question;
  const slot = q.pattern.findIndex((c, k) => c === null && !S.filled[k]);
  if (slot < 0) return;
  S.filled[slot] = q.tiles[i];
  drawLetters(q);
  const allFilled = q.pattern.every((c, k) => c !== null || S.filled[k]);
  if (allFilled) sendAnswer(q.pattern.map((c, k) => (c === null ? S.filled[k] : c)).join(""));
}

function tapSlot(k) {
  delete S.filled[k];
  drawLetters(S.view.question);
}

function clearLetters() {
  S.filled = {};
  drawLetters(S.view.question);
}

function pickOption(i) {
  const word = S.view.question.options[i];
  if (!S.tried.has(word)) sendAnswer(word);
}

function sendRace() {
  const input = $("#race-input");
  if (!input.value.trim()) { shake(input); return input.focus(); }
  sendAnswer(input.value);
}

function updateRaceBoxes() {
  const chars = $("#race-input").value.replace(/\s/g, "").split("");
  document.querySelectorAll(".mask .box:not(.fixed)").forEach((b, i) => {
    b.textContent = chars[i] || "";
    b.classList.toggle("typed", !!chars[i]);
  });
}

async function sendAnswer(answer) {
  if (S.answering) return;
  S.answering = true;
  try {
    const kind = S.view.question.kind;
    const r = await api(`/rooms/${S.code}/answer`, { ...auth(), answer });
    if (r.result === "correct") {
      SOUND.good();
      floatText(`+${r.points}`, "good");
      sparkle();
      if (kind === "pick") S.rightPick = answer;
      $("#my-score").textContent = r.score;
    } else if (r.result === "wrong") {
      SOUND.bad();
      floatText(r.lost ? `−${r.lost}` : "Oops!", "bad");
      shake($("#stage"));
      $("#my-score").textContent = r.score;
      if (kind === "pick") S.tried.add(answer);
      if (kind === "letters") clearLetters();
      if (kind === "race") $("#race-input").select();
    }
    if (S.view?.question) updateStageStatus(S.view, S.view.question);
    pollNow();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    S.answering = false;
  }
}

// ---------- end of game ----------

function renderFinished(v) {
  const ranked = v.players.filter((p) => p.playing).sort((a, b) => b.score - a.score);
  const myRank = ranked.findIndex((p) => p.pid === v.me) + 1;
  const order = [[ranked[1], 2], [ranked[0], 1], [ranked[2], 3]];
  const podium = order.map(([p, place]) => p ? `
    <div class="step p${place}">
      <span class="bubble">${p.avatar}</span>
      <span class="pname">${esc(p.name)}</span>
      <b>${p.score}</b>
      <div class="block">${place}</div>
    </div>` : "").join("");

  let review = "";
  if (v.review.length) {
    review = `<h3>Words to practice</h3><ul class="review">${v.review.map((w) => `
      <li><span class="e">${w.emoji}</span><span><b>${esc(w.en)}</b>${esc(w.vi)}</span>
      <button class="speak" data-speak="${esc(w.en)}" aria-label="Listen to ${esc(w.en)}">🔊</button></li>`).join("")}</ul>`;
  } else if (myRank) {
    review = `<p class="big-note center">You got every word right! 🌟</p>`;
  }

  const action = v.host === v.me
    ? `<button class="btn btn-big" data-action="again">Play again</button>`
    : `<p class="note">${v.public ? "A new game opens soon." : "Waiting for the host to start a new game…"}</p>`;

  setHTML($("#finished"), `
    <h2 class="center">${myRank === 1 ? "You won! 🏆" : "Game