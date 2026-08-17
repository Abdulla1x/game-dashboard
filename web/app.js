"use strict";

const state = {
  games: [],
  query: "",
  sort: "name",
  sources: new Set(),   // empty = all
  showHidden: false,
  busy: new Set(),
};

const $ = (id) => document.getElementById(id);
const grid = $("grid");

const SOURCE_LABEL = {
  steam: "Steam", epic: "Epic", xbox: "Xbox", riot: "Riot",
  folder: "Folder", shortcut: "Shortcut", manual: "Manual",
};

/* ---------- data ---------- */

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function refresh() {
  try {
    const data = await api("/api/games");
    state.games = data.games;
    $("sizes-btn").disabled = data.sizing;
    $("rescan-btn").disabled = data.scanning;
    if (data.scanning || data.sizing) setTimeout(refresh, 2500);
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- formatting ---------- */

function fmtSize(bytes) {
  if (!bytes) return "";
  const gb = bytes / 1073741824;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1048576))} MB`;
}

function fmtPlaytime(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function fmtWhen(unix) {
  if (!unix) return "";
  const days = Math.floor((Date.now() / 1000 - unix) / 86400);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

/* ---------- filtering ---------- */

function visibleGames() {
  const q = state.query.trim().toLowerCase();
  let list = state.games.filter((g) => {
    if (g.hidden && !state.showHidden) return false;
    if (state.sources.size && !state.sources.has(g.source)) return false;
    if (q && !g.name.toLowerCase().includes(q)) return false;
    return true;
  });

  const by = {
    name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    size: (a, b) => (b.size_bytes || 0) - (a.size_bytes || 0),
    last: (a, b) => (b.last_played || 0) - (a.last_played || 0),
    playtime: (a, b) => (b.playtime_seconds || 0) - (a.playtime_seconds || 0),
  };
  return list.sort(by[state.sort] || by.name);
}

/* ---------- rendering ---------- */

function render() {
  const list = visibleGames();
  grid.textContent = "";

  for (const game of list) grid.appendChild(card(game));

  $("empty").hidden = list.length > 0;
  const total = state.games.filter((g) => !g.hidden).length;
  $("count").textContent = list.length === total
    ? `${total} games`
    : `${list.length} of ${total}`;

  renderFilters();
}

function card(game) {
  const el = document.createElement("article");
  el.className = "card";
  el.tabIndex = 0;
  if (game.running) el.classList.add("running");
  if (game.hidden) el.classList.add("hidden-game");
  if (state.busy.has(game.id)) el.classList.add("busy");
  el.dataset.id = game.id;

  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = "";
  img.src = `/api/art?id=${encodeURIComponent(game.id)}`;
  // Extracted icons are tiny; contain rather than stretch them.
  img.addEventListener("load", () => {
    if (img.naturalWidth && img.naturalWidth < 200) img.classList.add("is-icon");
  });
  el.appendChild(img);

  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = game.running ? "Running" : (SOURCE_LABEL[game.source] || game.source);
  el.appendChild(badge);

  const more = document.createElement("button");
  more.className = "more";
  more.textContent = "\u22ef";
  more.title = "More";
  more.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const r = more.getBoundingClientRect();
    openMenu(game, r.left, r.bottom + 4);
  });
  el.appendChild(more);

  const label = document.createElement("div");
  label.className = "label";
  label.textContent = game.name;

  const bits = [];
  if (game.playtime_seconds) bits.push(fmtPlaytime(game.playtime_seconds));
  else if (game.last_played) bits.push(fmtWhen(game.last_played));
  if (game.size_bytes) bits.push(fmtSize(game.size_bytes));
  if (bits.length) {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = bits.join(" · ");
    label.appendChild(meta);
  }
  el.appendChild(label);

  el.addEventListener("click", () => play(game));
  el.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); play(game); }
  });
  el.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    openMenu(game, ev.clientX, ev.clientY);
  });
  return el;
}

function renderFilters() {
  const counts = {};
  for (const g of state.games) {
    if (g.hidden && !state.showHidden) continue;
    counts[g.source] = (counts[g.source] || 0) + 1;
  }

  const box = $("filters");
  box.textContent = "";

  for (const source of Object.keys(counts).sort()) {
    const chip = document.createElement("button");
    chip.className = "chip" + (state.sources.has(source) ? " on" : "");
    chip.innerHTML = `${SOURCE_LABEL[source] || source}<span class="n">${counts[source]}</span>`;
    chip.addEventListener("click", () => {
      state.sources.has(source) ? state.sources.delete(source) : state.sources.add(source);
      render();
    });
    box.appendChild(chip);
  }

  const hiddenCount = state.games.filter((g) => g.hidden).length;
  if (hiddenCount) {
    const chip = document.createElement("button");
    chip.className = "chip" + (state.showHidden ? " on" : "");
    chip.innerHTML = `Hidden<span class="n">${hiddenCount}</span>`;
    chip.addEventListener("click", () => { state.showHidden = !state.showHidden; render(); });
    box.appendChild(chip);
  }
}

/* ---------- actions ---------- */

async function play(game) {
  if (state.busy.has(game.id)) return;
  state.busy.add(game.id);
  render();
  try {
    await api("/api/launch", { method: "POST", body: JSON.stringify({ id: game.id }) });
    toast(`Launching ${game.name}`);
  } catch (err) {
    toast(`${game.name}: ${err.message}`, true);
  } finally {
    setTimeout(() => { state.busy.delete(game.id); render(); }, 2500);
  }
}

async function override(id, fields) {
  await api("/api/override", { method: "POST", body: JSON.stringify({ id, ...fields }) });
  await refresh();
}

/* ---------- context menu ---------- */

const menu = $("menu");

function openMenu(game, x, y) {
  menu.textContent = "";

  const add = (text, fn) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", () => { closeMenu(); fn(); });
    menu.appendChild(b);
  };

  add("Launch", () => play(game));
  if (game.install_dir) {
    add("Open folder", async () => {
      try {
        await api("/api/reveal", { method: "POST", body: JSON.stringify({ id: game.id }) });
      } catch (err) { toast(err.message, true); }
    });
  }
  menu.appendChild(document.createElement("hr"));
  add("Rename\u2026", () => renameDialog(game));
  if (game.install_dir) add("Pick executable\u2026", () => exeDialog(game));
  add("Fix cover art\u2026", () => artDialog(game));
  menu.appendChild(document.createElement("hr"));
  add(game.hidden ? "Unhide" : "Hide", () => override(game.id, { hidden: !game.hidden }));

  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
}

function closeMenu() { menu.hidden = true; }

document.addEventListener("click", (ev) => { if (!menu.contains(ev.target)) closeMenu(); });
document.addEventListener("scroll", closeMenu, true);

/* ---------- modal ---------- */

const modal = $("modal");
let onSave = null;

function openModal(title, buildBody, save) {
  $("modal-title").textContent = title;
  const body = $("modal-body");
  body.textContent = "";
  buildBody(body);
  onSave = save;
  modal.hidden = false;
  const first = body.querySelector("input");
  if (first) { first.focus(); first.select(); }
}

function closeModal() { modal.hidden = true; onSave = null; }

$("modal-cancel").addEventListener("click", closeModal);
$("modal-save").addEventListener("click", async () => {
  if (!onSave) return closeModal();
  try {
    await onSave();
    closeModal();
  } catch (err) {
    toast(err.message, true);
  }
});
modal.addEventListener("click", (ev) => { if (ev.target === modal) closeModal(); });

function field(parent, labelText, value, placeholder) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  if (placeholder) input.placeholder = placeholder;
  wrap.append(label, input);
  parent.appendChild(wrap);
  return input;
}

function renameDialog(game) {
  let input;
  openModal(`Rename ${game.name}`,
    (body) => { input = field(body, "Display name", game.name); },
    () => override(game.id, { name: input.value.trim() || game.name }));
}

function artDialog(game) {
  let appid, path;
  openModal(`Cover art — ${game.name}`, (body) => {
    const note = document.createElement("p");
    note.style.cssText = "margin:0 0 14px;color:var(--muted);font-size:13px";
    note.textContent = "Art is matched against Steam by name. Set the Steam app ID to " +
      "correct a bad match, or point at a local image file.";
    body.appendChild(note);
    appid = field(body, "Steam app ID", game.steam_appid || "", "e.g. 1245620");
    path = field(body, "Or image file path", game.art_override || "",
                 "C:\\path\\to\\cover.jpg");
  }, () => override(game.id, {
    steam_appid: appid.value.trim() ? Number(appid.value.trim()) : null,
    art: path.value.trim() || null,
  }));
}

async function exeDialog(game) {
  let chosen = game.exe_path || null;
  let manual;

  let data;
  try {
    data = await api(`/api/candidates?id=${encodeURIComponent(game.id)}`);
  } catch (err) {
    return toast(err.message, true);
  }

  openModal(`Executable — ${game.name}`, (body) => {
    const note = document.createElement("p");
    note.style.cssText = "margin:0 0 12px;color:var(--muted);font-size:13px";
    note.textContent = `${data.candidates.length} candidate(s) found, best first.`;
    body.appendChild(note);

    for (const cand of data.candidates) {
      const btn = document.createElement("button");
      btn.className = "cand" + (cand.path === chosen ? " on" : "");
      btn.innerHTML =
        `<div><b></b><em></em></div><span>${fmtSize(cand.size) || ""}</span>`;
      btn.querySelector("b").textContent = cand.name;
      btn.querySelector("em").textContent = cand.path;
      btn.addEventListener("click", () => {
        chosen = cand.path;
        manual.value = cand.path;
        body.querySelectorAll(".cand").forEach((n) => n.classList.remove("on"));
        btn.classList.add("on");
      });
      body.appendChild(btn);
    }
    manual = field(body, "Path", chosen || "", "Full path to the .exe");
  }, () => override(game.id, { exe: manual.value.trim() }));
}

/* ---------- toast ---------- */

let toastTimer;
function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 5200 : 2600);
}

/* ---------- wiring ---------- */

$("search").addEventListener("input", (ev) => { state.query = ev.target.value; render(); });
$("sort").addEventListener("change", (ev) => { state.sort = ev.target.value; render(); });

$("rescan-btn").addEventListener("click", async () => {
  $("rescan-btn").disabled = true;
  toast("Rescanning…");
  await api("/api/rescan", { method: "POST" }).catch((e) => toast(e.message, true));
  setTimeout(refresh, 1200);
});

$("sizes-btn").addEventListener("click", async () => {
  $("sizes-btn").disabled = true;
  toast("Measuring folder sizes — this takes a while");
  await api("/api/sizes", { method: "POST" }).catch((e) => toast(e.message, true));
  setTimeout(refresh, 2000);
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeMenu(); closeModal(); return; }
  if (ev.key === "/" && document.activeElement !== $("search") && modal.hidden) {
    ev.preventDefault();
    $("search").focus();
  }
});

refresh();
setInterval(refresh, 30000);   // pick up running-state changes from the tracker
