"use strict";

/* UI state is persisted so the view survives a reload — the window is usually opened,
   used, and closed again rather than left running. */
const STORE_KEY = "gd.ui";

function loadState() {
  const base = {
    games: [],
    query: "",
    sort: "name",
    view: "installed",    // "installed" | "all"
    sources: new Set(),   // empty = all
    showHidden: false,
    busy: new Set(),
  };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    if (saved.sort) base.sort = saved.sort;
    if (saved.view) base.view = saved.view;
    if (Array.isArray(saved.sources)) base.sources = new Set(saved.sources);
    base.showHidden = !!saved.showHidden;
  } catch (err) {
    /* corrupt or unavailable storage is not worth failing the app over */
  }
  return base;
}

const state = loadState();

function saveState() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      sort: state.sort,
      view: state.view,
      sources: [...state.sources],
      showHidden: state.showHidden,
    }));
  } catch (err) { /* ignore */ }
}

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

// Only the fields that actually affect the DOM. The 30s poll almost always returns an
// identical library, and re-rendering an unchanged grid is what made it flicker.
function signature(games) {
  let out = "";
  for (const g of games) {
    out += `${g.id}\u0001${g.name}\u0001${g.source}\u0001${g.running ? 1 : 0}` +
           `\u0001${g.hidden ? 1 : 0}` +
           `\u0001${g.installed === false ? 0 : 1}\u0001${g.playtime_seconds || 0}` +
           `\u0001${g.last_played || 0}\u0001${g.size_bytes || 0}` +
           `\u0001${g.art_version || 0}\u0001${g.art_kind || ""}` +
           // The count is enough: the pill shows a number, and a companion's *name*
           // lives on its own record, which is already in this string.
           `\u0001${(g.companions || []).length}\u0002`;
  }
  return out;
}

let lastSignature = null;

async function refresh() {
  try {
    const data = await api("/api/games");
    $("sizes-btn").disabled = data.sizing;
    $("rescan-btn").disabled = data.scanning;
    state.scanning = data.scanning;
    if (data.scanning || data.sizing) setTimeout(refresh, 2500);

    const sig = signature(data.games);
    state.games = data.games;
    if (sig === lastSignature) return;   // nothing visible changed; leave the DOM alone
    lastSignature = sig;
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

const isInstalled = (g) => g.installed !== false;

function visibleGames() {
  const q = state.query.trim().toLowerCase();
  let list = state.games.filter((g) => {
    if (g.hidden && !state.showHidden) return false;
    if (state.view === "installed" && !isInstalled(g)) return false;
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
  const cmp = by[state.sort] || by.name;
  // Installed first in the "All" view, so the library you can actually play stays on top.
  return list.sort((a, b) => (isInstalled(b) - isInstalled(a)) || cmp(a, b));
}

/* ---------- rendering ---------- */

/* Cards are keyed by game id and reused. Rebuilding the grid would recreate every
   <img>, and a fresh <img> paints its empty background before the bytes are back —
   that is a full-grid blink on every poll, search keystroke and filter click. */
const cards = new Map();

function render() {
  const list = visibleGames();
  const seen = new Set();
  let prev = null;

  for (const game of list) {
    let el = cards.get(game.id);
    if (!el) {
      el = createCard(game);
      cards.set(game.id, el);
    }
    updateCard(el, game);
    seen.add(game.id);

    // Move only when the position is actually wrong; moving a node keeps its images.
    const target = prev ? prev.nextSibling : grid.firstChild;
    if (el !== target) grid.insertBefore(el, target);
    prev = el;
  }

  for (const [id, el] of cards) {
    if (!seen.has(id)) {
      el.remove();
      cards.delete(id);
    }
  }

  // Two different empty states: a search that matched nothing, and a library with
  // nothing in it yet. Showing "Nothing matches that search" to a new user describes a
  // problem they do not have.
  const bare = state.games.length === 0;
  $("empty").hidden = list.length > 0 || bare;
  $("firstrun").hidden = !(bare && !state.scanning);
  const pool = state.games.filter((g) => !g.hidden &&
    (state.view === "all" || isInstalled(g)));
  $("count").textContent = list.length === pool.length
    ? `${pool.length} games`
    : `${list.length} of ${pool.length}`;

  renderFilters();
}

function createCard(game) {
  const el = document.createElement("article");
  el.className = "card";
  el.tabIndex = 0;
  el.dataset.id = game.id;

  const img = document.createElement("img");
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  // `art_kind` from /api/games is only a guess used to style the tile before the image
  // decodes. Once it has, the image itself is the authority — a card built after that
  // payload was assembled arrives 600px wide and must not keep the shrunken icon
  // treatment it was optimistically given.
  img.addEventListener("load", () => {
    img.classList.toggle("is-icon", !!img.naturalWidth && img.naturalWidth < 200);
  });
  el.appendChild(img);

  const badge = document.createElement("span");
  badge.className = "badge";
  el.appendChild(badge);

  const more = document.createElement("button");
  more.className = "more";
  more.textContent = "\u22ef";
  more.title = "More";
  more.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const r = more.getBoundingClientRect();
    openMenu(el._game, r.left, r.bottom + 4);
  });
  el.appendChild(more);

  const label = document.createElement("div");
  label.className = "label";
  const title = document.createElement("span");
  title.className = "title";
  const meta = document.createElement("span");
  meta.className = "meta";
  label.append(title, meta);
  el.appendChild(label);

  // Handlers read el._game so a reused card never acts on a stale record.
  el.addEventListener("click", () => play(el._game));
  el.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); play(el._game); }
  });
  el.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    openMenu(el._game, ev.clientX, ev.clientY);
  });

  el._img = img;
  el._badge = badge;
  el._title = title;
  el._meta = meta;
  return el;
}

function updateCard(el, game) {
  el._game = game;
  const installed = isInstalled(game);

  el.classList.toggle("running", !!game.running);
  el.classList.toggle("hidden-game", !!game.hidden);
  el.classList.toggle("busy", state.busy.has(game.id));
  el.classList.toggle("not-installed", !installed);

  // Re-assigning src restarts the load even when the bytes are identical, so only do
  // it when the art has genuinely changed.
  const src = `/api/art?id=${encodeURIComponent(game.id)}&v=${game.art_version || 0}`;
  if (el._img.getAttribute("src") !== src) el._img.setAttribute("src", src);
  el._img.classList.toggle("is-icon", game.art_kind === "icon");

  const badge = game.running ? "Running"
    : (installed ? (SOURCE_LABEL[game.source] || game.source) : "Not installed");
  if (el._badge.textContent !== badge) el._badge.textContent = badge;

  if (el._title.textContent !== game.name) el._title.textContent = game.name;

  const bits = [];
  if (installed) {
    if (game.playtime_seconds) bits.push(fmtPlaytime(game.playtime_seconds));
    else if (game.last_played) bits.push(fmtWhen(game.last_played));
    if (game.size_bytes) bits.push(fmtSize(game.size_bytes));
  } else if (game.playtime_seconds) {
    bits.push(fmtPlaytime(game.playtime_seconds));
  }
  const mates = (game.companions || []).length;
  if (mates) bits.push(`+${mates} app${mates === 1 ? "" : "s"}`);
  const meta = bits.join(" · ");
  if (el._meta.textContent !== meta) el._meta.textContent = meta;
}

let lastFilters = null;

function renderFilters() {
  const counts = {};
  for (const g of state.games) {
    if (g.hidden && !state.showHidden) continue;
    if (state.view === "installed" && !isInstalled(g)) continue;
    counts[g.source] = (counts[g.source] || 0) + 1;
  }
  const hiddenCount = state.games.filter((g) => g.hidden).length;

  // The chip bar is small but rebuilding it on every poll is still visible churn.
  const sig = JSON.stringify([counts, [...state.sources], state.showHidden, hiddenCount]);
  if (sig === lastFilters) return;
  lastFilters = sig;

  const box = $("filters");
  box.textContent = "";

  const chip = (text, count, on, onClick) => {
    const b = document.createElement("button");
    b.className = "chip" + (on ? " on" : "");
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = count;
    b.append(document.createTextNode(text), n);
    b.addEventListener("click", onClick);
    box.appendChild(b);
  };

  for (const source of Object.keys(counts).sort()) {
    chip(SOURCE_LABEL[source] || source, counts[source], state.sources.has(source), () => {
      state.sources.has(source) ? state.sources.delete(source) : state.sources.add(source);
      saveState();
      render();
    });
  }

  if (hiddenCount) {
    chip("Hidden", hiddenCount, state.showHidden, () => {
      state.showHidden = !state.showHidden;
      saveState();
      render();
    });
  }
}

/* ---------- actions ---------- */

function play(game) {
  if (!game || state.busy.has(game.id)) return;
  // Installing is a much bigger commitment than launching — a stray click should not
  // start a 100 GB download.
  if (!isInstalled(game)) return confirmInstall(game);
  return start(game, `Launching ${game.name}`);
}

async function start(game, message) {
  state.busy.add(game.id);
  render();
  try {
    const res = await api("/api/launch",
                          { method: "POST", body: JSON.stringify({ id: game.id }) });
    const also = res.also || [];
    const failed = res.failed || [];
    // One toast, not three: #toast is a single element, so a second call stomps the
    // first. The game itself started either way -- only the companions can be partial.
    if (failed.length) toast(`${message} — ${failed.join("; ")}`, true);
    else if (also.length) toast(`${message} + ${also.join(", ")}`);
    else toast(message);
  } catch (err) {
    toast(`${game.name}: ${err.message}`, true);
  } finally {
    setTimeout(() => { state.busy.delete(game.id); render(); }, 2500);
  }
}

function confirmInstall(game) {
  openModal(`Install ${game.name}?`, (body) => {
    note(body,
      `${game.name} is in your library but not installed. This hands the download to ` +
      `${SOURCE_LABEL[game.source] || game.source}, which will ask you where to put it.`);
  }, () => start(game, `Installing ${game.name}`), "Install");
}

async function override(id, fields) {
  await api("/api/override", { method: "POST", body: JSON.stringify({ id, ...fields }) });
  lastSignature = null;   // force a re-render; the override changed something visible
  await refresh();
}

/* ---------- context menu ---------- */

const menu = $("menu");

function openMenu(game, x, y) {
  if (!game) return;
  menu.textContent = "";

  const add = (text, fn) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", () => { closeMenu(); fn(); });
    menu.appendChild(b);
  };

  const installed = isInstalled(game);
  add(installed ? "Launch" : "Install\u2026", () => play(game));
  if (installed && game.install_dir) {
    add("Open folder", async () => {
      try {
        await api("/api/reveal", { method: "POST", body: JSON.stringify({ id: game.id }) });
      } catch (err) { toast(err.message, true); }
    });
  }
  if (installed) add("Launch with\u2026", () => companionsDialog(game));
  menu.appendChild(document.createElement("hr"));
  // For an app you added, Edit is a superset of Rename -- and two ways to set the name,
  // writing to two different places, is how "renaming did nothing" bugs happen.
  if (game.user_added) add("Edit\u2026", () => appDialog(game));
  else add("Rename\u2026", () => renameDialog(game));
  if (installed && game.install_dir) add("Pick executable\u2026", () => exeDialog(game));
  add("Fix cover art\u2026", () => artDialog(game));
  menu.appendChild(document.createElement("hr"));
  add(game.hidden ? "Unhide" : "Hide", () => override(game.id, { hidden: !game.hidden }));
  // Removal is only offered for entries the user created; everything else would just
  // come back on the next rescan, which is what Hide is for.
  if (game.user_added) add("Remove\u2026", () => removeDialog(game));

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

function openModal(title, buildBody, save, saveLabel, danger) {
  $("modal-title").textContent = title;
  $("modal-save").textContent = saveLabel || "Save";
  // Both of these toggle in *both* directions on purpose: the button is shared, so a
  // one-way add would leave the next dialog red, or disabled, for no reason.
  $("modal-save").classList.toggle("danger", !!danger);
  $("modal-save").disabled = false;
  const body = $("modal-body");
  body.textContent = "";
  buildBody(body);
  onSave = save;
  modal.hidden = false;
  // An "add a row" box is not what a dialog is about, so it never takes opening focus
  // -- otherwise Settings would open with the cursor in the folder adder.
  const first = body.querySelector("input:not([data-nofocus])");
  if (first) {
    first.focus();
    // select() is only meaningful on a text field, and throws on a checkbox.
    if (first.type === "text" || first.type === "password") first.select();
  }
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

/* Three dialogs each carried their own copy of this paragraph as an inline style, and
   the settings panel had its own local section(). Both belong beside field/checkbox. */
function note(parent, text) {
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = text;
  parent.appendChild(p);
  return p;
}

function section(parent, title, hint) {
  const h = document.createElement("h3");
  h.textContent = title;
  parent.appendChild(h);
  return hint ? note(parent, hint) : null;
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
    note(body, "Art is matched against Steam by name. Set the Steam app ID to " +
               "correct a bad match, or point at a local image file.");
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
    note(body, `${data.candidates.length} candidate(s) found, best first.`);

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

/* ---------- adding an app by hand ---------- */

/* Re-quote anything with a space so the field round-trips through the server's splitter. */
function joinArgs(args) {
  return (args || []).map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(" ");
}

/* `game` null = add. Everything the edit form needs is already in /api/games, so there
   is no GET to make first. */
function appDialog(game) {
  const editing = !!game;
  let name, target, args, visible;

  openModal(editing ? `Edit — ${game.name}` : "Add a game or app", (body) => {
    note(body, "A path to a .exe, or a .lnk / .url shortcut, or a link like " +
               "steam://rungameid/570 or https://tracker.gg/\u2026");
    name = field(body, "Name", editing ? game.name : "", "Game or app name");
    target = field(body, "Target", editing ? (game.launch || {}).value : "",
                   "D:\\Games\\Thing\\Thing.exe");
    args = field(body, "Arguments", editing ? joinArgs((game.launch || {}).args) : "",
                 "optional");
    visible = checkbox(body, "Show in library", !(editing && game.hidden));
  }, async () => {
    const res = await api("/api/apps", {
      method: "POST",
      body: JSON.stringify({
        id: editing ? game.id : null,
        name: name.value.trim(),
        target: target.value.trim(),
        args: args.value.trim(),
        visible: visible.checked,
      }),
    });
    for (const warn of res.warnings || []) toast(warn, true);
    lastSignature = null;
    await refresh();
  }, editing ? "Save" : "Add");
}

function removeDialog(game) {
  openModal(`Remove ${game.name}?`, (body) => {
    note(body, "Removes the entry from your library. Nothing on disk is touched, and " +
               "adding it again under the same name brings its playtime back.");
  }, async () => {
    await api("/api/apps/remove",
              { method: "POST", body: JSON.stringify({ id: game.id }) });
    lastSignature = null;
    await refresh();
  }, "Remove", true);
}

/* ---------- companion apps ---------- */

function companionsDialog(game) {
  const chosen = new Set(game.companions || []);
  // Not-installed entries launch an *install* URL. A companion that silently starts a
  // 100 GB download is not a feature.
  const pool = state.games.filter((g) => g.id !== game.id && isInstalled(g));
  // An id can be stored but absent -- a folder id is path-derived, so an offline drive
  // makes its game vanish. Keep it listed and ticked rather than dropping it on save.
  const missing = [...chosen].filter((id) => !pool.some((g) => g.id === id));
  const rows = [];

  openModal(`Launch with — ${game.name}`, (body) => {
    note(body, `These start alongside ${game.name} when you launch it. Their own ` +
               `companion apps are not launched.`);

    const searchWrap = document.createElement("div");
    searchWrap.className = "field";
    const search = document.createElement("input");
    search.type = "text";
    search.placeholder = "Search\u2026";
    searchWrap.appendChild(search);
    body.appendChild(searchWrap);

    const list = document.createElement("div");
    list.className = "folders scroll";
    body.appendChild(list);

    const add = (id, label, tag, on) => {
      const row = document.createElement("label");
      row.className = "folder";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = on;
      box.addEventListener("change", () => {
        if (box.checked) chosen.add(id);
        else chosen.delete(id);
      });
      const text = document.createElement("div");
      text.innerHTML = "<b></b><em></em>";
      text.querySelector("b").textContent = label;
      text.querySelector("em").textContent = tag;
      row.append(box, text);
      list.appendChild(row);
      rows.push({ node: row, key: label.toLowerCase() });
    };

    for (const id of missing) add(id, id, "not in your library right now", true);
    // Ticked first, so what is already configured is visible without scrolling.
    const sorted = pool.slice().sort((a, b) =>
      (chosen.has(b.id) - chosen.has(a.id)) ||
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    for (const mate of sorted) {
      // A helper app is usually hidden from the grid, which is exactly the companion
      // shape -- so hidden entries are offered, and labelled.
      const tag = [SOURCE_LABEL[mate.source] || mate.source, mate.hidden ? "hidden" : ""]
        .filter(Boolean).join(" \u00b7 ");
      add(mate.id, mate.name, tag, chosen.has(mate.id));
    }

    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      for (const row of rows) row.node.hidden = !!q && !row.key.includes(q);
    });
  }, () => override(game.id, { companions: [...chosen] }));
}

/* ---------- settings ---------- */

/* Rows of folder paths with a checkbox each: detected candidates come pre-ticked when
   they are already configured, and anything already in the config that detection did
   not propose is appended so saving never silently drops it.

   Rows the user owns -- from their config, or typed into the Add box -- also carry a ✕.
   Unticking a mistyped path leaves it sitting in the list looking broken, which is not
   the same thing as deleting it. A detected row has no ✕ on purpose: unticking is
   already the right "no" there, and detection would propose it again next time. */
function folderList(parent, configured, detected) {
  const wrap = document.createElement("div");
  wrap.className = "folders";
  parent.appendChild(wrap);

  const rows = [];
  const add = (path, on, label, removable) => {
    if (rows.some((r) => r.path.toLowerCase() === path.toLowerCase())) return;
    const row = document.createElement("label");
    row.className = "folder";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = on;
    const text = document.createElement("div");
    text.innerHTML = "<b></b><em></em>";
    text.querySelector("b").textContent = path;
    text.querySelector("em").textContent = label;
    row.append(box, text);
    const entry = { path, box };

    if (removable) {
      const x = document.createElement("button");
      x.type = "button";
      x.className = "x";
      x.textContent = "\u2715";
      x.title = "Remove this folder";
      x.addEventListener("click", (ev) => {
        // The row is a <label> wrapping the checkbox, so without preventDefault the
        // click is forwarded to it and removing a row would toggle it on the way out.
        ev.preventDefault();
        ev.stopPropagation();
        row.remove();
        rows.splice(rows.indexOf(entry), 1);
      });
      row.appendChild(x);
    }

    wrap.appendChild(row);
    rows.push(entry);
  };

  for (const cand of detected) {
    const games = `${cand.game_count}${cand.sampled ? "+" : ""} game${cand.game_count === 1 ? "" : "s"}`;
    const where = cand.source === "steam" ? "Steam library — already covered by the Steam scanner"
                : cand.source === "drive-root" ? "drive root"
                : "found on disk";
    add(cand.path, cand.already_configured, `${games} · ${where}`, false);
  }
  for (const path of configured) add(path, true, "in your config", true);

  const adder = document.createElement("div");
  adder.className = "folder-add";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "D:\\Games";
  input.dataset.nofocus = "1";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost";
  btn.textContent = "Add";
  const commit = () => {
    const path = input.value.trim();
    if (!path) return;
    add(path, true, "added by you", true);
    input.value = "";
    wrap.appendChild(adder);
  };
  btn.addEventListener("click", commit);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
  });
  adder.append(input, btn);
  wrap.appendChild(adder);

  return () => rows.filter((r) => r.box.checked).map((r) => r.path);
}

/* extra_game_dirs: one game's own folder, so there is nothing to tick -- a row is either
   listed or gone. This had no UI before, and `readExtra` echoed the stored value back
   verbatim on every save, so removing one meant hand-editing config.json. */
function pathList(parent, paths) {
  const wrap = document.createElement("div");
  wrap.className = "folders";
  parent.appendChild(wrap);

  const rows = [];
  const adder = document.createElement("div");
  adder.className = "folder-add";
  wrap.appendChild(adder);           // appended first, so rows can insert before it

  const add = (path) => {
    if (rows.some((r) => r.path.toLowerCase() === path.toLowerCase())) return;
    const row = document.createElement("div");
    row.className = "folder";
    const text = document.createElement("div");
    text.innerHTML = "<b></b>";
    text.querySelector("b").textContent = path;
    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.textContent = "\u2715";
    x.title = "Remove this folder";
    const entry = { path };
    x.addEventListener("click", () => {
      row.remove();
      rows.splice(rows.indexOf(entry), 1);
    });
    row.append(text, x);
    wrap.insertBefore(row, adder);
    rows.push(entry);
  };

  for (const path of paths) add(path);

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "D:\\Fortnite";
  input.dataset.nofocus = "1";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost";
  btn.textContent = "Add";
  const commit = () => {
    const path = input.value.trim();
    if (!path) return;
    add(path);
    input.value = "";
  };
  btn.addEventListener("click", commit);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
  });
  adder.append(input, btn);

  return () => rows.map((r) => r.path);
}

function secretField(parent, label, info) {
  const input = field(parent, label, "",
    info.set ? `saved${info.hint ? ` \u00b7 ends ${info.hint}` : ""} — leave blank to keep`
             : "not set");
  input.type = "password";
  return input;
}

async function settingsDialog() {
  let data;
  try {
    data = await api("/api/settings");
  } catch (err) {
    return toast(err.message, true);
  }

  const cfg = data.config;
  let readRoots = () => cfg.scan_roots || [];
  let readExtra = () => cfg.extra_game_dirs || [];
  let apiKey, gridKey, steamId, owned, port, browser;

  openModal("Settings", (body) => {
    const hint = section(body, "Game folders",
                         "Folders holding one subfolder per game. Detecting\u2026");
    const holder = document.createElement("div");
    body.appendChild(holder);

    section(body, "Individual game folders",
            "A single game's own folder, when it does not sit inside a scan root above " +
            "— D:\\Fortnite, not D:\\Games.");
    // Nothing to detect here, so this reader is live from the moment the dialog opens.
    readExtra = pathList(body, cfg.extra_game_dirs || []);

    section(body, "Accounts");
    steamId = field(body, "Steam ID (64-bit)", cfg.steam_id || "", "76561198\u2026");
    apiKey = secretField(body, "Steam API key", data.secrets.steam_api_key);
    gridKey = secretField(body, "SteamGridDB key", data.secrets.steamgriddb_key);
    owned = checkbox(body, "Include games I own but have not installed", cfg.include_owned);

    section(body, "App");
    port = field(body, "Port", String(cfg.port ?? 8777));
    browser = select(body, "Browser", ["chrome", "edge", "default"], cfg.browser || "chrome");

    // Detection is a couple of seconds of disk work, so the modal opens first and the
    // folder list fills in when it lands. Saving before then would post the stored
    // scan_roots straight back -- which used to be harmless and now would silently undo
    // a removal, so Save waits.
    $("modal-save").disabled = true;
    api("/api/detect").then((found) => {
      holder.textContent = "";
      readRoots = folderList(holder, cfg.scan_roots || [], found.candidates || []);
      hint.textContent = found.candidates && found.candidates.length
        ? "Folders holding one subfolder per game. Tick the ones to scan, ✕ to remove."
        : "Nothing detected automatically — add your game folders below.";
    }).catch(() => {
      holder.textContent = "";
      readRoots = folderList(holder, cfg.scan_roots || [], []);
      hint.textContent = "Could not detect folders — add your game folders below.";
    }).then(() => { $("modal-save").disabled = false; });
  }, async () => {
    const payload = {
      scan_roots: readRoots(),
      extra_game_dirs: readExtra(),
      steam_id: steamId.value.trim(),
      include_owned: owned.checked,
      port: Number(port.value.trim()) || 8777,
      browser: browser.value,
    };
    // Blank means "keep what is stored" — the server never sent the value to begin with.
    if (apiKey.value.trim()) payload.steam_api_key = apiKey.value.trim();
    if (gridKey.value.trim()) payload.steamgriddb_key = gridKey.value.trim();

    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    for (const warn of res.warnings || []) toast(warn, true);
    if ((res.restart_required || []).length) {
      toast(`Restart the dashboard to apply: ${res.restart_required.join(", ")}`);
    }
    if ((res.reinstall_required || []).length) {
      toast(`Re-run "py install.py" to update the shortcut`);
    }
    lastSignature = null;
    refresh();
  }, "Save and rescan");
}

function checkbox(parent, labelText, on) {
  const wrap = document.createElement("label");
  wrap.className = "field check";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!on;
  const span = document.createElement("span");
  span.textContent = labelText;
  wrap.append(input, span);
  parent.appendChild(wrap);
  return input;
}

function select(parent, labelText, options, value) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const sel = document.createElement("select");
  for (const opt of options) {
    const o = document.createElement("option");
    o.value = opt;
    o.textContent = opt;
    sel.appendChild(o);
  }
  sel.value = value;
  wrap.append(label, sel);
  parent.appendChild(wrap);
  return sel;
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
$("sort").addEventListener("change", (ev) => {
  state.sort = ev.target.value;
  saveState();
  render();
});

for (const btn of document.querySelectorAll("#view-toggle button")) {
  btn.addEventListener("click", () => {
    if (state.view === btn.dataset.view) return;
    state.view = btn.dataset.view;
    saveState();
    syncViewToggle();
    render();
  });
}

function syncViewToggle() {
  for (const btn of document.querySelectorAll("#view-toggle button")) {
    btn.classList.toggle("on", btn.dataset.view === state.view);
    btn.setAttribute("aria-pressed", btn.dataset.view === state.view ? "true" : "false");
  }
}

$("add-btn").addEventListener("click", () => appDialog(null));
$("settings-btn").addEventListener("click", settingsDialog);
$("firstrun-btn").addEventListener("click", settingsDialog);

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

$("sort").value = state.sort;
syncViewToggle();
refresh();
setInterval(refresh, 30000);   // pick up running-state changes from the tracker
