"use strict";

const BAND_MIN = 87.5, BAND_MAX = 108.0;
const $ = (id) => document.getElementById(id);
const pct = (f) => ((f - BAND_MIN) / (BAND_MAX - BAND_MIN)) * 100;

function storage(key, make) {
  try {
    let v = localStorage.getItem(key);
    if (!v) { v = make(); localStorage.setItem(key, v); }
    return v;
  } catch { return make(); }
}
const clientId = storage("freqon.client_id", () => "web-" + Math.random().toString(36).slice(2, 10));
const clientName = "Web player" + (navigator.platform ? ` (${navigator.platform})` : "");
const ident = { client_id: clientId, client_name: clientName, kind: "web" };

let status = null;
let stations = [];
let listening = false;
let pendingRetry = null;

// ---------------------------------------------------------------- API

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify({ ...ident, ...body }) : undefined,
  });
  if (res.status === 401) location.href = "/login"; // signed out (login turned on, or expired)
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, data };
}

async function tune(frequency, program = 0, override = false) {
  const r = await api("/api/tune", "POST", { frequency, program, override });
  if (r.status === 409) return askOverride(r.data, () => tune(frequency, program, true));
  if (!r.ok) return showError(r.data?.error || "Couldn't tune.");
  render(r.data);
  setListening(true); // picking a station means you want to hear it
  ensureAudio();
}

function setListening(on) {
  listening = on;
  $("play").setAttribute("aria-pressed", String(on));
  $("play").textContent = on ? "Stop" : "Listen";
}

async function setMode(mode) {
  const r = await api("/api/mode", "POST", { mode });
  if (r.ok) render(r.data);
}

async function scan(override = false) {
  const r = await api("/api/scan", "POST", { override });
  if (r.status === 409) return askOverride(r.data, () => scan(true));
  if (r.ok) render(r.data);
}

// A station the scan missed (weak that night) can be added while you're tuned to it.
$("add-station").addEventListener("click", async () => {
  const r = await api("/api/stations", "POST", { frequency: status.frequency });
  if (!r.ok) return showError(r.data?.error || "Couldn't add the station.");
  await loadStations();
  render(status);
});

async function loadStations() {
  const r = await api("/api/stations");
  if (r.ok) {
    stations = r.data.stations;
    renderStations();
    if (status) render(status); // "Add to the station list" depends on the list; status may have come first
  }
}

// ---------------------------------------------------------------- busy / errors

function askOverride(busy, retry) {
  $("busy-message").textContent = busy.message;
  const allowed = busy.can_take_over !== false; // the server can turn takeovers off
  $("busy-override").hidden = !allowed;
  $("busy-hint").hidden = !allowed;
  pendingRetry = allowed ? retry : null;
  $("busy").showModal();
}
$("busy").addEventListener("close", () => {
  const retry = pendingRetry;
  pendingRetry = null;
  if ($("busy").returnValue === "override" && retry) retry();
});

function showError(msg) {
  $("signal").textContent = msg;
}

// ---------------------------------------------------------------- audio

function ensureAudio() {
  const audio = $("audio");
  const q = new URLSearchParams({ ...ident, t: Date.now() });
  if (!audio.src || audio.paused || audio.error) {
    audio.src = "/stream.mp3?" + q;
    audio.play().catch((e) => showError("Playback was blocked: " + e.message));
  }
}

function stopAudio() {
  const audio = $("audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
}

$("play").addEventListener("click", async () => {
  setListening(!listening);
  if (!listening) return stopAudio();
  if (!status?.running) {
    const f = status?.frequency || stations[0]?.frequency;
    if (!f) {
      setListening(false);
      return showError("Pick a station or tune a frequency first.");
    }
    await tune(f, status?.program || 0);
  }
  ensureAudio();
});

$("audio").addEventListener("error", () => {
  if (listening) setTimeout(ensureAudio, 2000); // stream dropped (e.g. service restart): reconnect
});

// ---------------------------------------------------------------- broadcast extras

let mapStamp = 0;

function renderExtras(s, tuned) {
  // Album art the station sends for the song; otherwise its logo (broadcast or generated).
  const art = $("art");
  const want = !tuned || !s.channel_id ? "" :
    s.art_key ? `/art/${s.channel_id}.jpg?k=${s.art_key}` : `/logo/${s.channel_id}.png`;
  if (art.dataset.src !== want) {
    art.dataset.src = want;
    if (want) art.src = want; else art.removeAttribute("src");
  }
  art.hidden = !want;
  art.alt = s.art_key ? "Album art" : "Station logo";

  const alert = s.alert;
  $("alert").hidden = !alert;
  $("alert").textContent = alert ?
    `Emergency alert${alert.categories?.length ? ` (${alert.categories.join(", ")})` : ""}: ${alert.message}` : "";

  const details = [];
  if (s.audio && s.digital?.synced) details.push(`HD audio codec mode ${s.audio.codec_mode}`);
  const loc = s.station?.location;
  if (loc) details.push(`Transmitter ${loc.latitude.toFixed(3)}, ${loc.longitude.toFixed(3)}`);
  if (s.station?.utc_offset != null) {
    const h = s.station.utc_offset / 60;
    details.push(`Station time UTC${h >= 0 ? "+" : ""}${h}`);
  }
  $("details").textContent = details.join(". ");

  // Maps refresh at most once a minute (they change slowly).
  const maps = s.maps || [];
  $("maps").hidden = maps.length === 0;
  const now = Date.now();
  const refresh = now - mapStamp > 60000;
  for (const kind of ["traffic", "weather"]) {
    const fig = $(`map-${kind}`);
    fig.hidden = !maps.includes(kind);
    if (!fig.hidden && (refresh || !fig.querySelector("img").src)) fig.querySelector("img").src = `/maps/${kind}.png?t=${now}`;
  }
  if (refresh) mapStamp = now;
}

$("art").addEventListener("error", () => {
  // Art disappeared between the status update and the fetch: show the logo instead.
  const cid = status?.channel_id;
  if (cid && !$("art").src.includes("/logo/")) $("art").src = `/logo/${cid}.png`;
});

// ---------------------------------------------------------------- rendering

function fmt(f) { return f == null ? "--.-" : Number(f).toFixed(1); }

function render(s) {
  status = s;
  const tuned = s.frequency != null;
  $("freq").textContent = fmt(s.frequency);
  const src = s.running ? s.source : "none";
  const label = { digital: `HD${s.program + 1}`, analog: "FM", none: s.running ? "No signal" : "Off" }[src];
  $("source").className = "source source-" + src;
  $("source").textContent = label;
  $("station-name").textContent = s.station.display_name || (tuned ? `${fmt(s.frequency)} FM` : "Not tuned");

  const np = s.now_playing;
  const npText = [np.title, np.artist].filter(Boolean).join(" — ");
  $("now-playing").textContent = npText || s.station.slogan || "";

  const sig = [];
  if (s.digital.synced) sig.push(`HD locked${s.digital.ber != null ? `, ${(s.digital.ber * 100).toFixed(1)}% bit errors` : ""}`);
  else if (s.running) sig.push(s.mode === "analog" ? "FM only" : "Looking for HD signal…");
  if (s.digital.hd_hold) {
    sig.push("HD keeps dropping out, so this stays on FM. Choose Auto or HD to try HD again");
  } else if (s.mode === "auto" && s.source === "analog" && s.digital.quality != null && s.digital.quality < 0.9) {
    sig.push(`HD too weak (${Math.round(s.digital.quality * 100)}% of its audio getting through), playing FM`);
  }
  if (s.tuners && s.tuners.length > 1) sig.push(`Tuner ${s.tuner + 1}`);
  const others = s.listeners.filter((l) => l.client_id !== clientId && l.channel_id === s.channel_id);
  if (others.length) sig.push(`Also listening: ${[...new Set(others.map((l) => l.name))].join(", ")}`);
  if (s.error) sig.push(s.error);
  $("signal").textContent = sig.join(". ");
  renderExtras(s, tuned);
  const listed = stations.some((st) => st.frequency === s.frequency);
  $("add-station").hidden = !tuned || listed;
  $("add-station").textContent = `Add ${fmt(s.frequency)} to the station list`;

  const needle = $("needle");
  needle.hidden = !tuned;
  if (tuned) needle.style.left = pct(s.frequency) + "%";

  document.querySelectorAll(".mode button").forEach((b) =>
    b.setAttribute("aria-checked", String(b.dataset.mode === s.mode)));

  const progs = $("programs");
  progs.replaceChildren();
  if (s.programs.length > 1) {
    for (const p of s.programs) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = `HD${p.program + 1}`;
      b.title = p.type || p.name || "";
      b.setAttribute("aria-current", String(p.program === s.program));
      b.addEventListener("click", () => tune(s.frequency, p.program));
      progs.append(b);
    }
  }

  const sc = s.scan || {};
  const scanStatus = $("scan-status");
  $("scan").textContent = sc.running ? "Stop scan" : "Scan for stations";
  if (sc.running) {
    scanStatus.hidden = false;
    scanStatus.textContent = sc.phase === "sweep"
      ? `Sweeping the band… ${fmt(sc.frequency)} MHz (${sc.done} of ${sc.total})`
      : `Checking ${fmt(sc.frequency)} for HD (${sc.done} of ${sc.total})… found ${sc.found?.length || 0}`;
  } else if (sc.finished && !scanStatus.dataset.done) {
    scanStatus.dataset.done = "1";
    scanStatus.hidden = false;
    scanStatus.textContent = `Scan finished: found ${sc.found.length} stations.`;
    loadStations();
  } else if (sc.error) {
    scanStatus.hidden = false;
    scanStatus.textContent = "Scan failed: " + sc.error;
  }
  if (sc.running) delete scanStatus.dataset.done;

  renderStations();
}

function renderStations() {
  const list = $("station-list");
  list.replaceChildren();
  $("no-stations").hidden = stations.length > 0;
  const marks = $("marks");
  marks.replaceChildren();
  for (const st of stations) {
    const current = status && status.frequency === st.frequency;

    const m = document.createElement("button");
    m.type = "button";
    m.className = "mark" + (st.hd ? " hd" : "");
    m.style.left = pct(st.frequency) + "%";
    m.title = `${fmt(st.frequency)} ${st.display_name}`;
    m.setAttribute("aria-label", `Tune ${fmt(st.frequency)} ${st.display_name}`);
    m.setAttribute("aria-current", String(!!current));
    m.addEventListener("click", (e) => { e.stopPropagation(); tune(st.frequency, 0); });
    marks.append(m);

    const li = document.createElement("li");
    li.setAttribute("aria-current", String(!!current));
    const fb = document.createElement("button");
    fb.type = "button";
    fb.className = "st-freq";
    fb.textContent = fmt(st.frequency);
    fb.setAttribute("aria-label", `Tune ${fmt(st.frequency)}`);
    fb.addEventListener("click", () => tune(st.frequency, 0));
    const info = document.createElement("div");
    const name = document.createElement("div");
    name.className = "st-name";
    name.textContent = st.display_name;
    const meta = document.createElement("div");
    meta.className = "st-meta";
    meta.textContent = st.hd ? `HD Radio, ${st.channels.length} channel${st.channels.length > 1 ? "s" : ""}` : "Analog only";
    info.append(name, meta);
    const progs = document.createElement("div");
    progs.className = "st-progs";
    for (const c of st.channels) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = st.hd ? "hd" : "";
      b.textContent = st.hd ? `HD${c.program + 1}` : "FM";
      if (c.program_type) b.title = c.program_type;
      b.addEventListener("click", () => tune(c.frequency, c.program));
      progs.append(b);
    }
    li.append(fb, info, progs);
    list.append(li);
  }
}

function buildDial() {
  const dial = $("dial"), scale = $("scale");
  const addTick = (f, cls) => {
    const t = document.createElement("div");
    t.className = cls;
    t.style.left = pct(f) + "%";
    dial.insertBefore(t, $("marks"));
  };
  for (let i = 0; i <= 101; i++) addTick(87.7 + i * 0.2, "tick"); // every channel
  for (let f = 88; f <= 108; f += 2) addTick(f, "tick major");
  for (let f = 88; f <= 108; f += 4) {
    const s = document.createElement("span");
    s.textContent = f;
    s.style.left = pct(f) + "%";
    scale.append(s);
  }
  // Click anywhere on the dial: nearest saved station, or nearest channel if none saved.
  dial.addEventListener("click", (e) => {
    const r = dial.getBoundingClientRect();
    const f = BAND_MIN + ((e.clientX - r.left) / r.width) * (BAND_MAX - BAND_MIN);
    let target = Math.round((f - 0.1) / 0.2) * 0.2 + 0.1;
    if (stations.length) {
      const near = stations.reduce((a, b) => (Math.abs(b.frequency - f) < Math.abs(a.frequency - f) ? b : a));
      if (Math.abs(near.frequency - f) < 0.6) target = near.frequency;
    }
    tune(Math.round(target * 10) / 10, 0);
  });
}

// ---------------------------------------------------------------- inputs

document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

$("tune-form").addEventListener("submit", (e) => {
  e.preventDefault();
  let f = parseFloat($("tune-input").value);
  if (f > 870 && f <= 1080) f = f / 10; // "1011" -> 101.1
  if (!(f >= BAND_MIN && f <= BAND_MAX)) return showError(`Enter a frequency from ${BAND_MIN} to ${BAND_MAX}.`);
  tune(Math.round(f * 10) / 10, parseInt($("tune-program").value, 10));
});

$("scan").addEventListener("click", () => {
  if (status?.scan?.running) api("/api/scan", "DELETE");
  else scan();
});

// ---------------------------------------------------------------- live updates

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?${new URLSearchParams(ident)}`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "status") render(msg.status);
  };
  // A closed socket may mean the session ended: api() sends you to the sign-in page if so.
  ws.onclose = () => setTimeout(async () => {
    try { await api("/api/session"); } catch { /* server restarting */ }
    connect();
  }, 2000);
}

async function showAccount() {
  const r = await api("/api/session");
  if (r.ok && r.data?.login) {
    $("account-name").textContent = r.data.username;
    $("account").hidden = false;
  }
}

buildDial();
loadStations();
connect();
showAccount();
