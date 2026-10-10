"use strict";
/* MySearch UI — vanilla JS, no build step. Talks to the local daemon with the per-install token. */

const TOKEN = document.querySelector('meta[name="mse-token"]').content;
const UI_VERSION = document.querySelector('meta[name="mse-ui"]')?.content;
const APP_VERSION = document.querySelector('meta[name="mse-version"]')?.content;
const IN_APP = new URLSearchParams(location.search).get("app") === "1";
const MINI = new URLSearchParams(location.search).get("mini") === "1";   // the Quick Search panel (mini.js)
if (IN_APP) document.documentElement.classList.add("in-app");
if (MINI) document.documentElement.classList.add("mini");
/* Views pass optional pieces as `cond ? node : null`; skip null, undefined and false instead of printing "null". */
for (const m of ["replaceChildren", "append"]) {
  const orig = Element.prototype[m];
  Element.prototype[m] = function (...kids) { return orig.apply(this, kids.filter((k) => k !== null && k !== undefined && k !== false)); };
}
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const enc = encodeURIComponent;
const CHANNELS = [["keyword", "words"], ["meaning", "meaning"], ["translated", "translated"], ["visual", "visual"], ["audio", "audio"]];
const NARROW = matchMedia("(max-width: 820px)");
/* used until the service reports them (see the API notes in the agent report) */
const MSE_BIN_FALLBACK = "mse";
const SECRET_PATTERNS = [   // mirror of mse/ignore.py SECRET_PATTERNS
  "**/*credential*/", "**/*recoverycode*/", "**/*recovery_code*/", "**/*recovery-code*/", "**/*secret*/",
  "**/*recovery*code*", ".env", ".env.*", "**/.env", "**/.env.*",
  ".mcp.json", "**/mcp.json", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx", "*.ovpn",
  "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.ppk", ".ssh/", ".gnupg/", ".aws/", "Keychains/", "**/*credential*", "**/*secret*", "**/*password*",
];

const state = {
  scope: localGet("scope") || "all",
  collections: [],
  results: [],
  res: null,
  sel: -1,
  bridge: [],
  terms: [],
  query: "",            // the query the shown results belong to; favourites are keyed on it
  req: null,
  imageQuery: null,
  busy: false,
  year: null,
  settings: null,
  status: null,
  section: localGet("section") || "folders",
  open: new Set(loadList("open")),   // expanded folder cards
};
const fb = { available: null, items: [], pid: null };   // favourites: null = not known yet
const KIND_LABEL = { pdf: "PDF", office: "Office", text: "Notes & text", email: "E-mail", image: "Images", audio: "Audio", video: "Video", other: "Other" };
const ICON = {
  star: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z"/></svg>',
  hide: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M6.5 17.5l11-11"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>',
  mark: '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="/static/icon.svg#mark"/></svg>',
};

function localGet(k) { try { return localStorage.getItem("mse." + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("mse." + k, v); } catch { /* private window */ } }
function loadList(k) { try { return JSON.parse(localGet(k) || "[]"); } catch { return []; } }
function saveList(k, v) { localSet(k, JSON.stringify(v)); }

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "X-MSE-Token": TOKEN, ...(opts.body && !(opts.body instanceof Blob) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || `${r.status} ${r.statusText}`); e.status = r.status; throw e; }
  return j;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const fileUrl = (c, p) => `/api/file?c=${enc(c)}&path=${enc(p)}&t=${enc(TOKEN)}`;
const thumbUrl = (c, p) => `/api/thumb?c=${enc(c)}&path=${enc(p)}&t=${enc(TOKEN)}`;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
function size(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
function clock(t) { t = Math.round(t || 0); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; }
function ago(s) {
  if (!s) return "never";
  const t = typeof s === "number" ? s * (s < 1e12 ? 1000 : 1) : new Date(String(s).replace(" ", "T")).getTime();
  const d = (Date.now() - t) / 1000;
  if (!isFinite(d)) return String(s);
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}
const plural = (n, one, many = one + "s") => `${n.toLocaleString()} ${n === 1 ? one : many}`;
/* snippets arrive HTML-escaped with <mark>; mark words that came from the translation bridge in the second highlighter */
function markBridge(html) {
  if (!state.bridge.length) return html;
  return html.replace(/<mark>(.*?)<\/mark>/g, (m, w) =>
    state.bridge.some((b) => fold(w).startsWith(b.slice(0, Math.max(4, b.length - 3)))) && !state.terms.some((t) => fold(w).startsWith(fold(t).slice(0, 4)))
      ? `<mark class="tr">${w}</mark>` : m);
}
function fold(s) { return String(s).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/ß/g, "ss"); }

/* ------------------------------------------------------------------ native window: drag band
   The Mac app (app/MainWindowController.swift) drags the window from the header band, except over the controls reported here. */

const layoutBridge = window.webkit?.messageHandlers?.layout;
let layoutRaf = 0;
function reportLayout() {
  if (!layoutBridge || layoutRaf) return;
  layoutRaf = requestAnimationFrame(() => {
    layoutRaf = 0;
    const modal = !$("#help").hidden || !$("#drop").hidden || !!menuEl || !!$(".overlay.dlg");
    const top = $("#top").getBoundingClientRect();
    const rects = modal ? [] : $$("#top button, #top input, #top select, #top a, #top [data-nodrag]")
      .map((e) => e.getBoundingClientRect()).filter((r) => r.width && r.height)
      .map((r) => [r.left - 2, r.top - 2, r.width + 4, r.height + 4].map(Math.round));
    try { layoutBridge.postMessage({ band: modal ? 0 : Math.round(top.bottom), width: innerWidth, rects }); } catch { /* not in the app */ }
  });
}
if (layoutBridge) {
  const ro = new ResizeObserver(reportLayout);
  ro.observe($("#top")); ro.observe($("#scopes")); ro.observe($(".top-actions"));
  new MutationObserver(reportLayout).observe($("#top"), { childList: true, subtree: true });
  addEventListener("resize", reportLayout);
  addEventListener("load", reportLayout);
}

/* ------------------------------------------------------------------ scopes
   state.scope is "all", one folder or group name, or a comma list of folders (⌘-click). The service takes the
   same value as `c`. Tabs that do not fit go into the More menu, which also has a search and checkboxes. */

const GROUP_GLYPH = '<svg class="gl" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="4" width="9" height="9" rx="1.6"/><path d="M4.5 4V2.6c0-.6.5-1.1 1.1-1.1h7.8c.6 0 1.1.5 1.1 1.1v7.8c0 .6-.5 1.1-1.1 1.1H10.5"/></svg>';

async function loadCollections() {
  try {
    const j = await api("/api/collections");
    state.collections = j.collections || [];
    state.groups = j.groups || [];
    state.groupsApi = Array.isArray(j.groups);        // false until the service knows groups
    state.loaded = true;
  } catch (e) {
    state.collections = [];
  }
  renderScopes();
  if ($("#view-settings").hidden === false) {
    renderCollections();
    renderGroups();
    if (state.section === "privacy") renderPrivacy();
  }
}

const isGroup = (id) => (state.groups || []).some((g) => g.name === id);
function scopeLabel(sc = state.scope) {
  if (!sc || sc === "all") return "All";
  return sc.split(",").map((id) => (state.groups || []).find((g) => g.name === id)?.label || id).join(" + ");
}
/* the folders a scope searches */
function scopeCollections(sc = state.scope) {
  if (!sc || sc === "all") return state.collections.filter((c) => c.in_global).map((c) => c.name);
  const out = new Set();
  for (const id of sc.split(",")) {
    const g = (state.groups || []).find((x) => x.name === id);
    for (const n of g ? g.collections : [id]) out.add(n);
  }
  return [...out];
}
/* plain click: just this one; ⌘-click on a folder: add it to (or take it out of) the selection */
function setScope(id, additive) {
  let next = id;
  if (additive && id !== "all" && !isGroup(id)) {
    const cur = state.scope === "all" || state.scope.split(",").some(isGroup) ? [] : state.scope.split(",");
    const set = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    next = set.length ? set.join(",") : "all";
  }
  state.scope = next; localSet("scope", next);
  renderScopes(); runSearch();
}

/* ------------------------------------------------------------------ the scope shortcut: "w: invoice", "invoice in:work"
   The same rules as config.scope_shortcut in the service, which applies them (the query goes as typed). This copy
   only draws the chip, marks the shortcut in the field and suggests names; it never decides where a search runs. */

const RESERVED = new Set(["type", "kind", "ext", "in", "after", "before", "path", "has", "include", "exclude"]);   // config.FILTER_KEYS
const SC_PREFIX = /^\s*([\w-]+(?:\s*,\s*[\w-]+)*):(?=\s|["'“„]|$)/;
const SC_IN = /(?<!\S)in:(?:"([^"]*)"|([\w-]+(?:,[\w-]+)*))(?=\s|$)/gi;
const SC_QUOTED = /"[^"]*"|“[^”]*”|„[^“”]*[“”]/g;

/* every name a shortcut accepts -> { id: the folder / group / "all", kind, label, alias } */
function shortcutIndex() {
  const m = new Map([["all", { id: "all", kind: "all", label: "All", alias: false }]]);
  for (const c of state.collections) if (!m.has(c.name)) m.set(c.name, { id: c.name, kind: "folder", label: c.name, alias: false });
  for (const g of state.groups || []) if (!m.has(g.name)) m.set(g.name, { id: g.name, kind: "group", label: g.label || g.name, alias: false });
  for (const e of [...state.collections.map((c) => ["folder", c, c.name]), ...(state.groups || []).map((g) => ["group", g, g.label || g.name])])
    for (const a of e[1].aliases || []) if (!m.has(a)) m.set(a, { id: e[1].name, kind: e[0], label: e[2], alias: true });
  for (const k of [...m.keys()]) if (RESERVED.has(k)) m.delete(k);
  return m;
}

/* -> { tokens: [ids] | null, rest, ranges: [[start, end]] of the shortcut in q, unknown: names after in: that match nothing, label } */
function splitShortcut(q) {
  q = q || "";
  const idx = shortcutIndex();
  const tokens = [], ranges = [], unknown = [];
  const take = (names) => { for (const n of names) { const id = idx.get(n).id; if (!tokens.includes(id)) tokens.push(id); } };
  const m = SC_PREFIX.exec(q);
  if (m) {
    const names = m[1].split(",").map((n) => n.trim().toLowerCase());
    if (names.every((n) => idx.has(n))) { take(names); ranges.push([m[0].length - m[0].trimStart().length, m[0].length]); }
  }
  const quoted = [...q.matchAll(SC_QUOTED)].map((x) => [x.index, x.index + x[0].length]);
  for (const x of q.matchAll(SC_IN)) {
    if ([...quoted, ...ranges].some(([a, b]) => a <= x.index && x.index < b)) continue;
    const names = (x[1] ?? x[2]).split(",").map((n) => n.trim().toLowerCase()).filter(Boolean);
    if (!names.length) continue;
    if (names.every((n) => idx.has(n))) { take(names); ranges.push([x.index, x.index + x[0].length]); }
    else for (const n of names) if (!idx.has(n) && !unknown.includes(n)) unknown.push(n);
  }
  if (!ranges.length) return { tokens: null, rest: q, ranges, unknown, label: null };
  ranges.sort((a, b) => a[0] - b[0]);
  let rest = "", at = 0;
  for (const [a, b] of ranges) { rest += q.slice(at, a) + " "; at = b; }
  rest = (rest + q.slice(at)).replace(/\s+/g, " ").trim();
  return { tokens, rest, ranges, unknown, label: scopeLabel(tokens.join(",")) };
}

/* ------------------------------------------------------------------ scopes: tabs, the Folders ▾ picker, the scope menu */

function renderScopes() {
  const nav = $("#scopes");
  if (state.loaded) {
    const known = new Set(["all", ...state.collections.map((c) => c.name), ...(state.groups || []).map((g) => g.name)]);
    if (!state.scope.split(",").every((x) => known.has(x))) { state.scope = "all"; localSet("scope", "all"); }
  }
  const sel = new Set(state.scope.split(","));
  const total = state.collections.filter((c) => c.in_global).reduce((n, c) => n + (c.stats?.files || 0), 0);
  const tab = (id, label, n, group, title) => el("button", {
    "data-scope": id, class: group ? "group" : null, "aria-pressed": String(sel.has(id)), title,
    onclick: (e) => setScope(id, e.metaKey || e.ctrlKey),
  }, group ? el("span", { class: "glw", html: GROUP_GLYPH }) : null, label, n ? el("span", { class: "n" }, n.toLocaleString()) : null);
  const also = (x) => (x.aliases?.length ? ` · also ${x.aliases.map((a) => `${a}:`).join(" ")}` : "");
  nav.replaceChildren(
    tab("all", "All", total, false, "Every folder marked Include in All"),
    ...(state.groups || []).filter((g) => !g.hidden || sel.has(g.name))      // a group switched off as a tab shows only while chosen
      .map((g) => tab(g.name, g.label, g.files, true, `Group: ${g.collections.join(" + ")}${also(g)}`)),
    ...state.collections.map((c) => tab(c.name, c.name, c.stats?.files, false, `Only ${c.name}${also(c)} · ⌘-click to search it together with others`)));
  fitScopes();
  const multi = state.scope.includes(",");
  const box = $("#scope-multi");
  box.hidden = !multi;
  if (multi) box.replaceChildren(el("span", { class: "ml", title: scopeLabel() }, scopeLabel()),
    state.groupsApi ? el("button", { class: "linklike", onclick: () => saveGroup(state.scope.split(",")) }, "Save as group…") : null);
  qField?.update();
}

/* hide the tabs that do not fit (never All or a selected one) and count them on the Folders ▾ button */
function fitScopes() {
  const nav = $("#scopes");
  const tabs = $$("button[data-scope]", nav);
  tabs.forEach((t) => (t.hidden = false));
  let hidden = 0;
  for (const t of tabs.filter((t) => t.dataset.scope !== "all" && t.getAttribute("aria-pressed") !== "true").reverse()) {
    if (nav.scrollWidth <= nav.clientWidth + 1) break;
    t.hidden = true; hidden++;
  }
  const more = $("#scope-pick .sp-more");
  if (more) more.textContent = hidden ? `+${hidden}` : "";
  $("#scope-pick")?.setAttribute("title", `Choose folders and groups: tick several folders, pick a group${hidden ? ` · ${hidden} more not shown as tabs` : ""}`);
}
new ResizeObserver(() => { if (!MINI) fitScopes(); }).observe($("#top"));
$("#scope-pick")?.addEventListener("click", (e) => { e.stopPropagation(); openScopeMenu(mainScopeOps()); });

const mainScopeOps = () => ({ get: () => state.scope, set: (sc, additive) => setScope(sc, additive), anchor: $("#scope-pick") });

/* The scope menu, shared by the main window (a popover under Folders ▾) and Quick Search (inline, so the panel
   grows with it): a search, All, the groups (one at a time), and every folder with a checkbox to search several. */
function openScopeMenu(ops) {
  if (menuEl?.classList.contains("scope-menu")) { closeMenu(true); return; }
  closeMenu();
  menuReturn = document.activeElement;
  const input = el("input", { type: "search", placeholder: "Find a folder, group or short name", "aria-label": "Find a folder, group or short name", spellcheck: "false" });
  const list = el("div", { class: "sm-list", role: "listbox", "aria-label": "Folders and groups", "aria-multiselectable": "true" });
  const foot = el("footer");
  const chips = (x) => x.aliases?.length ? el("span", { class: "sm-al", title: `Short name${x.aliases.length > 1 ? "s" : ""}: type ${x.aliases.map((a) => `${a}: …`).join(" or ")}` }, x.aliases.map((a) => el("code", {}, a))) : null;
  const draw = () => {
    const sc = ops.get();
    const needle = fold(input.value.trim());
    const match = (s) => !needle || fold(s).includes(needle);
    const sel = new Set(sc.split(","));
    const multi = sc !== "all" && !sc.split(",").some(isGroup);
    const rows = [];
    if (match("all")) rows.push(el("button", { class: "sm-row", role: "option", "aria-selected": String(sc === "all"), onclick: () => { closeMenu(true); ops.set("all"); } },
      el("span", { class: "sm-radio" }), el("span", { class: "sm-name" }, "All"), el("span", { class: "sm-tag" }, "folders marked Include in All")));
    const groups = (state.groups || []).filter((g) => match(`${g.label} ${g.name} ${g.collections.join(" ")} ${(g.aliases || []).join(" ")}`));
    if (groups.length) rows.push(el("h4", {}, "Groups"));
    for (const g of groups) rows.push(el("button", { class: "sm-row", role: "option", "aria-selected": String(sel.has(g.name)), title: `${g.label}: ${g.collections.join(" + ")}`, onclick: () => { closeMenu(true); ops.set(g.name); } },
      el("span", { class: "sm-radio" }), el("span", { class: "sm-name" }, g.label), chips(g),
      g.hidden ? el("span", { class: "sm-tag", title: "Not shown as a tab (Settings › Folders › Groups)" }, "no tab") : null,
      el("span", { class: "n" }, (g.files || 0).toLocaleString())));
    const folders = state.collections.filter((c) => match(`${c.name} ${c.path} ${(c.aliases || []).join(" ")}`));
    const enabled = state.collections.filter((c) => c.enabled !== false).map((c) => c.name);
    if (folders.length) rows.push(el("h4", {}, "Folders", el("span", { class: "sm-bulk" },
      el("button", { type: "button", title: "Tick every folder, including those left out of All", onclick: () => { ops.set(enabled.join(",") || "all"); draw(); } }, "All"),
      el("button", { type: "button", title: "Untick every folder: back to All", onclick: () => { ops.set("all"); draw(); } }, "None"))));
    for (const c of folders) {
      const on = multi && sel.has(c.name);
      rows.push(el("div", { class: "sm-row folder", role: "option", "aria-selected": String(on || sel.has(c.name)) },
        el("input", { type: "checkbox", checked: on, title: "Search this folder together with the other ticked ones", "aria-label": `Include ${c.name}`,
          onchange: () => { ops.set(c.name, true); draw(); } }),
        el("button", { class: "sm-name", title: `Only ${c.name}`, onclick: () => { closeMenu(true); ops.set(c.name); } }, c.name), chips(c),
        !c.in_global ? el("span", { class: "sm-tag", title: "Left out of All (Settings › Folders)" }, "not in All") : null,
        el("span", { class: "n" }, (c.stats?.files || 0).toLocaleString())));
    }
    if (!rows.length) rows.push(el("p", { class: "sm-none" }, "No folder or group matches."));
    list.replaceChildren(...rows);
    const several = multi && sc.includes(",");
    foot.replaceChildren(el("span", { title: scopeLabel(sc) }, several ? scopeLabel(sc) : sc === "all" ? "Searching All · tick folders to pick several" : `Searching ${scopeLabel(sc)}`),
      several && state.groupsApi ? el("button", { class: "btn small", onclick: () => { closeMenu(true); saveGroup(sc.split(",")); } }, "Save as group…") : null);
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $(".sm-row button.sm-name, button.sm-row", list)?.click(); }
  });
  const hint = el("p", { class: "sm-hint" }, "Or type ", el("code", {}, "name: words"), " in the search box; ", el("code", {}, "in:name"), " works anywhere.");
  menuEl = el("div", { class: "menu scope-menu", role: "dialog", "aria-label": "Folders and groups" }, input, list, foot, hint);
  menuEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const items = $$("button.sm-row, .sm-row .sm-name, .sm-row input", menuEl), i = items.indexOf(document.activeElement);
      e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    }
    e.stopPropagation();
  });
  draw();
  ops.anchor?.setAttribute("aria-expanded", "true");
  menuEl._onClose = () => { ops.anchor?.setAttribute("aria-expanded", "false"); ops.onClose?.(); };
  if (ops.inline) { ops.inline.replaceChildren(menuEl); ops.inline.hidden = false; }
  else {
    document.body.append(menuEl);
    const r = ops.anchor.getBoundingClientRect();
    menuEl.style.top = `${r.bottom + 6}px`;
    menuEl.style.left = `${Math.max(8, Math.min(r.left, innerWidth - menuEl.offsetWidth - 8))}px`;
  }
  input.focus();
  reportLayout();
}

/* ------------------------------------------------------------------ the search field: shortcut mark, chip, name suggestions */

/* where the caret is asking for a name: after "wo:" at the start, or after "in:" anywhere */
function shortcutContext(value, caret) {
  const before = value.slice(0, caret);
  let m = /^(\s*(?:[\w-]+\s*,\s*)*)([\w-]+):$/.exec(before);
  if (m) return { kind: "prefix", partial: m[2].toLowerCase(), start: m[1].length, end: caret };
  m = /(?:^|\s)in:((?:[\w-]+,)*)([\w-]*)$/i.exec(before);
  if (m) return { kind: "in", partial: m[2].toLowerCase(), start: caret - m[2].length, end: caret };
  return null;
}

function shortcutField(o) {
  const f = { items: [], sel: 0, ctx: null };
  const close = () => { f.items = []; f.ctx = null; o.box.hidden = true; o.box.replaceChildren(); o.input.setAttribute("aria-expanded", "false"); o.onSuggest?.(); };
  f.close = close;
  f.update = () => {
    const q = o.input.value, sc = splitShortcut(q);
    if (o.mirror) {
      const parts = []; let at = 0;
      for (const [a, b] of sc.ranges) { parts.push(q.slice(at, a), el("mark", {}, q.slice(a, b))); at = b; }
      parts.push(q.slice(at));
      o.mirror.replaceChildren(...parts.filter((x) => x !== ""));
      o.mirror.scrollLeft = o.input.scrollLeft;
    }
    o.chip.hidden = !sc.tokens;
    if (sc.tokens) {
      o.chip.replaceChildren(el("span", { class: "qc-label" }, `in ${sc.label}`), el("span", { class: "qc-x", "aria-hidden": "true" }, "×"));
      o.chip.title = `This search runs in ${sc.label}, whichever tab is selected. Click to remove the shortcut.`;
      o.chip.setAttribute("aria-label", `Searching in ${sc.label}. Remove the shortcut`);
    }
    o.tabs?.()?.classList.toggle("overridden", !!sc.tokens);
    f.suggest();
    return sc;
  };
  f.suggest = () => {
    const ctx = document.activeElement === o.input && o.input.selectionStart === o.input.selectionEnd
      ? shortcutContext(o.input.value, o.input.selectionStart) : null;
    const idx = shortcutIndex();
    if (!ctx || idx.has(ctx.partial)) { close(); return; }          // a known name is a shortcut already
    const keys = [...idx.keys()];
    const hits = [...keys.filter((k) => k.startsWith(ctx.partial)), ...keys.filter((k) => !k.startsWith(ctx.partial) && ctx.partial && k.includes(ctx.partial))].slice(0, 8);
    if (!hits.length) { close(); return; }
    const same = f.ctx && f.ctx.kind === ctx.kind && f.ctx.start === ctx.start;
    f.ctx = ctx; f.items = hits; f.sel = same ? Math.min(f.sel, hits.length - 1) : 0;
    const what = (k) => { const e = idx.get(k); return e.kind === "all" ? "every folder marked Include in All" : e.alias ? `short for ${e.kind === "group" ? `the group ${e.label}` : e.id}` : e.kind === "group" ? `group · ${(state.groups || []).find((g) => g.name === e.id)?.collections.join(" + ") || ""}` : "folder"; };
    o.box.replaceChildren(...hits.map((k, i) => el("div", { class: "sg-row", role: "option", id: `${o.box.id}-${i}`, "aria-selected": String(i === f.sel),
      onmousedown: (e) => { e.preventDefault(); f.accept(i); } },
      el("span", { class: "sg-name" }, ctx.kind === "prefix" ? `${k}:` : `in:${k}`), el("span", { class: "sg-what" }, what(k)))),
      el("div", { class: "sg-foot" }, "⇥ or ↵ to take it · esc to keep typing"));
    o.box.hidden = false;
    o.input.setAttribute("aria-expanded", "true");
    o.input.setAttribute("aria-activedescendant", `${o.box.id}-${f.sel}`);
    if (!o.inline && o.place !== false) {
      const fr = o.input.getBoundingClientRect(), lr = o.input.closest("label")?.getBoundingClientRect() || fr;
      const pr = o.box.offsetParent?.getBoundingClientRect() || { left: 0, top: 0 };
      o.box.style.left = `${Math.max(0, fr.left - pr.left - 8)}px`;
      o.box.style.top = `${lr.bottom - pr.top + 6}px`;
    }
    o.onSuggest?.();
  };
  f.accept = (i) => {
    const k = f.items[i], ctx = f.ctx;
    if (!k || !ctx) return;
    const v = o.input.value, after = v.slice(ctx.end);
    const ins = ctx.kind === "prefix" ? `${k}: ` : `${k}${after.startsWith(" ") ? "" : " "}`;
    const tail = ctx.kind === "prefix" ? after.replace(/^\s+/, "") : after;
    o.input.value = v.slice(0, ctx.start) + ins + tail;
    const caret = ctx.start + ins.length;
    o.input.setSelectionRange(caret, caret);
    close();
    o.input.dispatchEvent(new Event("input", { bubbles: true }));
  };
  o.input.addEventListener("keydown", (e) => {
    if (!f.items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { f.sel = (f.sel + (e.key === "ArrowDown" ? 1 : -1) + f.items.length) % f.items.length; f.suggest(); }
    else if ((e.key === "Enter" && !e.altKey && !e.metaKey) || (e.key === "Tab" && !e.shiftKey)) f.accept(f.sel);
    else if (e.key === "Escape") close();
    else return;
    e.preventDefault(); e.stopImmediatePropagation();
  });
  const sync = () => { if (o.mirror) o.mirror.scrollLeft = o.input.scrollLeft; };
  o.input.addEventListener("input", f.update);
  o.input.addEventListener("scroll", sync);
  for (const ev of ["keyup", "click", "focus"]) o.input.addEventListener(ev, (e) => { sync(); if (e.type !== "keyup" || ["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) f.suggest(); });
  o.input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== o.input) close(); }, 0));
  o.chip.addEventListener("click", (e) => {
    e.preventDefault();
    o.input.value = splitShortcut(o.input.value).rest;
    o.input.focus();
    o.input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return f;
}
let qField = null;   // the main window's field (made once the page is wired, below)

async function saveGroup(collections, existing) {
  const suggestion = existing?.label || collections.join(" + ");
  const label = prompt(existing ? "New name for this group:" : "Name for this group (shown as a tab):", suggestion);
  if (!label?.trim()) return null;
  try {
    const g = await post("/api/groups", { label: label.trim(), collections, ...(existing ? { name: existing.name } : {}) });
    await loadCollections();
    if (!existing) setScope(g.name);
    toast(`${existing ? "Saved" : "Created"} the group ${g.label || label}`);
    return g;
  } catch (e) { toast(missingEndpoint(e) ? "Groups need a newer search service." : e.message); return null; }
}

/* ------------------------------------------------------------------ search */

let timer = null;
function scheduleSearch() { clearTimeout(timer); timer = setTimeout(runSearch, 220); }

function dateFilter() {
  const v = $("#f-date").value;
  if (!v) return null;
  const d = new Date();
  if (v === "year") return `${d.getFullYear()}-01-01`;
  d.setDate(d.getDate() - Number(v));
  return d.toISOString().slice(0, 10);
}
const FILTERS = ["#f-type", "#f-date", "#f-mode", "#f-include", "#f-exclude"];
function filtersActive() {
  return !!(state.year || $("#f-type").value || $("#f-date").value || $("#f-mode").value !== "hybrid"
    || $("#f-include").value.trim() || $("#f-exclude").value.trim());
}
function clearFilters() {
  state.year = null;
  for (const s of ["#f-type", "#f-date"]) $(s).value = "";
  $("#f-mode").value = "hybrid";
  for (const s of ["#f-include", "#f-exclude"]) { $(s).value = ""; localSet(s.slice(3), ""); }
  runSearch();
}

/* the filter fields as API parameters, shared by search and Ask */
function searchFilters() {
  const f = {};
  if ($("#f-type").value) f.type = $("#f-type").value;
  const after = dateFilter(); if (after) f.after = after;
  if (state.year) { f.after = `${state.year}-01-01`; f.before = `${Number(state.year) + 1}-01-01`; }
  if ($("#f-include").value.trim()) f.include = $("#f-include").value.trim();
  if ($("#f-exclude").value.trim()) f.exclude = $("#f-exclude").value.trim();
  return f;
}

async function runSearch() {
  const q = $("#q").value.trim();
  if (ask.q && ask.q !== q) clearAsk();
  for (const s of FILTERS) $(s).classList.toggle("on", !!$(s).value.trim() && $(s).value !== "hybrid");
  if (!q && !state.imageQuery) { clearResults(); return; }
  const sc = state.imageQuery ? null : splitShortcut(q);
  if (sc?.unknown.length) {           // "in:wo" while typing: wait for a name the service knows
    $(".results-head").hidden = false;
    $("#count").textContent = `No folder or group is called “${sc.unknown.join("”, “")}”: pick one from the list or the Folders ▾ menu`;
    return;
  }
  if (state.req) state.req.abort();
  const ctrl = new AbortController(); state.req = ctrl;
  const params = new URLSearchParams({ q, mode: $("#f-mode").value, limit: "40" });
  if (state.scope !== "all") params.set("c", state.scope);
  for (const [k, v] of Object.entries(searchFilters())) params.set(k, v);
  $("#count").textContent = "Searching…"; $(".results-head").hidden = false;
  try {
    let res;
    if (state.imageQuery) {
      const r = await fetch(`/api/search-image?${params}`, { method: "POST", body: state.imageQuery.blob, signal: ctrl.signal, headers: { "X-MSE-Token": TOKEN } });
      res = await r.json(); if (!r.ok) throw new Error(res.error);
    } else {
      const r = await fetch(`/api/search?${params}`, { signal: ctrl.signal, headers: { "X-MSE-Token": TOKEN } });
      res = await r.json(); if (!r.ok) throw new Error(res.error);
    }
    state.results = res.results;
    state.query = state.imageQuery ? "" : q;
    state.bridge = [...new Set(Object.values(res.bridge || {}).flat())];
    state.terms = (res.text || "").split(/\s+/).filter(Boolean);
    renderResults(res);
    renderFacets(res.facets, res.total);
    if (!state.imageQuery) { rememberSearch(q); refine(params, ctrl, res); }
  } catch (e) {
    if (e.name === "AbortError") return;
    $("#count").textContent = "";
    $("#results").replaceChildren();
    showEmpty("Search failed", e.message.includes("Failed to fetch") ? "The search service is not running. Start it with mse service install." : e.message);
  }
}

/* Instant results first; then let the re-ranker re-check close calls and quietly re-order (≈0.3-0.5 s). */
async function refine(params, ctrl, first) {
  if (first.results.length < 2) return;
  const p = new URLSearchParams(params); p.set("precise", "1");
  try {
    const r = await fetch(`/api/search?${p}`, { signal: ctrl.signal, headers: { "X-MSE-Token": TOKEN } });
    const res = await r.json();
    if (!r.ok || state.req !== ctrl) return;
    const before = first.results.map((x) => x.docid).join(), after = res.results.map((x) => x.docid).join();
    if (before === after) return;
    const keep = state.results[state.sel]?.docid;
    state.results = res.results;
    renderResults(res);
    const i = res.results.findIndex((x) => x.docid === keep);
    if (i > 0) select(i, false);
    $("#count").textContent += " · re-ranked";
  } catch { /* superseded by a newer search */ }
}

function clearResults() {
  clearAsk();
  state.results = []; state.sel = -1; state.res = null; state.query = "";
  $("#facets").hidden = true; $("#btn-pin").hidden = true; $(".results-head").hidden = true;
  $("#results").replaceChildren(); $("#count").textContent = ""; $("#bridge").hidden = true;
  $("#panes").classList.remove("show-preview");
  showEmpty();
  $("#preview").replaceChildren(previewEmpty("Select a result to preview it here."));
}

/* a real name for the tips: a short alias if there is one, else a group, else a folder */
function shortcutExample() {
  const withAlias = [...(state.groups || []), ...state.collections].find((x) => x.aliases?.length);
  return withAlias ? withAlias.aliases[0] : (state.groups || [])[0]?.name || state.collections[0]?.name || "work";
}

function showEmpty(title, text) {
  const box = $("#empty"); box.hidden = false;
  if (title) {
    box.replaceChildren(el("h2", {}, title), el("p", {}, text || ""),
      filtersActive() ? el("div", { class: "actions" }, el("button", { class: "btn small", onclick: clearFilters }, "Clear filters")) : null);
    return;
  }
  box.replaceChildren(
    el("div", { class: "empty-mark", html: ICON.mark }),
    el("h2", {}, "Search your folders"),
    el("p", {}, "Ask in English or German, or type something exact: a name, an invoice number, a case reference."),
    el("ul", { class: "tips" },
      el("li", {}, el("code", {}, "invoices from March 2025"), " dates in plain words"),
      el("li", {}, el("code", {}, "\"exact phrase\""), " these words in this order"),
      el("li", {}, el("code", {}, "*.pdf"), " in files to include, to narrow by type or folder"),
      el("li", {}, el("code", {}, `${shortcutExample()}: invoice`), " only that folder or group, whichever tab is on; ", el("code", {}, "in:" + shortcutExample()), " works anywhere"),
      el("li", {}, "Drop an image anywhere to find similar pictures and scans"),
      el("li", {}, "Questions: switch to ", el("button", { class: "linklike", onclick: () => setMode("ask") }, "Ask"), " (⌘J) for a short answer with references to your documents")),
    savedSearches());
}

function previewEmpty(msg) {
  const notch = (y, on) => on ? `<rect class="n" x="9" y="${y - 3}" width="10" height="6" rx="3"/>` : `<circle class="n" cx="18" cy="${y}" r="2.4"/>`;
  return el("div", { class: "preview-empty", html: `<div>
    <svg class="pe-card" viewBox="0 0 132 88" aria-hidden="true">
      <rect class="c" x="11.5" y="6.5" width="112" height="75" rx="7"/>
      ${[20, 31, 42, 53, 64].map((y, i) => notch(y, i === 0 || i === 2)).join("")}
      <rect class="h" x="31" y="29" width="46" height="9" rx="2"/>
      <path class="l" d="M33 22h66M33 33h76M33 44h56M33 55h70M33 66h40"/>
    </svg>
    <p>${esc(msg)}</p>
    <p class="keys-hint"><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>↵</kbd> open · <kbd>?</kbd> shortcuts</p></div>` });
}

/* ------------------------------------------------------------------ recent & pinned searches (this device only) */

let rememberTimer = null;
function rememberSearch(q) {
  clearTimeout(rememberTimer);
  if (q.length < 3) return;
  rememberTimer = setTimeout(() => {               // only searches the user paused on, not every keystroke
    const recent = loadList("recent").filter((x) => x.q !== q);
    recent.unshift({ q, scope: state.scope });
    saveList("recent", recent.slice(0, 12));
  }, 1500);
  const pinned = loadList("pinned").some((x) => x.q === q);
  const b = $("#btn-pin"); b.hidden = false; b.classList.toggle("on", pinned); b.textContent = pinned ? "★ Pinned" : "☆ Pin";
}
function togglePin() {
  const q = $("#q").value.trim(); if (!q) return;
  let pinned = loadList("pinned");
  if (pinned.some((x) => x.q === q)) pinned = pinned.filter((x) => x.q !== q);
  else pinned.unshift({ q, scope: state.scope });
  saveList("pinned", pinned.slice(0, 20));
  rememberSearch(q);
}
function savedSearches() {
  const wrap = el("div", { class: "saved-searches" });
  const run = (x) => { state.scope = x.scope || "all"; $("#q").value = x.q; renderScopes(); runSearch(); };   // renderScopes drops a scope that is gone
  for (const [title, key] of [["Pinned", "pinned"], ["Recent", "recent"]]) {
    const list = loadList(key);
    if (!list.length) continue;
    wrap.append(el("h3", {}, title), el("div", { class: "chip-row" }, list.map((x) =>
      el("button", { class: "chip", title: `in ${splitShortcut(x.q).label || scopeLabel(x.scope || "all")}`, onclick: () => run(x) }, x.q))));
  }
  return wrap;
}

/* ------------------------------------------------------------------ facets */

function renderFacets(f, total) {
  const box = $("#facets");
  box.replaceChildren();
  if (!f) { box.hidden = true; return; }
  const groups = [];
  const kindSel = $("#f-type").value;
  const kinds = Object.entries(f.kind || {});
  if (kinds.length > 1 || kindSel) groups.push(kinds.map(([k, n]) => el("button", {
    class: "chip" + (kindSel === k ? " on" : ""), title: kindSel === k ? "Show all types" : `Only ${KIND_LABEL[k] || k}`,
    "aria-pressed": String(kindSel === k),
    onclick: () => { $("#f-type").value = kindSel === k || !KIND_LABEL[k] || k === "other" ? "" : k; runSearch(); },
  }, KIND_LABEL[k] || k, el("b", {}, n))));
  const years = Object.entries(f.year || {});
  if (years.length > 1 || state.year) groups.push(years.slice(0, 6).map(([y, n]) => el("button", {
    class: "chip" + (state.year === y ? " on" : ""), title: state.year === y ? "Any year" : `Only ${y}`,
    "aria-pressed": String(state.year === y),
    onclick: () => { state.year = state.year === y ? null : y; runSearch(); },
  }, y, el("b", {}, n))));
  const colls = Object.entries(f.collection || {});
  if (colls.length > 1) groups.push(colls.map(([c, n]) => el("button", {
    class: "chip", title: `Only ${c}`, onclick: () => setScope(c),
  }, c, el("b", {}, n))));
  groups.forEach((g, i) => { if (i) box.append(el("span", { class: "fsep", "aria-hidden": "true" })); box.append(el("span", { class: "fgroup" }, g)); });
  box.hidden = !groups.length;
}

/* ------------------------------------------------------------------ results */

function renderResults(res) {
  state.res = res;
  $(".results-head").hidden = false;
  const n = res.results.length;
  const where = res.scope?.label || (state.scope !== "all" ? scopeLabel() : res.collections.length > 1 ? `${res.collections.length} folders` : res.collections[0] || "no folders");
  $("#count").textContent = state.imageQuery
    ? `${n} similar to ${state.imageQuery.name} · ${res.timings.total_ms} ms`
    : `${n} result${n === 1 ? "" : "s"} in ${where} · ${res.timings.total_ms} ms`;
  const inScope = new Set(res.scope?.shortcut ? res.collections : scopeCollections());
  const scope = state.collections.filter((c) => inScope.has(c.name));
  const items = scope.reduce((n, c) => n + (c.stats?.items || 0), 0);
  const waiting = scope.reduce((n, c) => n + (c.stats?.embed_pending || 0), 0);
  const heavy = scope.reduce((n, c) => n + (c.stats?.heavy_pending || 0), 0);
  if (items && (waiting / items > 0.02 || heavy > 0)) {
    const pct = Math.round(100 * (1 - waiting / items));
    $("#count").textContent += pct < 100 ? ` · index ${pct}% ready${heavy ? `, ${heavy.toLocaleString()} scans/media to go` : ""}` : ` · ${heavy.toLocaleString()} scans/media to go`;
  }
  $("#count").title = $("#count").textContent;
  const b = $("#bridge");
  if (state.bridge.length) {
    b.hidden = false;
    b.innerHTML = "Also searched " + state.bridge.map((t) => `<mark class="tr">${esc(t)}</mark>`).join(" ");
  } else b.hidden = true;
  if (!n) {
    $("#results").replaceChildren();
    const shortcut = res.scope?.shortcut;
    showEmpty("Nothing found", shortcut ? `Nothing in ${res.scope.label}. Take the shortcut out of the search to look in ${scopeLabel()}, or try other words.`
      : state.scope === "all"
      ? "Try other words, a different language, or fewer filters. Files waiting for the night run (OCR, audio, video) are not searchable by content yet."
      : `Nothing in ${scopeLabel()}. Try All, or other words.`);
    $("#preview").replaceChildren(previewEmpty("No result selected."));
    return;
  }
  $("#empty").hidden = true;
  renderList();
  const want = state.pendingSelect;
  const at = !want ? -1 : want.docid ? state.results.findIndex((r) => r.docid === want.docid)
    : state.results.findIndex((r) => r.collection === want.collection && r.path === want.path);
  state.pendingSelect = null;
  select(Math.max(0, at), at > 0);
  if (want?.docid) {
    const r = state.results[at];
    if (!r) openDoc(want.docid, want.page);
    else if (want.page) renderPreview({ ...r, hits: [{ page: want.page, t0: null, t1: null, modality: "text", why: ["link"], similarity: null, snippet: null }, ...r.hits.filter((h) => h.page !== want.page)] });
  }
  renderAsk();
}

function renderList() {
  const multi = (state.res?.collections.length || 0) > 1;
  $("#results").replaceChildren(...state.results.map((r, i) => resultCard(r, i, multi)));
  if (state.sel >= 0) markSelected(state.sel);
}

const canFeedback = () => fb.available === true && !!state.query && !state.imageQuery;
function favBadge(r) {
  if (r.favorite === "query") return el("span", { class: "tag fav", title: "You marked this as the best result for this search" }, "★ Best for this search");
  if (r.favorite === "similar") return el("span", { class: "tag fav sim", title: "You marked this as the best result for a similar search" }, "★ Best for a similar search");
  return null;
}
function starLabel(r) {
  return r.favorite === "query" ? "Best result for this search — click to unmark"
    : r.favorite === "similar" ? "Best for a similar search — click to make it best for this one too"
    : "Best result for this search (⌘D)";
}

function resultCard(r, i, multi) {
  const why = new Set(r.why.map((w) => (w === "name" ? "keyword" : w)));
  const notches = el("div", { class: "notches", "aria-hidden": "true" },
    CHANNELS.map(([k, label]) => el("i", { class: why.has(k) ? "on" : "", title: (why.has(k) ? "Matched by " : "Not matched by ") + label })));
  const hit = r.hits.find((h) => h.snippet) || r.hits[0];
  let where = "";
  if (hit?.page) where = `p.${hit.page}`;
  if (hit?.t0 != null) where = clock(hit.t0);
  const snip = hit?.snippet
    ? el("div", { class: "r-snip", html: (where ? `<span class="where">${esc(where)}</span>` : "") + markBridge(hit.snippet) })
    : el("div", { class: "r-snip" }, where ? el("span", { class: "where" }, where) : null,
      r.metadata_only ? "File name only — contents not indexed for this folder" : hit ? `${hit.modality} match` : "");
  const star = canFeedback() ? el("button", {
    class: "star" + (r.favorite === "query" ? " on" : r.favorite === "similar" ? " sim" : ""), html: ICON.star,
    title: starLabel(r), "aria-label": starLabel(r), "aria-pressed": String(r.favorite === "query"), tabindex: "-1",
    onclick: (e) => { e.stopPropagation(); toggleBest(r); },
  }) : null;
  const side = el("div", { class: "r-side" },
    el("div", { class: "r-top" }, el("span", { class: "r-date" }, r.date || ""), star),
    r.thumb ? el("img", { class: "r-thumb", src: thumbUrl(r.collection, r.path), alt: "", loading: "lazy" }) : null);
  const tags = el("div", { class: "r-tags" },
    favBadge(r),
    multi ? el("span", { class: "tag coll" }, r.collection) : null,
    el("span", { class: "tag" }, r.ext || r.kind),
    r.pages ? el("span", { class: "tag" }, `${r.pages} p`) : null,
    r.copies?.length ? el("span", { class: "tag versions", title: r.copies.map((x) => x.path).join("\n") }, `${r.copies.length + 1} versions`) : null,
    r.metadata_only ? el("span", { class: "tag lock" }, "name only") : null);
  const idx = () => state.results.indexOf(r);
  const li = el("li", {
    class: "r", role: "option", "aria-selected": "false", "data-i": i, style: `animation-delay:${Math.min(i, 12) * 18}ms`,
    title: `Matched by: ${r.why.join(", ")} · relevance ${r.relevance}%`,
    onclick: () => { select(idx()); if (NARROW.matches) $("#panes").classList.add("show-preview"); },
    ondblclick: () => openFile(r),
    oncontextmenu: (e) => { e.preventDefault(); select(idx(), false); openMenu(e.clientX, e.clientY, resultMenu(r)); },
  }, notches,
  el("div", { class: "r-title" }, r.title && r.kind !== "text" ? r.title : r.name),
  side,
  el("div", { class: "r-path" }, el("bdi", {}, r.path)),
  snip, tags);
  li._r = r;
  return li;
}

function markSelected(i) {
  $$(".r").forEach((x, k) => x.setAttribute("aria-selected", String(k === i)));
}

function select(i, scroll = true) {
  const items = $$(".r");
  if (!items.length) return;
  i = Math.max(0, Math.min(items.length - 1, i));
  markSelected(i);
  state.sel = i;
  if (scroll) items[i].scrollIntoView({ block: "nearest" });
  renderPreview(state.results[i]);
}

function resultMenu(r) {
  const fbItems = canFeedback() ? ["-",
    { label: "Best result for this search", key: "⌘D", checked: r.favorite === "query", run: () => toggleBest(r) },
    { label: "Not relevant for this search", run: () => hideResult(r) }] : [];
  return [
    { label: "Open", key: "↵", run: () => openFile(r) },
    { label: "Show in Finder", key: "⌘↵", run: () => openFile(r, true) },
    { label: "Copy path", run: () => copy(r.abs_path, "Path copied") },
    { label: "Copy for AI", run: () => copy(citation(r), "Citation copied") },
    ...fbItems, "-",
    r.metadata_only ? null : { label: "Find similar", run: () => findSimilar(r) },
    { label: "Re-index file", run: () => reindex(r) },
  ].filter(Boolean);
}

/* ------------------------------------------------------------------ favourites (best result / not relevant) */

async function probeFeedback() {
  const before = fb.available;
  try {
    const r = await api("/api/feedback");
    fb.available = true; fb.items = r.items || [];
  } catch (e) {
    if (missingEndpoint(e)) { fb.available = false; fb.items = []; }    // the service predates favourites
  }
  $("#nav-n-favourites").textContent = fb.available && fb.items.length ? fb.items.length : "";
  if (before !== fb.available && state.res) { renderList(); refreshPreviewFav(); }
  return fb.available;
}

const missingEndpoint = (e) => e.status === 404 && /no such endpoint/i.test(e.message);
function feedbackFailed(e) {
  if (missingEndpoint(e)) {
    fb.available = false;
    renderList(); refreshPreviewFav();
    toast("Favourites need a newer search service. Restart it after updating: mse service restart");
  } else toast(e.message);
}

async function toggleBest(r) {
  if (!canFeedback() || !r) return;
  const query = state.query;
  try {
    if (r.favorite === "query") {
      await post("/api/feedback/remove", { query, collection: r.collection, path: r.path });
      r.favorite = null;
      toast("No longer the best result for this search");
    } else {
      await post("/api/feedback", { query, collection: r.collection, path: r.path, kind: "best" });
      r.favorite = "query";
      toast(`Best result for “${query}”. It will come first next time.`);
    }
    refreshCard(r); refreshPreviewFav();
    probeFeedback();
  } catch (e) { feedbackFailed(e); }
}

async function hideResult(r) {
  if (!canFeedback() || !r) return;
  const query = state.query, i = state.results.indexOf(r);
  try {
    const j = await post("/api/feedback", { query, collection: r.collection, path: r.path, kind: "hide" });
    const li = $$(".r").find((x) => x._r === r);
    const finish = () => {
      const k = state.results.indexOf(r);
      if (k < 0) return;
      state.results.splice(k, 1);
      li?.remove();
      if (!state.results.length) { renderResults({ ...state.res, results: [] }); return; }
      state.sel = -1;
      select(Math.min(k, state.results.length - 1), false);
    };
    if (li && !matchMedia("(prefers-reduced-motion: reduce)").matches) { li.classList.add("leaving"); setTimeout(finish, 190); } else finish();
    toast("Hidden for this search", { label: "Undo", run: async () => {
      try {
        await post("/api/feedback/remove", j?.id != null ? { id: j.id } : { query, collection: r.collection, path: r.path });
        if (state.query === query && !state.results.includes(r)) {
          state.results.splice(Math.max(0, Math.min(i, state.results.length)), 0, r);
          renderList(); select(state.results.indexOf(r));
        }
        probeFeedback();
      } catch (e) { feedbackFailed(e); }
    } });
    probeFeedback();
  } catch (e) { feedbackFailed(e); }
}

function refreshCard(r) {
  const li = $$(".r").find((x) => x._r === r);
  if (!li) return;
  const fresh = resultCard(r, state.results.indexOf(r), (state.res?.collections.length || 0) > 1);
  fresh.style.animation = "none";
  fresh.setAttribute("aria-selected", li.getAttribute("aria-selected"));
  li.replaceWith(fresh);
}

function refreshPreviewFav() {
  const r = state.results[state.sel];
  const head = $(".pv-head");
  if (!r || !head || head._r !== r) return;
  const old = $(".pv-star", head), fresh = previewStar(r);
  if (old && fresh) old.replaceWith(fresh); else if (old) old.remove(); else if (fresh) $(".pv-titlerow", head).append(fresh);
  const badge = $(".pv-meta .tag.fav", head);
  badge?.remove();
  const nb = favBadge(r); if (nb) $(".pv-meta", head).prepend(nb);
  const hide = $(".pv-hide", head);
  if (hide) hide.hidden = !canFeedback();
}

function previewStar(r) {
  if (!canFeedback()) return null;
  return el("button", {
    class: "btn small pv-star" + (r.favorite === "query" ? " on" : r.favorite === "similar" ? " sim" : ""),
    title: starLabel(r), "aria-pressed": String(r.favorite === "query"), onclick: () => toggleBest(r),
    html: ICON.star + "<span>Best result</span>",
  });
}

/* ------------------------------------------------------------------ preview */

let previewSeq = 0;
async function renderPreview(r) {
  const seq = ++previewSeq;
  const pv = $("#preview");
  const meta = [];
  if (r.kind) meta.push(["Type", r.ext ? r.ext.toUpperCase() : r.kind]);
  if (r.date) meta.push(["Date", r.date]);
  if (r.pages) meta.push(["Pages", r.pages]);
  if (r.duration) meta.push(["Length", clock(r.duration)]);
  if (r.size) meta.push(["Size", size(r.size)]);
  if (r.lang && r.lang !== "unknown" && r.lang !== "other") meta.push(["Language", r.lang.toUpperCase()]);
  meta.push(["Folder", r.collection]);
  const head = el("div", { class: "pv-head" },
    el("button", { class: "btn small ghost pv-back", onclick: () => $("#panes").classList.remove("show-preview") }, state.mode === "ask" ? "← Chat" : "← Results"),
    el("div", { class: "pv-titlerow" },
      el("h2", { class: "pv-title" }, r.title && r.kind !== "text" ? r.title : r.name),
      previewStar(r)),
    el("div", { class: "pv-path" }, r.path, el("span", { class: "id", title: "Document id for agents and the CLI" }, "#" + r.docid)),
    el("div", { class: "pv-meta" }, favBadge(r), meta.map(([k, v]) => el("span", {}, el("b", {}, k), String(v)))),
    el("div", { class: "pv-actions" },
      el("button", { class: "btn primary small", onclick: () => openFile(r) }, "Open"),
      el("button", { class: "btn small", onclick: () => openFile(r, true) }, "Show in Finder"),
      el("button", { class: "btn small", onclick: () => copy(r.abs_path, "Path copied") }, "Copy path"),
      el("button", { class: "btn small", title: "Copy a citation with the matching passage, ready to paste into a chat", onclick: () => copy(citation(r), "Citation copied") }, "Copy for AI"),
      r.metadata_only ? null : el("button", { class: "btn small", onclick: () => findSimilar(r) }, "Find similar"),
      el("button", { class: "btn small", onclick: () => reindex(r) }, "Re-index file"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn small ghost pv-hide", hidden: !canFeedback(), title: "Leave this file out of this search from now on", onclick: () => hideResult(r) }, "Not relevant")),
    r.context ? el("p", { class: "pv-context" }, r.context) : null,
    r.copies?.length ? el("div", { class: "versions-list" }, el("b", {}, "Same document elsewhere"),
      r.copies.map((x) => el("button", { title: "Preview this version", onclick: () => renderPreview({ ...r, ...x, name: x.path.split("/").pop(), title: null, copies: [], hits: [], thumb: false, ext: x.path.split(".").pop() }) }, x.path))) : null);
  head._r = r;
  const hits = el("div", { class: "pv-hits" }, r.hits.map((h) => {
    let where = h.page ? `page ${h.page}` : h.t0 != null ? `${clock(h.t0)}–${clock(h.t1)}` : h.modality === "title" ? "file name" : "";
    return el("div", { class: "hit", onclick: () => jump(r, h) },
      el("span", { class: "why" }, [where, h.why.join(" + "), h.similarity != null ? `similarity ${h.similarity}` : ""].filter(Boolean).join(" · ")),
      h.snippet ? el("div", { html: markBridge(h.snippet) }) : el("div", {}, `${h.modality} match`));
  }));
  const body = el("div", { class: "pv-body", id: "pv-body" });
  pv.replaceChildren(head, hits, body);
  pv.scrollTop = 0;
  await renderBody(r, r.hits[0], body, seq);
}

async function renderBody(r, h, body, seq) {
  if (r.metadata_only) {
    body.replaceChildren(el("div", { class: "pv-note" },
      "This folder's rules index only this file's name, so its contents are not read or shown here. Use Open or Show in Finder to view it."));
    return;
  }
  const url = fileUrl(r.collection, r.path);
  if (r.kind === "pdf") {
    const page = h?.page ? `#page=${h.page}` : "";
    body.replaceChildren(el("iframe", { src: url + page, title: r.name }));
  } else if (r.kind === "image") {
    body.replaceChildren(el("img", { src: url, alt: r.name }));
  } else if (r.kind === "video") {
    body.replaceChildren(el("video", { src: url + (h?.t0 != null ? `#t=${Math.floor(h.t0)}` : ""), controls: true, preload: "metadata" }));
  } else if (r.kind === "audio") {
    body.replaceChildren(el("audio", { src: url + (h?.t0 != null ? `#t=${Math.floor(h.t0)}` : ""), controls: true, preload: "metadata" }));
  } else {
    body.replaceChildren(el("div", { class: "pv-note" }, "Loading text…"));
    try {
      const d = await api(`/api/doc?c=${enc(r.collection)}&ref=${enc(r.path)}&max=60000`);
      if (seq !== previewSeq) return;
      body.replaceChildren(textView(d.text || "(no text extracted)", d.truncated, /^(md|markdown|mdx)$/i.test(r.ext || "")));
      const first = body.querySelector("mark");
      if (first) first.scrollIntoView({ block: "center" });
    } catch (e) {
      body.replaceChildren(el("div", { class: "pv-note" }, e.message));
    }
  }
}

function foldMap(s) {
  /* folded text + index of the original character for each folded character ("ß" → "ss" shifts positions) */
  let out = "", map = [];
  for (let i = 0; i < s.length; i++) {
    const f = fold(s[i]);
    out += f;
    for (let k = 0; k < f.length; k++) map.push(i);
  }
  return [out, map];
}

/* returns text → HTML with the searched words in the highlighter (bridge words in mint) */
const STOP = new Set(("a an and are as at be by did do does for from how in is it its of on or that the this to was were what when where which "
  + "who why will with am an auf aus bei das dem den der des die ein eine einen einer für im ist mit oder sind und von war was wann wer wie wo zu zum zur").split(" "));
function highlighter() {
  const words = [...state.terms, ...state.bridge].map(fold).filter((w) => w.length > 1 && !STOP.has(w))
    .sort((a, b) => b.length - a.length).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!words.length) return esc;
  const rx = new RegExp(`(?<![\\p{L}\\p{N}])(${words.join("|")})`, "giu");
  return (text) => {
    const [folded, map] = foldMap(text);
    let html = "", pos = 0, m;
    rx.lastIndex = 0;
    while ((m = rx.exec(folded))) {
      const a = map[m.index], b = map[m.index + m[1].length - 1] + 1;
      if (a < pos) continue;
      const isBridge = state.bridge.some((x) => m[1].startsWith(fold(x).slice(0, 5))) && !state.terms.some((t) => m[1].startsWith(fold(t).slice(0, 4)));
      html += esc(text.slice(pos, a)) + `<mark${isBridge ? ' class="tr"' : ""}>` + esc(text.slice(a, b)) + "</mark>";
      pos = b;
    }
    return html + esc(text.slice(pos));
  };
}

const MAIL_FIELD = /^(Subject|From|To|Cc|Bcc|Reply-To|Date|Sent|Attachments):[ \t]*(.*)$/i;
const NUMERIC = /^[-+−]?(?:[$€£]\s?)?\d[\d.,'\s]*(?:\s?(?:%|€|EUR|AED|USD|PKR|\$))?$/i;
const trimBlank = (lines) => { let a = 0, b = lines.length; while (a < b && !lines[a].trim()) a++; while (b > a && !lines[b - 1].trim()) b--; return lines.slice(a, b); };

/* The document text with structure: "## " headings, spreadsheets ("Sheet: …" sections of "a | b | c" rows) as tables,
   and an e-mail's Subject/From/To/Date lines as a header block. */
function textView(text, truncated, md) {
  const hl = highlighter();
  const box = el("div", { class: "pv-text" });
  const sections = [];
  let cur = { heading: null, lines: [] };
  for (const line of text.split("\n")) {
    const m = /^## (.+)$/.exec(line);
    if (m) {
      if (cur.heading || cur.lines.some((l) => l.trim())) sections.push(cur);
      cur = { heading: m[1].trim(), lines: [] };
    } else cur.lines.push(line);
  }
  sections.push(cur);
  sections.forEach((s, k) => {
    if (s.heading) box.append(el("h4", { html: hl(s.heading) }));
    let lines = trimBlank(s.lines);
    if (k === 0) {
      let n = 0; const fields = [];
      for (; n < lines.length; n++) { const m = MAIL_FIELD.exec(lines[n]); if (!m) break; fields.push([m[1], m[2]]); }
      if (fields.length >= 2 && fields.some(([f]) => /^(from|subject)$/i.test(f))) {
        box.append(mailView(fields, hl));
        lines = trimBlank(lines.slice(n));
      }
    }
    if (!lines.length) return;
    const rows = lines.filter((l) => l.trim());
    if (/^sheet\b/i.test(s.heading || "") && rows.length >= 2 && rows.filter((l) => l.includes(" | ")).length >= rows.length / 2) {
      box.append(sheetView(rows, hl));
    } else if (md) appendMarkdown(box, lines, hl);
    else box.append(el("div", { class: "pv-para", html: hl(lines.join("\n")) }));
  });
  if (truncated) box.append(el("p", { class: "pv-note" }, "Showing the first part of this document. Open the file to read all of it."));
  return box;
}

/* Markdown notes: text with **bold** and `code`, and | pipe | tables | as tables */
function appendMarkdown(box, lines, hl) {
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$/.test(l);
  let text = [];
  const flush = () => { const t = text.join("\n").replace(/^\n+|\n+$/g, ""); if (t) box.append(el("div", { class: "pv-para", html: inlineMarkdown(hl(t)) })); text = []; };
  for (let i = 0; i < lines.length; i++) {
    if (isRow(lines[i]) && isSep(lines[i + 1] || "")) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(lines[i]), body = [];
      i += 2;
      while (i < lines.length && isRow(lines[i])) body.push(cells(lines[i++]));
      i--;
      flush();
      box.append(el("div", { class: "pv-table" }, el("table", { class: "sheet" },
        el("thead", {}, el("tr", {}, head.map((c) => el("th", { html: inlineMarkdown(hl(c)) })))),
        el("tbody", {}, body.map((r) => el("tr", {}, r.map((c) => el("td", { class: NUMERIC.test(c) ? "num" : null, html: inlineMarkdown(hl(c)) }))))))));
    } else text.push(lines[i]);
  }
  flush();
}

/* **bold** and `code` in Markdown notes; runs on already-escaped, highlighted HTML (neither marker is escaped) */
function inlineMarkdown(html) {
  return html.replace(/\*\*(?=\S)([^*\n]+?)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

function mailView(fields, hl) {
  const subject = fields.find(([f]) => /^subject$/i.test(f));
  return el("div", { class: "mail-head" },
    subject && subject[1] ? el("div", { class: "mail-subject", html: hl(subject[1]) }) : null,
    el("dl", {}, fields.filter((f) => f !== subject).map(([f, v]) => [el("dt", {}, f), el("dd", { html: hl(v) })])));
}

function sheetView(rows, hl) {
  const MAX = 400;
  const cells = rows.slice(0, MAX).map((l) => l.split(" | ").map((c) => c.trim()));
  const isNum = (c) => NUMERIC.test(c);
  const avgLen = cells.flat().reduce((n, c) => n + c.length, 0) / Math.max(1, cells.flat().length);
  if (avgLen > 70) return el("div", { class: "pv-para", html: hl(rows.join("\n")) });   // a cover or notes sheet: prose, not a table
  // the header is the first wide, all-text row followed by numbers; title rows above it become a caption
  const wide = Math.max(2, Math.ceil(Math.max(...cells.map((r) => r.length)) * 0.6));
  let h = cells.slice(0, 6).findIndex((r, i) => r.length >= wide && r.every((c) => !isNum(c)) && cells.slice(i + 1, i + 8).some((x) => x.some(isNum)));
  const caption = h > 0 ? cells.slice(0, h).map((r) => r.join(" · ")) : [];
  const head = h >= 0 ? cells[h] : null;
  const dateCol = new Set((head || []).map((c, i) => (/\b(date|datum|day|tag|paid|due|valuta)\b/i.test(c) ? i : -1)).filter((i) => i >= 0));
  const cell = (c, i) => {
    if (dateCol.has(i) && /^\d{5}(\.0+)?$/.test(c) && +c > 30000 && +c < 60000) {   // Excel's day serial (1900 system)
      const d = new Date(Date.UTC(1899, 11, 30) + Math.round(+c) * 864e5).toISOString().slice(0, 10);
      return el("td", { class: "date", title: `Spreadsheet date ${c}` }, d);
    }
    return el("td", { class: isNum(c) ? "num" : null, html: hl(c) });
  };
  const body = cells.slice(h >= 0 ? h + 1 : 0).map((r) => el("tr", {}, r.map(cell)));
  return el("div", { class: "pv-sheet" },
    caption.length ? el("p", { class: "sheet-cap", html: caption.map(hl).join("<br>") }) : null,
    el("div", { class: "pv-table" },
      el("table", { class: "sheet" },
        head ? el("thead", {}, el("tr", {}, head.map((c) => el("th", { html: hl(c) })))) : null,
        el("tbody", {}, body)),
      rows.length > MAX ? el("div", { class: "sheet-more" }, `${(rows.length - MAX).toLocaleString()} more rows — open the file to see them`) : null));
}

function jump(r, h) {
  const body = $("#pv-body");
  if (r.kind === "pdf" && h.page) body.replaceChildren(el("iframe", { src: fileUrl(r.collection, r.path) + `#page=${h.page}`, title: r.name }));
  const media = body.querySelector("video,audio");
  if (media && h.t0 != null) { media.currentTime = h.t0; media.play().catch(() => {}); }
}

async function findSimilar(r) {
  if (state.req) state.req.abort();
  state.imageQuery = null;
  $("#count").textContent = "Finding similar…";
  try {
    const res = await api(`/api/similar?c=${enc(r.collection)}&path=${enc(r.path)}&limit=40`);
    state.results = res.results; state.bridge = []; state.terms = []; state.query = "";
    $("#q").value = ""; $("#q").placeholder = `Similar to ${r.name} — type to search again`;
    renderResults({ ...res, collections: [res.collection], timings: res.timings || { total_ms: 0 } });
    $("#count").textContent = `${res.results.length} similar to ${r.name}`;
    $("#facets").hidden = true; $("#btn-pin").hidden = true;
  } catch (e) { toast(e.message); }
}

async function openFile(r, reveal = false) {
  try { await post("/api/open", { collection: r.collection, path: r.path, reveal }); } catch (e) { toast(e.message); }
}
async function reindex(r) {
  try { await post("/api/sync", { collection: r.collection, path: r.path }); toast("Re-indexing this file"); poll(true); } catch (e) { toast(e.message); }
}
function citation(r) {
  const plain = (h) => (h || "").replace(/<\/?mark[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  const lines = [`Source: ${r.name} — ${r.collection}/${r.path} (#${r.docid}${r.date ? ", " + r.date : ""})`];
  for (const h of r.hits.filter((x) => x.snippet).slice(0, 2)) {
    const where = h.page ? `p.${h.page}: ` : h.t0 != null ? `${clock(h.t0)}: ` : "";
    lines.push(`> ${where}${plain(h.snippet)}`);
  }
  lines.push(`Full text: mse get '#${r.docid}'`);
  return lines.join("\n");
}

function copy(text, msg) { navigator.clipboard?.writeText(text).then(() => toast(msg), () => toast(text)); }

let toastTimer;
function toast(msg, action) {
  let t = $("#toast");
  if (!t) { t = el("div", { id: "toast", class: "toast", role: "status" }); document.body.append(t); }
  t.replaceChildren(el("span", {}, msg),
    action ? el("button", { onclick: () => { t.hidden = true; action.run(); } }, action.label) : null);
  t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), action ? 6000 : 2600);
}

/* ------------------------------------------------------------------ context menu */

let menuEl = null, menuReturn = null;
function openMenu(x, y, items) {
  closeMenu();
  menuReturn = document.activeElement;
  menuEl = el("div", { class: "menu", role: "menu" }, items.map((it) => it === "-" ? el("hr") : el("button", {
    role: "menuitem", class: it.checked ? "checked" : null, title: it.title || null,
    onclick: () => { closeMenu(); it.run(); },
  }, it.key ? el("span", { class: "k" }, it.key) : null, el("span", { class: "ml" }, it.label))));
  menuEl.addEventListener("keydown", (e) => {
    const btns = $$("button", menuEl), i = btns.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); btns[(i + (e.key === "ArrowDown" ? 1 : -1) + btns.length) % btns.length].focus(); }
    else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); closeMenu(true); }
    e.stopPropagation();
  });
  document.body.append(menuEl);
  const r = menuEl.getBoundingClientRect();
  menuEl.style.left = `${Math.max(8, Math.min(x, innerWidth - r.width - 8))}px`;
  menuEl.style.top = `${Math.max(8, Math.min(y, innerHeight - r.height - 8))}px`;
  $("button", menuEl)?.focus({ preventScroll: true });
  reportLayout();
}
function closeMenu(restoreFocus) {
  if (!menuEl) return;
  const m = menuEl;
  menuEl = null; m.remove(); m._onClose?.(); reportLayout();
  if (restoreFocus) menuReturn?.focus?.({ preventScroll: true });
}
document.addEventListener("mousedown", (e) => {
  if (!menuEl || menuEl.contains(e.target)) return;
  if (menuEl.classList.contains("scope-menu") && e.target.closest("#scope-pick, .m-pick")) return;   // its button toggles it
  closeMenu();
}, true);
addEventListener("blur", () => closeMenu());
addEventListener("resize", () => closeMenu());
document.addEventListener("scroll", (e) => { if (!(menuEl && e.target instanceof Node && menuEl.contains(e.target))) closeMenu(); }, true);

/* ------------------------------------------------------------------ keyboard shortcuts overlay */

let helpReturn = null;
function showHelp() {
  closeMenu();
  helpReturn = document.activeElement;
  $("#help").hidden = false;
  $("#help [data-close]").focus();
  reportLayout();
}
function hideHelp() {
  $("#help").hidden = true;
  reportLayout();
  helpReturn?.focus?.({ preventScroll: true });
}
$("#help").addEventListener("mousedown", (e) => { if (e.target === e.currentTarget) hideHelp(); });
$("#help [data-close]").addEventListener("click", hideHelp);

/* ------------------------------------------------------------------ image search (drag & drop) */

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (!MINI && [...e.dataTransfer.types].includes("Files")) { dragDepth++; $("#drop").hidden = false; reportLayout(); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop").hidden = true; reportLayout(); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("#drop").hidden = true; reportLayout();
  const f = [...e.dataTransfer.files].find((x) => x.type.startsWith("image/"));
  if (!f) { toast("Drop an image file (JPG, PNG, HEIC…)"); return; }
  state.imageQuery = { blob: f, name: f.name };
  $("#q").value = ""; $("#q").placeholder = `Searching by picture: ${f.name} — type to search by words again`;
  showSearch(); runSearch();
});

/* ------------------------------------------------------------------ settings: shell */

const SECTIONS = ["folders", "search", "wholemac", "ask", "schedule", "favourites", "agents", "privacy", "app", "about"];

function showSettings(section) {
  closeMenu();
  $("#view-search").hidden = true; $("#view-settings").hidden = false;
  $("#btn-settings").setAttribute("aria-pressed", "true");
  goSection(SECTIONS.includes(section) ? section : state.section);
  renderCollections(); renderGroups(); loadSettings(); poll(true); probeFeedback();
}
function showSearch() {
  $("#view-settings").hidden = true; $("#view-search").hidden = false;
  (state.mode === "ask" ? $("#ask-q") : $("#q")).focus();
  $("#btn-settings").setAttribute("aria-pressed", "false");
}

function goSection(sec) {
  if (!SECTIONS.includes(sec) || $(`#set-nav [data-sec="${sec}"]`)?.hidden) sec = "folders";
  state.section = sec; localSet("section", sec);
  for (const s of $$(".set-sec")) s.hidden = s.dataset.sec !== sec;
  $$("#set-nav button").forEach((b) => (b.dataset.sec === sec ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
  $("#set-body").scrollTop = 0;
  if (sec === "favourites") renderFavourites();
  if (sec === "agents") { renderAgents(); renderGlobalAgents(); }
  if (sec === "privacy") renderPrivacy();
  if (sec === "about") renderAbout();
  if (sec === "ask") loadProviders();
  if (sec === "wholemac") wmTick();
}
$("#set-nav").addEventListener("click", (e) => { const b = e.target.closest("button[data-sec]"); if (b) goSection(b.dataset.sec); });
$("#set-nav").addEventListener("keydown", (e) => {
  if (!["ArrowDown", "ArrowUp"].includes(e.key)) return;
  e.preventDefault();
  const shown = SECTIONS.filter((x) => !$(`#set-nav [data-sec="${x}"]`).hidden);
  const i = shown.indexOf(state.section) + (e.key === "ArrowDown" ? 1 : -1);
  const sec = shown[(i + shown.length) % shown.length];
  goSection(sec); $(`#set-nav button[data-sec="${sec}"]`).focus();
});
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-goto]");
  if (a) { e.preventDefault(); goSection(a.dataset.goto); }
});

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    state.settings = s;
    const f = $("#settings-form");
    f.nightly_at.value = s.schedule.nightly_at; f.nightly_enabled.checked = s.schedule.nightly_enabled;
    f.only_on_ac_power.checked = s.schedule.only_on_ac_power; f.scan_every_min.value = s.schedule.scan_every_min;
    f.live_embed_limit.value = s.schedule.live_embed_limit; f.idle_unload_min.value = s.worker.idle_unload_min;
    const g = $("#search-form");
    g.limit.value = s.search.limit ?? 20;
    g.bridge.checked = !!(s.translation.enabled && s.translation.corpus_bridge);
    g.rerank_enabled.checked = !!s.search.rerank_enabled;
    g.hub_penalty.value = s.search.hub_penalty ?? 0.6;
    showHub();
    const p = $("#privacy-form");
    p.redact.checked = s.agents ? s.agents.redact !== false : localGet("redact") !== "0";
    renderAskCloudHead();
  } catch { /* daemon down; status line says so */ }
}
function showHub() { $("#s-hub-out").textContent = `×${Number($("#s-hub").value).toFixed(2)}`; }
$("#s-hub").addEventListener("input", showHub);

async function saveForm(form, body, after) {
  const out = $(".saved", form);
  out.classList.remove("err");
  try {
    await post("/api/settings", body);
    out.textContent = "Saved.";
    after?.();
    setTimeout(() => { if (out.textContent === "Saved.") out.textContent = ""; }, 2500);
  } catch (err) { out.textContent = err.message; out.classList.add("err"); }
}

$("#settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  saveForm(f, {
    schedule: { nightly_at: f.nightly_at.value, nightly_enabled: f.nightly_enabled.checked, only_on_ac_power: f.only_on_ac_power.checked,
      scan_every_min: Number(f.scan_every_min.value), live_embed_limit: Number(f.live_embed_limit.value) },
    worker: { idle_unload_min: Number(f.idle_unload_min.value) },
  }, () => poll(true));
});
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  saveForm(f, {
    search: { limit: Math.max(1, Math.min(100, Number(f.limit.value) || 20)), rerank_enabled: f.rerank_enabled.checked, hub_penalty: Number(f.hub_penalty.value) },
    translation: { enabled: f.bridge.checked, corpus_bridge: f.bridge.checked },
  }, () => poll(true));
});
$("#privacy-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  saveForm(f, { agents: { redact: f.redact.checked } }, () => localSet("redact", f.redact.checked ? "1" : "0"));
});

/* ------------------------------------------------------------------ settings: whole-Mac search
   mac_index.roots, mac_index.exclude and mac_index.skip_protected (mse/macindex.py). GET /api/files/status says what
   the entries mean and how the crawl and Full Disk Access stand; GET /api/settings has the raw lists. An edit reads the
   raw list again, changes it and posts the whole list, so the other entries go back unchanged; the service validates
   it (a 400 with a readable message) and the built-in rules cannot be changed from here. */

const wm = { st: null, cfg: null, timer: 0, gen: 0 };
const wmMsg = (e) => String(e?.message || e).replace(/^(ValueError|KeyError|PermissionError): /, "");

async function wmLoad() {
  try {
    const [st, s] = await Promise.all([api("/api/files/status"), api("/api/settings")]);
    wm.st = st; wm.cfg = s.mac_index || {};
  } catch { wm.st = null; }
  wmRender();
}
function wmTick() {            // one refresh loop while the section is open; opening it again restarts it, never doubles it
  clearTimeout(wm.timer);
  const gen = ++wm.gen;
  if ($("#view-settings").hidden || state.section !== "wholemac") return;
  wmLoad().finally(() => { if (gen === wm.gen) wm.timer = setTimeout(wmTick, wm.st?.crawling ? 1500 : 6000); });
}

function wmNotice(kind, text, rescan) {
  const n = $("#wm-notice");
  if (!text) { n.hidden = true; return; }
  n.className = `wm-notice ${kind}`;
  n.replaceChildren(el("span", {}, text),
    rescan ? el("button", { class: "linklike", type: "button", onclick: wmRescan }, "Rescan now") : null);
  n.hidden = false;
}
const WM_APPLIES = "Names in skipped folders leave the results at once. The index catches up in the background within a few seconds.";

async function wmRescan() {
  wmNotice();
  try {
    const r = await post("/api/files/crawl", { force: true });
    toast(r.started ? "Scanning started." : r.reason === "already crawling" ? "Already scanning." : `Not started: ${r.reason || "unknown reason"}`);
  } catch (e) { wmNotice("err", wmMsg(e)); }
  wmLoad();
}
$("#wm-rescan").addEventListener("click", wmRescan);

/* change one list of mac_index: mutate(list) returns the new list (or null to do nothing); true when it was saved */
async function wmEdit(key, mutate, done) {
  try {
    const cur = (await api("/api/settings")).mac_index || {};
    const next = mutate([...(cur[key] || [])]);
    if (!next) return false;
    await post("/api/settings", { mac_index: { [key]: next } });
    wmNotice("ok", `${done} ${WM_APPLIES}`, true);
    return true;
  } catch (e) { wmNotice("err", wmMsg(e)); return false; }
  finally { wmLoad(); }
}
async function wmPick(key, done) {
  let path;
  try { ({ path } = await post("/api/pick-folder")); } catch (e) { wmNotice("err", wmMsg(e)); return; }
  if (path) wmEdit(key, (l) => [...l, path], done(path));
}
$("#wm-root-add").addEventListener("click", () => wmPick("roots", (p) => `Now searching ${p}.`));
$("#wm-skip-add").addEventListener("click", () => wmPick("exclude", (p) => `Skipping ${p}.`));
$("#wm-skip-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#wm-skip-input"), v = input.value.trim();
  if (!v) return;
  if (await wmEdit("exclude", (l) => [...l, v], `Skipping ${v}.`)) input.value = "";
});
$("#wm-skip-protected").addEventListener("change", async (e) => {
  const on = e.target.checked;
  try {
    await post("/api/settings", { mac_index: { skip_protected: on ? "auto" : "never" } });
    wmNotice("ok", on ? "Protected folders are skipped again. The index catches up in the background within a few seconds."
      : "Protected folders will be read, and macOS may ask for permission the first time. The index catches up in the background within a few seconds.", true);
  } catch (err) { wmNotice("err", wmMsg(err)); }
  wmLoad();
});

function wmRow(main, note, remove) {
  return el("li", { class: "wm-item" },
    el("div", { class: "wm-main" }, main, note ? el("small", { class: `wm-note${note.bad ? " bad" : ""}` }, note.text) : null),
    remove);
}

function wmRender() {
  const st = wm.st, cfg = wm.cfg || {};
  const facts = $("#wm-status");
  $("#wm-rescan").disabled = !st || !st.enabled || st.crawling;
  $("#wm-rescan").textContent = st?.crawling ? "Scanning…" : "Rescan now";
  if (!st) {
    facts.replaceChildren(el("dt", {}, "State"), el("dd", {}, "The search service did not answer. Is it running?"));
    for (const id of ["#wm-roots", "#wm-skips", "#wm-hard"]) $(id).replaceChildren();
    return;
  }
  // ---- status
  const pct = Math.round((st.progress?.fraction || 0) * 100);
  const stateText = { disabled: "Switched off", crawling: `Indexing, ${pct}% done`, ready: "Ready", waiting: "Waiting to start", "not crawled": "Not indexed yet" }[st.state] || st.state;
  const fda = { true: "Granted", false: "Not granted", null: "Not known" }[String(st.full_disk_access)];
  facts.replaceChildren(
    el("dt", {}, "State"), el("dd", {}, stateText,
      st.state === "crawling" ? el("div", { class: "bar wm-bar" }, el("span", { style: `width:${pct}%` })) : null,
      st.state === "crawling" && st.progress?.current ? el("div", { class: "wm-cur mono" }, st.progress.current) : null),
    el("dt", {}, "Names indexed"), el("dd", {}, st.entries.toLocaleString() + (st.state === "crawling" ? " so far" : "")),
    el("dt", {}, "Last update"), el("dd", {}, st.last_update ? ago(st.last_update) : "never"),
    el("dt", {}, "Full Disk Access"), el("dd", {}, fda),
    st.error ? el("dt", {}, "Last error") : null, st.error ? el("dd", { class: "wm-err" }, st.error) : null);

  // ---- roots
  const roots = cfg.roots || [], searched = new Set(st.roots || []);
  $("#wm-roots").replaceChildren(...roots.map((r) => wmRow(el("code", { class: "wm-path" }, r),
    searched.has(r) || !st.enabled ? null : { text: "Not searched: the folder is missing, or it is inside a place that is never indexed.", bad: true },
    el("button", { class: "btn small ghost", type: "button", disabled: roots.length < 2, title: roots.length < 2 ? "At least one folder is needed" : null,
      "aria-label": `Stop searching ${r}`, onclick: () => wmEdit("roots", (l) => l.filter((x) => x !== r), `Stopped searching ${r}.`) }, "Remove"))));

  // ---- skipped folders
  const entries = st.exclude_entries || [];
  const bad = new Set(entries.filter((e) => e.invalid).map((e) => e.entry));
  $("#wm-skips").replaceChildren(...(entries.length ? entries.map((e) => wmRow(
    el("code", { class: "wm-path" }, e.path || e.entry, e.kind === "pattern" && !e.invalid ? el("span", { class: "wm-tag" }, "name or pattern") : null),
    e.invalid ? { text: `Not valid any more, so no change can be saved until it is removed. ${e.invalid}`, bad: true } : e.note ? { text: e.note } : null,
    el("button", { class: "btn small ghost", type: "button", "aria-label": `Stop skipping ${e.path || e.entry}`,
      onclick: () => wmEdit("exclude", (l) => l.filter((x) => x !== e.entry && !(bad.has(e.entry) && bad.has(x))),
        bad.has(e.entry) && bad.size > 1 ? `Removed ${bad.size} entries that were not valid.` : `Stopped skipping ${e.path || e.entry}.`) }, "Remove")))
    : [el("li", { class: "wm-empty" }, "Nothing skipped by you. The built-in rules below still apply.")]));

  // ---- always skipped
  $("#wm-hard").replaceChildren(...(st.hard_excluded || []).map((g) => el("details", { class: "wm-group" },
    el("summary", {}, g.label, el("span", { class: "n" }, String(g.items.length))),
    el("div", { class: "chips mono" }, g.items.map((i) => el("span", {}, i))))));

  // ---- folders macOS protects
  const mode = cfg.skip_protected || "auto", skipped = st.skipped_protected || [];
  const sw = $("#wm-skip-protected");
  sw.checked = mode !== "never";
  $("#wm-protect-note").textContent = (mode === "always"
    ? "Set to always in the configuration: they are skipped even when Full Disk Access is granted. Switching off reads them. " : "")
    + "Desktop, Documents, Downloads, Movies, Music, Pictures and iCloud Drive. macOS asks before an app reads them, so while Full Disk Access is missing they are skipped and no permission dialog appears.";
  const box = $("#wm-fda");
  const real = st.python_real_path || st.python_path;
  if (st.full_disk_access === false && skipped.length) {
    box.hidden = false;
    box.replaceChildren(el("div", { class: "wm-fda-body" },
      el("p", {}, `Full Disk Access is not granted, so ${skipped.length === 1 ? "this folder is" : "these folders are"} not searched:`),
      el("div", { class: "chips mono" }, skipped.map((p) => el("span", {}, p))),
      el("p", {}, "To include them:"),
      el("ol", { class: "wm-steps" },
        el("li", {}, "Open System Settings › Privacy & Security › Full Disk Access."),
        el("li", {}, "Press + and add this program, the one the MySearch service runs under:",
          el("div", { class: "token" }, el("code", { class: "wm-py" }, real),
            el("button", { class: "btn small", type: "button", onclick: () => copy(real, "Path copied") }, "Copy path")),
          st.python_path && st.python_path !== real ? el("small", { class: "wm-note" }, `The service starts it as ${st.python_path}, which points to this file.`) : null),
        el("li", {}, "Restart the service so it picks the permission up: run ", el("code", {}, "mse service restart"), " in Terminal."))));
  } else {
    box.hidden = false;
    box.replaceChildren(el("div", { class: "wm-fda-body" }, el("p", { class: "wm-fine" },
      mode === "never" ? "Skipping is off. macOS may ask for permission the first time each of these folders is read."
      : st.full_disk_access === true ? "Full Disk Access is granted, so these folders are searched."
      : "Whether Full Disk Access is granted could not be determined.")));
  }
}

/* ------------------------------------------------------------------ settings: folders */

function collState(c) {
  const s = c.stats;
  if (!c.exists) return ["danger", "Folder not found"];
  if (c.enabled === false) return ["", "Paused"];
  if (!s) return ["warn", "Not indexed yet"];
  if (state.status?.jobs?.current?.collection === c.name) return ["busy", "Indexing…"];
  if (s.embed_pending) return ["busy", `${s.embed_pending.toLocaleString()} to embed`];
  if (s.errors) return ["warn", plural(s.errors, "unreadable file")];
  if (s.heavy_pending) return ["", `${s.heavy_pending.toLocaleString()} for the night run`];
  return ["ok", "Up to date"];
}

function renderCollections() {
  const box = $("#collections");
  $("#nav-n-folders").textContent = state.collections.length || "";
  if (!state.collections.length) {
    box.replaceChildren(el("div", { class: "panel empty-panel" },
      el("p", {}, "No folders yet. Add one to start indexing — text becomes searchable within minutes."),
      el("button", { class: "btn primary", onclick: addFolder }, "Add folder…")));
    return;
  }
  box.querySelector(".empty-panel")?.remove();
  const cards = new Map($$(".coll", box).map((n) => [n.dataset.name, n]));
  const names = new Set(state.collections.map((c) => c.name));
  for (const [name, node] of cards) if (!names.has(name)) node.remove();
  if (!state.open.size && state.collections.length === 1 && !localGet("open")) state.open.add(state.collections[0].name);
  let prev = null;
  for (const c of state.collections) {
    const node = cards.get(c.name) || collectionCard(c);
    updateCollection(node, c);
    if (prev ? prev.nextElementSibling !== node : box.firstElementChild !== node) prev ? prev.after(node) : box.prepend(node);
    prev = node;
  }
}

/* the service's errors read "ValueError: 'type' is reserved …": the reason is enough here */
const cleanError = (m) => String(m || "").replace(/^(ValueError|KeyError|PermissionError): /, "");
const aliasList = (v) => String(v || "").split(/[\s,]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
const aliasChips = (x) => x.aliases?.length ? el("span", { class: "al-chips", title: `Short name${x.aliases.length > 1 ? "s" : ""}: ${x.aliases.map((a) => `${a}: …`).join(", ")}` }, x.aliases.map((a) => el("code", {}, a))) : null;

function fillForm(f, c) {
  f.description.value = c.description || "";
  f.aliases.value = (c.aliases || []).join(", ");
  f.in_global.checked = !!c.in_global;
  f.enabled.checked = c.enabled !== false;
  f.images.checked = !!c.media?.images; f.audio.checked = !!c.media?.audio; f.video.checked = !!c.media?.video;
  f.pdf_pages.value = c.media?.pdf_pages || "smart";
  f.exclude.value = (c.exclude || []).join("\n");
  f.metadata_only.value = (c.metadata_only || []).join("\n");
  f.contexts.value = Object.entries(c.contexts || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
}

function updateCollection(node, c) {
  node._c = c;
  const s = c.stats;
  $(".coll-name", node).replaceChildren(c.name, aliasChips(c), c.ask_cloud === false
    ? el("span", { class: "tag local-tag", title: "Ask answers questions about this folder on this Mac only" }, "local answers") : null);
  const cc = $(".coll-cloud", node), ccIn = $("input", cc);
  cc.hidden = !("ask_cloud" in c);                    // services without cloud models do not report it
  if (!ccIn.dataset.busy) ccIn.checked = c.ask_cloud !== false;
  for (const b of $$(".flag", node)) {               // the two switches, readable and usable while the card is closed
    const k = b.dataset.flag, on = k === "enabled" ? c.enabled !== false : !!c.in_global;
    b.setAttribute("aria-pressed", String(on));
    b.textContent = k === "enabled" ? (on ? "On" : "Paused") : (on ? "In All" : "Not in All");
    b.title = k === "enabled"
      ? (on ? "Indexed and searchable. Click to pause this folder." : "Paused: not indexed or searched. Click to turn it back on.")
      : (on ? "Searched by All. Click to leave it out of All (its own tab, groups and shortcuts still search it)."
        : "Left out of All: only its own tab, groups and shortcuts search it. Click to include it in All.");
  }
  $(".coll-path", node).replaceChildren(el("bdi", {}, c.path));
  $(".coll-path", node).title = c.path;
  $(".coll-quick", node).textContent = s ? `${plural(s.files, "file")} · ${size(s.bytes)}` : "";
  const [cls, label] = collState(c);
  const pill = $(".coll-state", node); pill.className = "pill coll-state " + cls; pill.textContent = label;
  $(".coll-stats", node).textContent = s
    ? `${plural(s.files, "file")} · ${size(s.bytes)} · ${plural(s.items, "piece")} · ${s.embed_pending.toLocaleString()} waiting to embed · ${s.heavy_pending} waiting for OCR/media · ${s.metadata_only} name-only · ${s.errors} unreadable · scanned ${ago(s.last_scan)} · night work ${ago(s.last_heavy)}`
    : c.exists ? "Not indexed yet." : `The folder ${c.path} is not there. Reconnect the drive, or remove it here.`;
  const hb = $('[data-act="health"]', node);
  hb.hidden = !s;
  if (s) hb.textContent = `${s.errors} unreadable · ${s.metadata_only} name-only — ${$(".health-body", node).hidden ? (s.errors ? "show problems" : "show details") : "hide"}`;
  const f = $(".coll-form", node);
  if (!f.dataset.dirty) fillForm(f, c);
  setOpen(node, state.open.has(c.name), true);
}

function setOpen(node, open, quiet) {
  node.classList.toggle("open", open);
  $(".coll-summary", node).setAttribute("aria-expanded", String(open));
  $(".coll-detail", node).hidden = !open;
  const name = node.dataset.name;
  if (open) state.open.add(name); else state.open.delete(name);
  if (!quiet) saveList("open", [...state.open]);
  if (open && !node._tested) { node._tested = true; node._runTests?.(); loadCardAgents(node); }
}

function collectionCard(c) {
  const node = $("#tpl-collection").content.firstElementChild.cloneNode(true);
  node.dataset.name = c.name;
  node._c = c;
  const f = $(".coll-form", node);
  $(".coll-summary", node).addEventListener("click", () => setOpen(node, !node.classList.contains("open")));
  f.addEventListener("input", () => { f.dataset.dirty = "1"; $(".saved", f).textContent = ""; });
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const lines = (t) => t.split("\n").map((x) => x.trim()).filter(Boolean);
    const contexts = {};
    for (const l of lines(f.contexts.value)) { const i = l.indexOf(":"); if (i > 0) contexts[l.slice(0, i).trim()] = l.slice(i + 1).trim(); }
    const out = $(".saved", f); out.classList.remove("err");
    try {
      await post(`/api/collections/${enc(c.name)}`, {
        description: f.description.value.trim(), in_global: f.in_global.checked, enabled: f.enabled.checked,
        ...(Array.isArray(node._c.aliases) ? { aliases: aliasList(f.aliases.value) } : {}),     // older services: no aliases
        exclude: lines(f.exclude.value), metadata_only: lines(f.metadata_only.value), contexts,
        media: { images: f.images.checked, audio: f.audio.checked, video: f.video.checked, pdf_pages: f.pdf_pages.value },
      });
      delete f.dataset.dirty;
      out.textContent = "Saved. Rule changes apply at the next sync.";
      loadCollections();
    } catch (err) { out.textContent = cleanError(err.message); out.classList.add("err"); }
  });
  $('[data-act="ask-cloud"]', node).addEventListener("change", async (e) => {
    const i = e.target, on = i.checked;
    i.disabled = true; i.dataset.busy = "1";
    try {
      await post(`/api/collections/${enc(c.name)}`, { ask_cloud: on });
      toast(on ? `Ask may use a cloud model for ${c.name}` : `Ask answers about ${c.name} on this Mac only`);
      await loadCollections();
    } catch (err) { i.checked = !on; toast(cleanError(err.message)); }
    i.disabled = false; delete i.dataset.busy;
  });
  for (const b of $$(".flag", node)) b.addEventListener("click", async () => {
    const k = b.dataset.flag, cur = node._c;
    const next = !(k === "enabled" ? cur.enabled !== false : !!cur.in_global);
    b.disabled = true;
    try {
      await post(`/api/collections/${enc(c.name)}`, { [k]: next });
      f[k].checked = next;                            // an open, edited form must not save the old value later
      toast(k === "enabled" ? `${c.name} ${next ? "is on again" : "is paused"}` : `${c.name} ${next ? "is searched by All" : "is left out of All"}`);
      await loadCollections();
    } catch (err) { toast(cleanError(err.message)); }
    b.disabled = false;
  });
  // rule tester: how many indexed files each rule list matches, as you type
  const tests = [];
  for (const box of node.querySelectorAll(".rule-test")) {
    const ta = f[box.dataset.for];
    let t = null;
    const test = async () => {
      const patterns = ta.value.split("\n").map((x) => x.trim()).filter(Boolean);
      if (!patterns.length || !node._c.stats) { box.textContent = ""; return; }
      try {
        const r = await post("/api/test-rule", { collection: c.name, patterns });
        const what = box.dataset.for === "exclude" ? "would be skipped" : "are name-only";
        box.classList.toggle("warn", box.dataset.for === "exclude" && r.matches > 0);
        box.textContent = r.matches
          ? `${r.matches.toLocaleString()} of ${r.of.toLocaleString()} indexed files ${what}: ${r.sample.slice(0, 3).join(", ")}${r.matches > 3 ? " …" : ""}`
          : `No indexed files match${box.dataset.for === "exclude" ? " (already skipped files are not listed)" : ""}.`;
      } catch (e) { box.textContent = e.message; }
    };
    ta.addEventListener("input", () => { clearTimeout(t); t = setTimeout(test, 400); });
    tests.push(test);
  }
  node._runTests = () => tests.forEach((t) => t());
  // health: unreadable files, failed media, name-only files — with retry
  const hb = node.querySelector('[data-act="health"]'), hbody = node.querySelector(".health-body");
  hb.addEventListener("click", async () => {
    if (!hbody.hidden) { hbody.hidden = true; hb.setAttribute("aria-expanded", "false"); updateCollection(node, node._c); return; }
    try {
      const h = await api(`/api/health?c=${enc(c.name)}`);
      const list = (items, fmt) => el("ul", {}, items.slice(0, 50).map((x) => el("li", {}, ...fmt(x))));
      hbody.replaceChildren(
        el("div", {}, el("b", {}, `Unreadable (${h.error_count})`), h.errors.length ? list(h.errors, (x) => [x.path, el("span", {}, ` — ${x.error}`)]) : " none"),
        el("div", {}, el("b", {}, `Media that could not be embedded (${h.failed_media.length})`), h.failed_media.length ? list(h.failed_media, (x) => [x]) : " none"),
        el("div", {}, el("b", {}, `Name-only by your rules (${h.name_only_count})`), h.name_only.length ? list(h.name_only, (x) => [x]) : " none"),
        (h.error_count || h.failed_media.length) ? el("div", {}, el("button", { class: "btn small", onclick: async () => {
          try { const r = await post("/api/retry", { collection: c.name }); toast(`Retrying ${r.retrying} files`); poll(true); } catch (e) { toast(e.message); }
        } }, "Retry failed files")) : null);
      hbody.hidden = false; hb.setAttribute("aria-expanded", "true");
      updateCollection(node, node._c);
    } catch (e) { toast(e.message); }
  });
  node.querySelector('[data-act="sync"]').addEventListener("click", async () => {
    try { await post("/api/sync", { collection: c.name, kind: "sync" }); toast(`Syncing ${c.name} now`); poll(true); } catch (e) { toast(e.message); }
  });
  node.querySelector('[data-act="remove"]').addEventListener("click", async () => {
    const agentsOk = await agentsAvailable();
    const f = await dialog({ title: `Remove ${c.name} from MySearch?`, confirm: "Remove", danger: true,
      text: "Its search index is moved aside. The folder and your files are not touched.",
      content: agentsOk ? el("div", { class: "dlg-fields" },
        el("label", { class: "check-line" }, el("input", { type: "checkbox", name: "disconnect", checked: true }), "Also disconnect AI agents"),
        el("p", { class: "dlg-note" }, "Takes the MySearch entry out of this folder's agent settings files. The rest of those files stays as it is.")) : null });
    if (!f) return;
    try {
      const r = await post(`/api/collections/${enc(c.name)}/remove`, agentsOk ? { disconnect_agents: !!f.disconnect?.checked } : {});
      toast(`Removed ${c.name}${r.agents?.files ? `; agents disconnected (${agentActions(r.agents.files)})` : ""}`);
      state.open.delete(c.name); loadCollections();
    } catch (e) { toast(e.message); }
  });
  for (const [act, install] of [["agents-connect", true], ["agents-disconnect", false]]) {
    node.querySelector(`[data-act="${act}"]`).addEventListener("click", () => setCardAgents(node, install));
  }
  return node;
}

async function addFolder() {
  let path;
  try { ({ path } = await post("/api/pick-folder")); } catch (e) { toast(e.message); return; }
  if (path) addFolderDialog(path);
}
async function addFolderDialog(path) {
  const agentsOk = await agentsAvailable();
  const name = el("input", { name: "name", type: "text", value: path.split("/").pop(), spellcheck: "false", autocomplete: "off" });
  const f = await dialog({ title: "Add a folder", confirm: "Add folder", content: el("div", { class: "dlg-fields" },
    el("p", { class: "dlg-path mono" }, path),
    el("label", {}, el("span", {}, "Name ", el("small", {}, "shown as a tab")), name),
    agentsOk ? el("label", { class: "check-line" }, el("input", { type: "checkbox", name: "connect", checked: true }), "Connect AI agents in this folder") : null,
    agentsOk ? el("p", { class: "dlg-note" }, "Writes a MySearch entry into this folder's settings files for Claude Code, Cursor, Codex, Grok and OpenCode, so agents working there search it. Nothing else in those files changes, no other settings are touched, and Disconnect takes the entry out again.") : null) });
  if (!f) return;
  try {
    const r = await post("/api/collections", { path, name: name.value.trim() || undefined, ...(agentsOk ? { connect_agents: !!f.connect?.checked } : {}) });
    const agents = r.agents?.files ? ` Agents connected (${agentActions(r.agents.files)}).` : r.agents_error ? ` The agents could not be connected: ${r.agents_error}` : "";
    toast(`Added. Text is searchable within minutes; OCR and media run tonight or on Sync now.${agents}`);
    if (r.collection?.name) { state.open.add(r.collection.name); saveList("open", [...state.open]); }
    loadCollections(); poll(true);
  } catch (e) { toast(e.message); }
}

/* a small form in a dialog; resolves to the form when confirmed, null when cancelled */
function dialog({ title, text, content, confirm = "OK", danger = false }) {
  return new Promise((resolve) => {
    closeMenu();
    const back = document.activeElement;
    const ok = el("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "submit" }, confirm);
    const form = el("form", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title },
      el("header", {}, el("h2", {}, title)),
      text ? el("p", { class: "dlg-text" }, text) : null,
      content || null,
      el("div", { class: "dlg-actions" }, el("button", { class: "btn ghost", type: "button", onclick: () => done(false) }, "Cancel"), ok));
    const ov = el("div", { class: "overlay dlg" }, form);
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) done(false); });
    function done(yes) { ov.remove(); reportLayout(); back?.focus?.({ preventScroll: true }); resolve(yes ? form : null); }
    form.addEventListener("submit", (e) => { e.preventDefault(); done(true); });
    form.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); done(false); }
      e.stopPropagation();
    });
    document.body.append(ov);
    (form.querySelector("input[type=text]") || ok).focus();
    reportLayout();
  });
}

/* ------------------------------------------------------------------ agents per folder and everywhere */

let agentsApi = null;           // null: not asked yet
async function agentsAvailable() {
  if (agentsApi !== null) return agentsApi;
  try { await api("/api/agents/global"); agentsApi = true; } catch (e) { agentsApi = missingEndpoint(e) ? false : null; }
  return !!agentsApi;
}
const AGENT_STATE = {
  connected: ["ok", ""], partial: ["warn", "partly"], missing: ["", "not set up"], other: ["warn", "set up differently"], unreadable: ["danger", "can't read"],
};
function agentSummary(rows) {
  const by = new Map();
  for (const r of rows) {
    const a = r.agent.replace(/\s*\(approval\)$/i, "");       // Claude Code's approval file belongs to Claude Code
    if (!by.has(a)) by.set(a, []);
    by.get(a).push(r);
  }
  return [...by].map(([agent, rs]) => ({
    agent, rows: rs,
    state: rs.some((r) => r.state === "unreadable") ? "unreadable" : rs.some((r) => r.state === "other") ? "other"
      : rs.every((r) => r.state === "connected") ? "connected" : rs.some((r) => r.state === "connected") ? "partial" : "missing",
  }));
}
function agentPills(rows) {
  return agentSummary(rows).map(({ agent, state, rows: rs }) => {
    const [cls, label] = AGENT_STATE[state] || ["", state];
    return el("span", { class: `pill agent ${cls}`, tabindex: "0",
      title: rs.map((r) => `${r.file}: ${r.state}${r.note ? ` (${r.note})` : ""}`).join("\n") },
    el("i", { class: "dot", "aria-hidden": "true" }), agent, label ? el("small", {}, label) : null);
  });
}
function agentNotes(rows) {
  const notes = [...new Set(rows.map((r) => r.note).filter(Boolean).flatMap((n) => n.split("; ")))].map((n) => n[0].toUpperCase() + n.slice(1));
  if (rows.some((r) => /codex/i.test(r.agent)) && !notes.some((n) => /trust/i.test(n))) notes.push("Codex reads a folder's own settings only in projects you have marked as trusted in Codex.");
  return notes;
}
function agentActions(files) {
  const n = {};
  for (const f of files) n[f.action] = (n[f.action] || 0) + 1;
  const words = { added: "added", updated: "updated", present: "already there", skipped: "skipped", removed: "removed", absent: "not there", kept: "kept" };
  return Object.entries(n).map(([k, v]) => `${v} ${words[k] || k}`).join(", ") || "nothing to change";
}

async function loadCardAgents(node) {
  const box = $(".coll-agents", node), name = node.dataset.name;
  let rows;
  try { rows = (await api(`/api/collections/${enc(name)}/agents`)).agents || []; }
  catch (e) { box.hidden = true; return; }
  box.hidden = false;
  $(".ca-pills", box).replaceChildren(...agentPills(rows));
  const connected = rows.filter((r) => r.state === "connected").length;
  $('[data-act="agents-connect"]', box).disabled = connected === rows.length && rows.length > 0;
  $('[data-act="agents-connect"]', box).textContent = connected ? "Connect the rest" : "Connect";
  $('[data-act="agents-disconnect"]', box).hidden = !connected;
  $(".ca-note", box).replaceChildren(...agentNotes(rows).map((n) => el("span", {}, n)));
}
async function setCardAgents(node, install) {
  const name = node.dataset.name;
  try {
    const r = await post(`/api/collections/${enc(name)}/agents`, { install });
    toast(`${install ? "Connected" : "Disconnected"} ${name}: ${agentActions(r.files || [])}.`);
  } catch (e) { toast(e.message); }
  loadCardAgents(node);
}

/* Settings › Agents: the apps' own settings, for every project */
async function renderGlobalAgents() {
  const box = $("#agents-global");
  let rows;
  try { rows = (await api("/api/agents/global")).agents || []; agentsApi = true; }
  catch (e) { box.replaceChildren(); if (missingEndpoint(e)) agentsApi = false; return; }
  const connected = rows.filter((r) => r.state === "connected").length;
  box.replaceChildren(el("article", { class: "agent" },
    el("h3", {}, "Your AI apps, everywhere"),
    el("p", {}, "Adds MySearch to the apps' own settings, for every project: Claude Desktop, Cursor and the Grok Bot app, Grok, Codex and Antigravity. An agent then searches the folder it is working in. Only the MySearch entry is written; an app whose folder does not exist is skipped."),
    rows.length ? el("div", { class: "ca-pills" }, agentPills(rows)) : el("p", { class: "muted" }, "None of these apps is installed."),
    el("div", { class: "ca-actions" },
      el("button", { class: "btn small primary", disabled: rows.length > 0 && connected === rows.length, onclick: async () => {
        try { const r = await post("/api/agents/global", { install: true }); toast(`Connected: ${agentActions(r.files || [])}.`); } catch (e) { toast(e.message); }
        renderGlobalAgents();
      } }, connected ? "Connect the rest" : "Connect"),
      connected ? el("button", { class: "btn small ghost", onclick: async () => {
        try { const r = await post("/api/agents/global", { install: false }); toast(`Disconnected: ${agentActions(r.files || [])}.`); } catch (e) { toast(e.message); }
        renderGlobalAgents();
      } }, "Disconnect") : null),
    agentNotes(rows).length ? el("p", { class: "ca-note" }, agentNotes(rows).map((n) => el("span", {}, n))) : null));
}

/* ------------------------------------------------------------------ settings: groups */

let groupEditing = null;        // the group name being edited, or "" for a new one
function renderGroups() {
  const box = $("#groups");
  const on = !!state.groupsApi;
  $(".groups-head").hidden = !on; $(".sub-lede").hidden = !on; box.hidden = !on;
  if (!on) return;
  const rows = (state.groups || []).map((g) => groupEditing === g.name ? groupEditor(g) : el("div", { class: "group-row" + (g.hidden ? " is-hidden" : "") },
    el("span", { class: "glw", html: GROUP_GLYPH }),
    el("div", { class: "g-main" }, el("b", {}, g.label, aliasChips(g)),
      el("span", { class: "g-members" }, g.collections.map((c) => el("code", {}, c)))),
    el("span", { class: "n mono" }, `${(g.files || 0).toLocaleString()} files`),
    el("span", { class: "g-actions" },
      "hidden" in g ? el("label", { class: "g-tab", title: "Show this group as a tab next to All. Off: it stays in the Folders ▾ menu and still works by its name or short name." },
        el("input", { type: "checkbox", class: "switch", role: "switch", checked: !g.hidden, "aria-label": `Show ${g.label} as a tab`,
          onchange: (e) => setGroupTab(g, e.target.checked, e.target) }), "Tab") : null,
      el("button", { class: "btn small", onclick: () => { groupEditing = g.name; renderGroups(); } }, "Edit"),
      el("button", { class: "btn small danger-ghost", onclick: () => deleteGroup(g) }, "Delete…"))));
  if (groupEditing === "") rows.unshift(groupEditor(null));
  box.replaceChildren(...(rows.length ? rows : [el("p", { class: "empty-note" }, "No groups yet. Make one here, or ⌘-click two folder tabs and choose Save as group.")]));
  $(".group-editor input[type=text]", box)?.focus();
}
async function setGroupTab(g, on, input) {
  input.disabled = true;
  try {
    await post("/api/groups", { name: g.name, label: g.label, collections: g.collections, hidden: !on });
    await loadCollections(); renderGroups();
    toast(on ? `${g.label} has its own tab again` : `${g.label} is no longer a tab. Find it under Folders ▾, or type ${(g.aliases || [])[0] || g.name}: …`);
  } catch (e) { input.checked = !on; input.disabled = false; toast(cleanError(e.message)); }
}

function groupEditor(g) {
  const label = el("input", { type: "text", name: "label", value: g?.label || "", placeholder: "e.g. Work", spellcheck: "false", autocomplete: "off" });
  const aliases = el("input", { type: "text", name: "aliases", value: (g?.aliases || []).join(", "), placeholder: "e.g. w", spellcheck: "false", autocomplete: "off" });
  const tabOn = el("input", { type: "checkbox", class: "switch", role: "switch", name: "tab", checked: !g?.hidden });
  const fresh = !g || "hidden" in g;                 // a service that knows aliases and hidden groups
  const picks = state.collections.map((c) => el("label", { class: "check-line" },
    el("input", { type: "checkbox", name: "member", value: c.name, checked: !!g?.collections.includes(c.name) }), c.name,
    el("small", {}, ` ${(c.stats?.files || 0).toLocaleString()} files`)));
  const out = el("span", { class: "saved", role: "status" });
  const form = el("form", { class: "group-editor panel" },
    el("div", { class: "ge-row" },
      el("label", {}, el("span", {}, g ? "Name" : "New group"), label),
      fresh ? el("label", {}, el("span", {}, "Short names ", el("small", {}, "type ", el("code", {}, "w: invoice"), " to search the group")), aliases) : null,
      fresh ? el("label", { class: "check-line" }, tabOn, "Show as a tab") : null),
    el("fieldset", { class: "media" }, el("legend", {}, "Folders in this group"), picks),
    el("div", { class: "form-actions" },
      el("button", { class: "btn primary", type: "submit" }, g ? "Save" : "Create group"),
      el("button", { class: "btn ghost", type: "button", onclick: () => { groupEditing = null; renderGroups(); } }, "Cancel"), out));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const members = $$("input[name=member]:checked", form).map((x) => x.value);
    out.classList.add("err");
    if (!label.value.trim()) { out.textContent = "Give the group a name."; return; }
    if (!members.length) { out.textContent = "Tick at least one folder."; return; }
    try {
      await post("/api/groups", { label: label.value.trim(), collections: members, ...(g ? { name: g.name } : {}),
        ...(fresh ? { aliases: aliasList(aliases.value), hidden: !tabOn.checked } : {}) });
      groupEditing = null;
      await loadCollections(); renderGroups();
      toast(g ? `Saved ${label.value.trim()}` : `Created ${label.value.trim()}. It has its own tab now.`);
    } catch (err) { out.textContent = missingEndpoint(err) ? "Groups need a newer search service." : cleanError(err.message); }
  });
  return form;
}
async function deleteGroup(g) {
  const ok = await dialog({ title: `Delete the group ${g.label}?`, text: "Only the group goes. Its folders, their indexes and your files are not touched.", confirm: "Delete group", danger: true });
  if (!ok) return;
  try {
    await post(`/api/groups/${enc(g.name)}/remove`, {});
    if (state.scope.split(",").includes(g.name)) { state.scope = "all"; localSet("scope", "all"); }
    await loadCollections(); renderGroups();
    toast(`Deleted the group ${g.label}`);
  } catch (e) { toast(e.message); }
}
$("#btn-new-group").addEventListener("click", () => { groupEditing = ""; renderGroups(); });

/* ------------------------------------------------------------------ settings: favourites */

/* Re-ranker learning from favourites (finetune.py): one line under the introduction, e.g. "Learning: 42 / 300
   favourites until the first fine-tune · base model". Left out when the service predates it or does not answer. */
async function renderLearning() {
  let line = $("#fav-learning");
  if (!line) { line = el("p", { id: "fav-learning", class: "lede" }); $("#fav-list").before(line); }
  let text = "";
  try { text = (await api("/api/finetune")).line || ""; } catch { text = ""; }
  line.textContent = text;
  line.hidden = !text;
}

async function renderFavourites() {
  renderLearning();
  const box = $("#fav-list");
  if (!box.childElementCount) box.replaceChildren(el("p", { class: "lede" }, "Loading…"));
  const ok = await probeFeedback();
  if (ok === false) {
    box.replaceChildren(el("div", { class: "panel prose" },
      el("p", {}, "Favourites need a newer version of the search service than the one running now."),
      el("p", {}, "After updating, restart it with ", el("code", {}, "mse service restart"), ". Search keeps working in the meantime.")));
    return;
  }
  if (ok !== true) { box.replaceChildren(el("div", { class: "panel prose" }, el("p", {}, "The search service is not answering. Favourites appear here when it is back."))); return; }
  const needle = fold($("#fav-filter").value.trim());
  const items = fb.items.filter((x) => !needle || fold(`${x.query} ${x.path} ${x.collection}`).includes(needle));
  if (!fb.items.length) {
    box.replaceChildren(el("div", { class: "panel prose" },
      el("p", {}, "No favourites yet."),
      el("p", {}, "When a search finds the file you wanted, click its ", el("b", {}, "☆"), " (or press ", el("kbd", {}, "⌘"), el("kbd", {}, "D"),
        ") to make it the best result for that search. Right-click a result, or use ", el("b", {}, "Not relevant"), " in the preview, to keep a file out of a search.")));
    return;
  }
  if (!items.length) { box.replaceChildren(el("p", { class: "lede" }, "Nothing matches the filter.")); return; }
  const groups = new Map();
  for (const x of items) {
    const k = fold(x.query).trim();
    if (!groups.has(k)) groups.set(k, { query: x.query, items: [], latest: 0 });
    const g = groups.get(k);
    g.items.push(x);
    g.latest = Math.max(g.latest, Date.parse(String(x.last_used || x.created || "").replace(" ", "T")) || 0);
  }
  const sorted = [...groups.values()].sort((a, b) => b.latest - a.latest);
  box.replaceChildren(...sorted.map((g) => el("section", { class: "fav-group" },
    el("h3", {}, el("span", { class: "q", title: g.query }, `“${g.query}”`),
      el("button", { class: "linklike", onclick: () => { $("#q").value = g.query; state.imageQuery = null; showSearch(); runSearch(); } }, "Search again")),
    el("div", { class: "fav-rows" }, g.items.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "best" ? -1 : 1)).map((x) => favRow(x))))));
}

function favRow(x) {
  const best = x.kind === "best";
  const name = String(x.path).split("/").pop();
  const meta = [x.uses ? `used ${x.uses}×` : null, x.last_used ? `last ${ago(x.last_used)}` : x.created ? `added ${ago(x.created)}` : null].filter(Boolean).join(" · ");
  const row = el("div", { class: "fav-row" },
    el("span", { class: "fav-kind " + (best ? "best" : "hide"), title: best ? "Best result for this search" : "Not relevant for this search", html: best ? ICON.star : ICON.hide }),
    el("div", { class: "fav-what" },
      el("div", { class: "fav-name", title: name }, name, best ? null : el("span", { class: "tag", style: "margin-left:8px" }, "hidden")),
      el("div", { class: "fav-path", title: `${x.collection}/${x.path}` }, el("bdi", {}, `${x.collection} · ${x.path}`))),
    el("span", { class: "fav-meta" }, meta),
    el("button", { class: "icon-btn small", title: "Remove", "aria-label": `Remove ${name} from “${x.query}”`, html: ICON.close, onclick: async () => {
      try {
        await post("/api/feedback/remove", x.id != null ? { id: x.id } : { query: x.query, collection: x.collection, path: x.path });
        fb.items = fb.items.filter((y) => y !== x);
        for (const r of state.results) if (r.path === x.path && r.collection === x.collection && fold(state.query) === fold(x.query)) { r.favorite = null; refreshCard(r); }
        refreshPreviewFav();
        toast(best ? "Removed from favourites" : "This file can show up in that search again");
        renderFavourites();
      } catch (e) { feedbackFailed(e); }
    } }));
  return row;
}
$("#fav-filter").addEventListener("input", () => renderFavourites());

/* ------------------------------------------------------------------ settings: agents, privacy, about */

function snippet(text, shown) {
  return el("div", { class: "snippet" }, el("pre", {}, shown ?? text),
    el("button", { class: "btn small", onclick: () => copy(text, "Copied") }, "Copy"));
}

function renderAgents() {
  const bin = state.status?.mse_bin || state.settings?.mse_bin || MSE_BIN_FALLBACK;
  const port = location.port || String(state.settings?.port || 8737);
  const mask = TOKEN.slice(0, 4) + "•".repeat(12) + TOKEN.slice(-4);
  const http = (tok) => `POST http://127.0.0.1:${port}/mcp\nX-MSE-Token: ${tok}`;
  const curl = (tok) => `curl -s http://127.0.0.1:${port}/mcp -H "X-MSE-Token: ${tok}" -H "Content-Type: application/json" \\\n  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`;
  const variants = [
    ["JSON", `"mysearch": { "command": "${bin}", "args": ["mcp"] }`,
      "Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json · Cursor: ~/.cursor/mcp.json — inside \"mcpServers\"."],
    ["Codex", `[mcp_servers.mysearch]\ncommand = "${bin}"\nargs = ["mcp"]`, "Codex: ~/.codex/config.toml"],
  ];
  let which = Number(localGet("agentTab") || 0) || 0;
  const varBox = el("div");
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Configuration format" });
  const showVar = () => {
    seg.replaceChildren(...variants.map(([label], i) => el("button", { "aria-pressed": String(i === which), onclick: () => { which = i; localSet("agentTab", i); showVar(); } }, label)));
    varBox.replaceChildren(snippet(variants[which][1]), el("p", { class: "where" }, variants[which][2]));
  };
  showVar();
  let revealed = false;
  const tokCode = el("code", { class: "mono" }, mask);
  const httpBox = el("div");
  const showHttp = () => { tokCode.textContent = revealed ? TOKEN : mask; httpBox.replaceChildren(snippet(http(TOKEN), http(revealed ? TOKEN : mask)), el("div", { style: "height:8px" }), snippet(curl(TOKEN), curl(revealed ? TOKEN : mask))); };
  showHttp();
  $("#agents").replaceChildren(
    el("article", { class: "agent" }, el("h3", {}, "Claude Code"),
      el("p", {}, "Registers MySearch for every project:"), snippet(`claude mcp add --scope user mysearch -- ${bin} mcp`)),
    el("article", { class: "agent" }, el("h3", {}, "Claude Desktop, Cursor, Codex and other MCP clients"),
      el("p", {}, "Add this server to the client's MCP configuration. It runs over stdio, so no token is needed."), seg, varBox),
    el("article", { class: "agent" }, el("h3", {}, "HTTP clients"),
      el("p", {}, "Send MCP requests to the running service, with the token in a header."), httpBox,
      el("div", { class: "token" }, el("span", { class: "label" }, "Token"), tokCode,
        el("button", { class: "btn small", onclick: (e) => { revealed = !revealed; e.target.textContent = revealed ? "Hide" : "Show"; showHttp(); } }, "Show"),
        el("button", { class: "btn small", onclick: () => copy(TOKEN, "Token copied") }, "Copy token"))),
    el("article", { class: "agent" }, el("h3", {}, "Shell-only agents"),
      el("p", {}, "Anything that can run a command can use the CLI. Piped output is masked like MCP."),
      snippet(`mse search --json "…"\nmse get '#docid'`)));
}

function renderPrivacy() {
  renderPrivacyLede();
  const list = $("#name-only");
  const rows = state.collections.map((c) => el("li", {},
    el("span", { class: "who" }, c.name),
    (c.metadata_only || []).length ? (c.metadata_only || []).map((p) => el("code", {}, p)) : el("span", { class: "muted", style: "color:var(--ink-3)" }, "no name-only rules"),
    el("button", { class: "linklike", onclick: () => { state.open.add(c.name); saveList("open", [...state.open]); goSection("folders"); renderCollections();
      const n = $$(".coll").find((x) => x.dataset.name === c.name); n?.scrollIntoView({ block: "start" }); $("textarea[name=metadata_only]", n)?.focus(); } }, "Edit")));
  list.replaceChildren(...(rows.length ? rows : [el("li", {}, "No folders yet.")]));
  $("#secret-patterns").replaceChildren(...(state.settings?.secret_patterns || SECRET_PATTERNS).map((p) => el("span", {}, p)));
}

function renderAbout() {
  const st = state.status, s = state.settings;
  const base = (p) => (p ? String(p).split("/").pop() : "—");
  const files = state.collections.reduce((n, c) => n + (c.stats?.files || 0), 0);
  const uiDate = UI_VERSION && /^\d+$/.test(UI_VERSION) ? new Date(Number(UI_VERSION) * 1000).toLocaleString() : UI_VERSION;
  const facts = [
    ["Version", `${st?.version || APP_VERSION || "—"} · interface of ${uiDate || "—"}`],
    ["Service", st ? `Running for ${Math.round(st.uptime_s / 60)} min · ${st.daemon_rss_mb} MB · port ${st.port} · pid ${st.pid}` : "Not running"],
    ["Indexed", `${plural(state.collections.length, "folder")} · ${plural(files, "file")}`],
    ["Search model", `${base(s?.model?.path)}${st && !st.model_present ? " (missing)" : ""}`],
    ["Re-ranker", base(s?.model?.reranker)],
    ["Speech to text", `${base(s?.model?.asr?.path)}${st && !st.asr_present ? " (not installed)" : ""}`],
    ["Data", st?.data_dir || "~/Library/Application Support/MySearch"],
    ["Logs", "~/Library/Logs/MySearch"],
  ];
  $("#about").replaceChildren(...facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, k === "Data" || k === "Logs" || /model|ranker|Speech/.test(k) ? el("span", { class: "mono" }, v) : v)]));
}
$("#btn-about-keys").addEventListener("click", showHelp);

/* ------------------------------------------------------------------ Ask: a short answer from the top results
   POST /api/ask streams NDJSON: sources, then tokens (with [n] markers), then done or error. Hidden unless the
   service reports `status.ask` and it is available and enabled. */

const ask = { q: "", text: "", sources: [], done: null, error: null, ctrl: null, stopped: false, raf: 0 };
const QUESTION = /\?\s*$|^(what|how|when|where|who|whom|whose|which|why|is|are|was|were|do|does|did|can|could|should|would|will|has|have|had|wie|was|wann|wo|woher|wohin|wer|wem|wen|wessen|welche[rsnm]?|warum|weshalb|wieso|ist|sind|war|hat|haben|gibt|kann|muss|soll|darf)\b/i;
const askReady = () => !!(state.status?.ask?.available && state.status.ask.enabled);
const isQuestion = (q) => QUESTION.test(q.trim()) && q.trim().split(/\s+/).length >= 3;
const tsToSec = (ts) => { if (!ts) return null; const p = String(ts).split(":").map(Number); return p.reduce((a, b) => a * 60 + b, 0); };

function askBody(q, scope) {
  const b = { q };
  if (scope && scope !== "all") b.collections = scope.split(",");
  const f = MINI ? {} : searchFilters();
  if (Object.keys(f).length) b.filters = f;
  return b;
}

/* calls on(message) for each streamed line; throws on HTTP errors (409 ask_disabled, 404 before the backend exists) */
async function askStream(body, on, signal) {
  const r = await fetch("/api/ask", { method: "POST", signal, headers: { "X-MSE-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    const e = new Error(j.error === "ask_disabled" ? "Ask is turned off." : j.error || `${r.status} ${r.statusText}`);
    e.status = r.status; e.code = j.error; throw e;
  }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  const flush = (line) => { line = line.trim(); if (!line) return; try { on(JSON.parse(line)); } catch { /* partial or bad line */ } };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) { flush(buf.slice(0, i)); buf = buf.slice(i + 1); }
  }
  flush(buf + dec.decode());
}

/* answer text → HTML: escaped, **bold**, [n] as citation chips for the sources that exist */
function answerHtml(text, sources) {
  return inlineMarkdown(esc(text)).replace(/\[(\d{1,2})\]/g, (m, n) => {
    const s = sources.find((x) => String(x.n) === n);
    return s ? `<button class="cite" data-n="${n}" title="${esc(s.title || s.path)}${s.page ? `, page ${s.page}` : ""}">${n}</button>` : m;
  });
}
function answerFooter(a) {
  const n = a.sources.length;
  const bits = [`Answer from ${n} document${n === 1 ? "" : "s"}`];
  if (a.done?.model && !a.done.where) bits.push(a.done.model);
  if (a.done?.fallback_reason) bits.push(fallbackText(a.done.fallback_reason));
  if (a.done?.training) bits.push(`⚠ ${TRAIN_TEXT}`);
  if (a.done?.timings?.total_ms) bits.push(`${(a.done.timings.total_ms / 1000).toFixed(1)} s`);
  return bits.join(" · ");
}
function answerCopy(a) {
  const src = a.sources.map((s) => `[${s.n}] ${s.title || s.path.split("/").pop()} — ${s.collection}/${s.path}${s.page ? `, p. ${s.page}` : ""}${s.ts ? ` at ${s.ts}` : ""}`);
  return `${a.text.trim()}\n\nSources:\n${src.join("\n")}`;
}

function startAsk(q = $("#q").value.trim()) {
  if (!q || !askReady()) return;
  stopAsk(true);
  Object.assign(ask, { q, text: "", sources: [], done: null, error: null, stopped: false, ctrl: new AbortController() });
  renderAsk();
  const ctrl = ask.ctrl;
  askStream(askBody(q, state.scope), (m) => {
    if (ask.ctrl !== ctrl) return;
    if (m.type === "sources") ask.sources = m.sources || [];
    else if (m.type === "token") ask.text += m.text || "";
    else if (m.type === "done") ask.done = m;
    else if (m.type === "error") ask.error = m.message || "The answer failed.";
    scheduleAskRender();
  }, ctrl.signal).catch((e) => {
    if (e.name === "AbortError" || ask.ctrl !== ctrl) return;
    ask.error = e.code === "ask_disabled" ? "Ask is turned off. Turn it on in Settings › Ask." : e.message;
    if (e.code === "ask_disabled" && state.status?.ask) state.status.ask.enabled = false;
  }).finally(() => { if (ask.ctrl === ctrl) { ask.ctrl = null; scheduleAskRender(); } });
}
function stopAsk(quiet) {
  if (!ask.ctrl) return;
  ask.ctrl.abort(); ask.ctrl = null; ask.stopped = !quiet;
  if (!quiet) scheduleAskRender();
}
function clearAsk() {
  stopAsk(true);
  Object.assign(ask, { q: "", text: "", sources: [], done: null, error: null, stopped: false });
  if (!MINI) renderAsk();
}
/* rendering waits for a frame, or 80 ms when the window gets none (WebKit gives no frames to a hidden window) */
function soon(fn) {
  let done = false;
  const run = () => { if (!done) { done = true; fn(); } };
  requestAnimationFrame(run); setTimeout(run, 80);
}
function scheduleAskRender() {
  if (ask.raf) return;
  ask.raf = 1;
  soon(() => { ask.raf = 0; MINI ? miniRenderAsk() : renderAsk(); });
}

/* Search view: when the query reads like a question, a quiet bar offers to ask it in the Ask view */
function renderAsk() {
  const box = $("#ask");
  if (!box) return;
  const q = $("#q").value.trim();
  if (!q || state.imageQuery || !isQuestion(splitShortcut(q).rest) || !state.status?.ask?.available) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren(el("div", { class: "ask-bar" },
    el("button", { class: "btn small ask-go", onclick: () => askInChat(q) }, el("span", { class: "ask-glyph", "aria-hidden": "true" }, "?"), "Ask about this"),
    el("span", {}, askReady() ? askBarText(q) : "Ask is off: this opens Ask, where you can turn it on."),
    el("kbd", { title: "Option-Return" }, "⌥↵")));
}

function askBarText(q) {
  const sc = splitShortcut(q), pred = predictWhere(sc.tokens ? sc.tokens.join(",") : state.scope, null);
  if (pred?.where === "cloud") return `A short answer with references, written from your top results by ${pred.model} (cloud).`;
  return "A short answer with references, written from your top results on this Mac.";
}

/* a cited source in the preview, at the page or time it came from */
async function openCited(sources, n) {
  const s = (sources || []).find((x) => String(x.n) === String(n));
  if (!s) return;
  const hit = { page: s.page || null, t0: tsToSec(s.ts), t1: null, modality: "text", why: ["cited"], similarity: null,
    snippet: s.snippet ? esc(s.snippet) : null, heading: null };
  const i = state.mode === "search" ? state.results.findIndex((r) => r.collection === s.collection && r.path === s.path) : -1;
  let r;
  if (i >= 0) { r = state.results[i]; state.sel = i; markSelected(i); $$(".r")[i]?.scrollIntoView({ block: "nearest" }); }
  else {
    try {
      const d = await api(`/api/doc?c=${enc(s.collection)}&ref=${enc(s.path)}&max=1`);
      r = { collection: d.collection, path: d.path, abs_path: d.abs_path, name: d.name, ext: d.ext, kind: d.kind, title: d.title,
        docid: d.docid, date: d.doc_date, size: d.size, pages: d.pages, duration: d.duration, lang: d.lang,
        metadata_only: d.metadata_only, context: d.context, why: [], hits: [], copies: [], thumb: false };
    } catch (e) { toast(e.message); return; }
    markSelected(-1);
  }
  renderPreview({ ...r, hits: [hit, ...r.hits.filter((h) => h.page !== hit.page || h.t0 !== hit.t0)] });
  if (NARROW.matches) $("#panes").classList.add("show-preview");
}

/* ------------------------------------------------------------------ Search | Ask */

function setMode(m, focus = true, stay = false) {
  state.mode = m === "ask" && !MINI ? "ask" : "search";
  localSet("mode", state.mode);
  if (!stay && !$("#view-settings").hidden) { $("#view-settings").hidden = true; $("#view-search").hidden = false; $("#btn-settings").setAttribute("aria-pressed", "false"); }
  const asking = state.mode === "ask";
  $("#view-search").classList.toggle("asking", asking);
  $("#panes").classList.toggle("asking", asking);
  $(".query-bar").hidden = asking; $(".results").hidden = asking; $("#chat").hidden = !asking;
  for (const b of $$("#mode-switch button")) b.setAttribute("aria-selected", String(b.dataset.mode === state.mode));
  if (asking) {
    renderChat();
    if (!$("#preview .pv-head")) $("#preview").replaceChildren(previewEmpty("Click a numbered reference in an answer to see that passage here."));
    if (focus) $("#ask-q").focus();
  } else {
    if (!$("#preview .pv-head") && !state.results.length) $("#preview").replaceChildren(previewEmpty("Select a result to preview it here."));
    if (focus) $("#q").focus();
  }
  reportLayout();
}

/* ------------------------------------------------------------------ Ask: a chat about your documents
   Each question streams from POST /api/ask (NDJSON: sources, tokens, done or error; 409 ask_disabled) with the
   chat's last three turns as `history`, so follow-ups work. The tabs, Folders ▾ and a shortcut in the question
   ("w: …") choose where it reads. Chats stay on this Mac (localStorage, the last 30). */

const CHAT_MAX = 30, CHAT_HISTORY = 3;
function loadChats() {
  return loadList("chats").filter((t) => t && typeof t.id === "string" && Array.isArray(t.turns));
}
const chat = { threads: loadChats(), id: localGet("chat.current") || null, ctrl: null, live: -1, removed: new Set(), askKey: "", draftRoute: "cloud", draftWhere: null };
const curChat = () => chat.threads.find((t) => t.id === chat.id) || null;

/* merged with what is stored, so a chat Quick Search handed over is never lost; newest first, the last 30 */
function saveChats() {
  const mine = new Map(chat.threads.map((t) => [t.id, t]));
  for (const t of loadChats()) if (!mine.has(t.id) && !chat.removed.has(t.id)) mine.set(t.id, t);
  chat.threads = [...mine.values()].sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, CHAT_MAX);
  saveList("chats", chat.threads);
}
addEventListener("storage", (e) => {
  if (e.key !== "mse.chats" || MINI || chat.ctrl) return;
  const mine = new Map(chat.threads.map((t) => [t.id, t]));
  for (const t of loadChats()) if (!chat.removed.has(t.id)) mine.set(t.id, mine.get(t.id) || t);
  chat.threads = [...mine.values()].sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, CHAT_MAX);
  if (state.mode === "ask") renderChatRail();
});

function newChat() {
  stopChat();
  chat.id = null; localSet("chat.current", "");
  chat.draftRoute = "cloud"; chat.draftWhere = null;
  renderChat();
  $("#ask-q")?.focus();
}
function openChat(id) {
  if (id === chat.id) return;
  stopChat();
  chat.id = id; localSet("chat.current", id);
  renderChat();
  scrollChat(true);
}
function removeChat(id) {
  const t = chat.threads.find((x) => x.id === id);
  if (!t) return;
  if (id === chat.id) { stopChat(); chat.id = null; }
  chat.removed.add(id);
  chat.threads = chat.threads.filter((x) => x.id !== id);
  saveChats(); renderChat();
  toast("Chat removed", { label: "Undo", run: () => { chat.removed.delete(id); chat.threads.push(t); saveChats(); renderChat(); } });
}
function stopChat() { chat.ctrl?.abort(); }

/* from the Search view (the Ask bar, ⌥↵) or from Quick Search: a new chat with this question */
function askInChat(q) {
  setMode("ask", false);
  newChat();
  if (q && askReady()) sendChat(q);
  else { $("#ask-q").value = q || ""; autosize(); chatField?.update(); $("#ask-q").focus(); }
}

async function sendChat(text, retryAt = null) {
  const q = String(text || "").trim();
  if (!q || chat.ctrl) return;
  if (!askReady()) { renderChat(); return; }
  let t = curChat();
  if (!t) {
    t = { id: `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, title: (splitShortcut(q).rest || q).slice(0, 80),
      created: Date.now(), updated: Date.now(), turns: [], ...(chat.draftRoute === "local" ? { route: "local" } : {}),
      where: chat.draftWhere || { scope: state.scope || "all", path: "" } };
    chat.threads.unshift(t); chat.id = t.id; localSet("chat.current", t.id);
  }
  t.where = t.where || { scope: state.scope || "all", path: "" };      // the folder is fixed per chat from its first question
  const at = retryAt ?? t.turns.length;
  const sc = splitShortcut(q);
  if (sc.tokens) t.where = { scope: sc.tokens.join(","), path: "" };   // a shortcut in the question moves this chat, as before
  const w = t.where;
  const local = t.route === "local" && askRouteKnown();                // this chat stays on this Mac
  const turn = { q, a: "", sources: [], citations: [], steps: [], at: Date.now(), scope: whereLabel(w), stepsOpen: false };
  turn.steps.push({ kind: "search", title: `Search(${whereLabel(w)}, "${sc.tokens ? sc.rest : q}")`, res: null, ms: null, run: true, inferred: true });
  t.turns[at] = turn;
  const history = t.turns.slice(Math.max(0, at - CHAT_HISTORY), at).filter((x) => x.a && !x.error)
    .map((x) => ({ q: splitShortcut(x.q).rest || x.q, a: x.a }));
  const cols = w.scope && w.scope !== "all" ? w.scope.split(",") : null;
  const body = { q, ...(cols ? { collections: cols } : {}), ...(w.path ? { path: w.path } : {}),
    ...(history.length ? { history } : {}), ...(local ? { route: "local" } : {}) };
  const ctrl = new AbortController();
  chat.ctrl = ctrl; chat.live = at;
  t.updated = Date.now(); saveChats();
  renderChat(); scrollChat(true);
  const t0 = Date.now();
  try {
    await askStream(body, (m) => {
      if (chat.ctrl !== ctrl) return;
      if (m.type === "step") {
        turn.steps = turn.steps.filter((s) => !s.inferred);            // the service reports its own steps from now on
        addStep(turn, m);
      } else if (m.type === "sources") {
        turn.sources = m.sources || [];
        if (m.scope?.label) turn.scope = m.scope.label;
        inferAtSources(turn);
      } else if (m.type === "token") {
        if (!turn.steps.some((s) => s.kind === "model")) addStep(turn, { kind: "model", text: modelHint(w, local), inferred: true });
        turn.a += m.text || "";
      } else if (m.type === "done") {
        Object.assign(turn, { citations: m.citations || [], model: m.model, ms: m.timings?.total_ms ?? Date.now() - t0, answered: m.answered, ...(m.answer ? { a: m.answer } : {}),
          ...(m.where ? { where: m.where, provider: m.provider || null, fallback_reason: m.fallback_reason || null, redacted: m.redacted ?? null, ...(m.training ? { training: true } : {}) } : {}) });
        const ms = turn.steps.find((s) => s.kind === "model" && s.inferred);
        if (ms && m.model) ms.title = stepTitle("model", m.model);
        finishSteps(turn);
      } else if (m.type === "error") { turn.error = m.message || "The answer failed."; failSteps(turn); }
      scheduleChatRender();
    }, ctrl.signal);
  } catch (e) {
    if (e.name === "AbortError") { turn.stopped = true; finishSteps(turn); }
    else {
      turn.error = e.code === "ask_disabled" ? "Ask is turned off." : e.message;
      failSteps(turn);
      if (e.code === "ask_disabled" && state.status?.ask) state.status.ask.enabled = false;
    }
  } finally {
    if (chat.ctrl === ctrl) { chat.ctrl = null; chat.live = -1; }
    finishSteps(turn);
    t.updated = Date.now(); saveChats();
    if (curChat() === t) { renderChat(); scrollChat(); }
  }
}

let chatRenderQueued = false;
function scheduleChatRender() {
  if (chatRenderQueued) return;
  chatRenderQueued = true;
  soon(() => {
    chatRenderQueued = false;
    const t = curChat(), node = $(`#chat-thread [data-turn="${chat.live}"]`);
    if (!t || !node || !t.turns[chat.live]) { renderChat(); return; }
    const stick = nearBottom();
    node.replaceWith(turnNode(t.turns[chat.live], chat.live));
    if (stick) scrollChat(true);
  });
}
const nearBottom = () => { const b = $("#chat-thread"); return !b || b.scrollHeight - b.scrollTop - b.clientHeight < 80; };
function scrollChat(force) { const b = $("#chat-thread"); if (b && (force || nearBottom())) b.scrollTop = b.scrollHeight; }

function renderChat() {
  if (MINI || !$("#chat")) return;
  renderChatRail(); renderChatHead(); renderCloudSetup(); renderChatThread(); renderComposer();
}

function ago2(ms) { return ago(ms / 1000); }
function renderChatRail() {
  const rail = $("#chat-rail");
  const items = chat.threads.map((t) => el("div", { class: "cr-item" + (t.id === chat.id ? " on" : ""), role: "listitem" },
    el("button", { class: "cr-open", title: t.title, "aria-current": t.id === chat.id ? "true" : null, onclick: () => openChat(t.id) },
      el("span", { class: "cr-title" }, t.title || "Untitled"),
      el("span", { class: "cr-meta" }, `${t.turns.length} question${t.turns.length === 1 ? "" : "s"} · ${ago2(t.updated || t.created)}${t.route === "local" ? " · local" : ""}`)),
    el("button", { class: "cr-x", title: "Remove this chat", "aria-label": `Remove the chat ${t.title}`, html: ICON.close, onclick: () => removeChat(t.id) })));
  rail.replaceChildren(
    el("button", { class: "btn small new-chat", onclick: newChat }, "+ New chat"),
    el("h4", {}, "Recent"),
    items.length ? el("div", { class: "cr-list", role: "list" }, items) : el("p", { class: "cr-none" }, "Your chats appear here. They stay on this Mac."));
}

function renderChatHead() {
  const t = curChat();
  $("#chat-head").replaceChildren(
    el("button", { class: "btn small chats-btn", "aria-haspopup": "menu", onclick: (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      openMenu(r.left, r.bottom + 4, [{ label: "New chat", run: newChat }, ...(chat.threads.length ? ["-"] : []),
        ...chat.threads.slice(0, 15).map((x) => ({ label: x.title || "Untitled", title: x.title, checked: x.id === chat.id, run: () => openChat(x.id) }))]);
    } }, "Chats ▾"),
    el("h2", { class: "chat-title", title: t?.title || "" }, t?.title || "New chat"),
    t ? el("button", { class: "btn small ghost", onclick: newChat, title: "Start a new chat (the context of this one is not carried over)" }, "+ New chat") : null);
}

function renderChatThread() {
  const box = $("#chat-thread");
  const t = curChat();
  if (!t || !t.turns.length) { box.replaceChildren(chatEmpty()); return; }
  box.replaceChildren(el("div", { class: "turns" }, t.turns.map((x, i) => turnNode(x, i))));
}

function turnNode(x, i) {
  const live = i === chat.live && !!chat.ctrl;
  const sc = splitShortcut(x.q);
  const cited = new Set(x.citations || []);
  const secs = x.ms ? `${(x.ms / 1000).toFixed(1)} s` : "";
  const kids = [
    el("div", { class: "t-q" }, el("span", { class: "t-prompt", "aria-hidden": "true" }, ">"),
      el("span", { class: "t-text" }, sc.tokens ? sc.rest : x.q), sc.tokens ? el("span", { class: "t-in", title: `Asked in ${sc.label}` }, `in ${sc.label}`) : null),
  ];
  if (x.steps?.length) kids.push(stepsNode(x, i, live));
  if (x.error) kids.push(el("p", { class: "t-error" }, x.error));
  else if (!x.a) { if (!live) kids.push(el("p", { class: "t-wait" }, x.stopped ? "Stopped before an answer." : "No answer.")); }
  else kids.push(el("div", { class: "t-answer" + (live ? " streaming" : ""), html: answerBlocks(x.a, x.sources || []) }));
  if (x.a && x.sources?.length) kids.push(sourcesNode(x, i, cited));
  kids.push(el("div", { class: "t-actions" },
    live ? el("button", { class: "btn small", onclick: stopChat, title: "Stop (esc)" }, "Stop") : null,
    !live && x.a ? el("button", { class: "btn small ghost", onclick: () => copy(turnCopy(x), "Answer copied with its sources") }, "Copy") : null,
    !live && !chat.ctrl ? el("button", { class: "btn small ghost", title: "Ask this question again", onclick: () => sendChat(x.q, i) }, "Retry") : null,
    el("span", { class: "t-meta" }, turnMeta(x, live, secs))));
  return el("div", { class: "turn" + (live ? " live" : ""), "data-turn": i, "aria-busy": String(live) }, kids);
}

/* ---- the steps: one line per step (⏺ grey while running, green when done, red on error), results under ⎿ ----
   The service sends {type: "step", kind, text, count?, ms?}; without them the steps are inferred from what arrives. */
function addStep(turn, s) {
  const kind = s.kind || "step", count = s.count, text = String(s.text || "");
  /* "Found n results" closes the running Search step instead of starting a line of its own */
  if (kind === "search" && count != null && !s.title) {
    const open = [...turn.steps].reverse().find((p) => p.kind === "search" && p.run);
    if (open) {
      open.run = false; open.inferred = false;
      open.res = s.res ?? `${count} result${count === 1 ? "" : "s"}`; open.ms = s.ms ?? open.ms;
      return;
    }
  }
  for (const p of turn.steps) p.run = false;
  const res = s.res ?? (count != null ? ({ search: `${count} result${count === 1 ? "" : "s"}`, read: `${count} passage${count === 1 ? "" : "s"}` }[kind] || String(count)) : null);
  const done = !!s.done || count != null || s.ms != null;
  turn.steps.push({ kind, title: s.title || stepTitle(kind, text), res, ms: s.ms ?? null, run: !done, err: false, inferred: !!s.inferred });
}
function stepTitle(kind, text) {
  const unq = (t) => t.replace(/^["\u201c\u201e]+|["\u201d]+$/g, "");
  const m = /^Searching (.+?) for (.+)$/i.exec(text);
  if (kind === "search" && m) return `Search(${m[1]}, "${unq(m[2])}")`;
  const name = { search: "Search", read: "Read", rewrite: "Rewrite", model: "Ask" }[kind] || (kind.charAt(0).toUpperCase() + kind.slice(1));
  const body = text.replace(/^(Reading|Asking|Searching|Found)\s+/i, "");
  return `${name}(${kind === "rewrite" ? unq(body.replace(/^the follow-up as:\s*/i, "")) : body})`;
}
/* the service sent no step events: show what arrived. Sources mean the search finished and the passages were read */
function inferAtSources(turn) {
  const n = turn.sources.length, docs = new Set(turn.sources.map((s) => `${s.collection}/${s.path}`)).size;
  const srch = turn.steps.find((s) => s.kind === "search" && s.inferred);
  if (srch) { srch.run = false; srch.res = `Found ${n} source${n === 1 ? "" : "s"}`; }
  if (!turn.steps.some((s) => s.kind === "read")) {
    addStep(turn, { kind: "read", title: `Read(${n} passage${n === 1 ? "" : "s"} from ${docs} document${docs === 1 ? "" : "s"})`, done: true, inferred: true });
  }
}
function modelHint(w, local) {
  const pred = predictWhere(w.scope, local ? "local" : chatRoute());
  return pred?.model || "answer";
}
function finishSteps(turn) { for (const s of turn.steps) s.run = false; }
function failSteps(turn) {
  for (const s of turn.steps) s.run = false;
  const last = turn.steps[turn.steps.length - 1];
  if (last) last.err = true;
}
function stepsNode(x, i, live) {
  const n = x.steps.length, secs = x.ms ? ` · ${(x.ms / 1000).toFixed(1)} s` : "";
  const label = `${n} step${n === 1 ? "" : "s"}${secs}`;
  if (live || x.stepsOpen) {
    const head = live ? null : el("button", { class: "t-sum", type: "button", "aria-expanded": "true", onclick: () => { x.stepsOpen = false; redrawTurn(i); } }, "▾ ", label);
    return el("div", { class: "t-steps" }, head, x.steps.map(stepLine));
  }
  return el("div", { class: "t-steps" }, el("button", { class: "t-sum", type: "button", "aria-expanded": "false",
    onclick: () => { x.stepsOpen = true; redrawTurn(i); } }, el("span", { class: "t-dot ok", "aria-hidden": "true" }, "⏺"), " ", label));
}
function stepLine(s) {
  const state = s.err ? "err" : s.run ? "run" : "ok";
  const res = [s.res, s.ms != null ? fmtMs(s.ms) : null].filter(Boolean).join(" · ");
  return el("div", { class: `t-step ${state}`, "data-kind": s.kind },
    el("div", { class: "t-line" }, el("span", { class: "t-dot", "aria-hidden": "true" }, "⏺"), el("span", { class: "t-title" }, s.title)),
    res ? el("div", { class: "t-res" }, el("span", { class: "t-glyph", "aria-hidden": "true" }, "⎿"), el("span", {}, res)) : null);
}
function redrawTurn(i) {
  const t = curChat(), node = $(`#chat-thread .turn[data-turn="${i}"]`);
  if (t && node && t.turns[i]) node.replaceWith(turnNode(t.turns[i], i)); else renderChatThread();
}
function sourcesNode(x, i, cited) {
  return el("div", { class: "t-sources" }, el("div", { class: "t-srch" }, "Sources"),
    x.sources.map((s) => el("button", { class: "src" + (cited.size && !cited.has(s.n) ? " uncited" : ""), type: "button", "data-n": s.n, "data-turn": i,
      title: `${s.collection}/${s.path}${s.page ? `, page ${s.page}` : ""}` },
      el("b", {}, `[${s.n}]`), el("span", { class: "src-path" }, el("em", {}, `${s.collection} / `), s.path),
      s.page ? el("i", {}, `p. ${s.page}`) : s.ts ? el("i", {}, s.ts) : null)));
}

/* the answer as clean markdown: paragraphs, lists, headings and code; [n] become citation chips */
function answerBlocks(text, sources) {
  const inl = (s) => inlineMarkdown(esc(s)).replace(/\[(\d{1,2})\]/g, (m, n) => {
    const src = sources.find((x) => String(x.n) === n);
    return src ? `<button class="cite" type="button" data-n="${n}" title="${esc(src.title || src.path)}${src.page ? `, page ${src.page}` : ""}">${n}</button>` : m;
  });
  const out = [];
  let para = [], list = null, code = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${inl(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((x) => `<li>${inl(x)}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const line of String(text || "").replace(/\r/g, "").split("\n")) {
    if (code !== null) {
      if (/^\s*```/.test(line)) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; } else code.push(line);
      continue;
    }
    if (/^\s*```/.test(line)) { flushPara(); flushList(); code = []; continue; }
    const h = /^\s*(#{1,4})\s+(.*)$/.exec(line), ul = /^\s*[-*•]\s+(.*)$/.exec(line), ol = /^\s*\d{1,2}[.)]\s+(.*)$/.exec(line);
    if (h) { flushPara(); flushList(); out.push(`<h4>${inl(h[2])}</h4>`); }
    else if (ul || ol) {
      flushPara();
      const tag = ul ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push((ul || ol)[1]);
    }
    else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line.trim()); }
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
  flushPara(); flushList();
  return out.join("");
}
/* "claude-opus-4-1 · ☁ cloud · 2.1 s · check the sources"; older services: "model · 2.1 s · check the sources" */
function turnMeta(x, live, secs) {
  if (live) return [];
  const bits = [];
  if (x.a && x.model) bits.push(x.where ? el("span", { class: "ta-model", title: whereTitle(x) }, x.model) : x.model);
  if (x.a && x.where) bits.push(whereBadge(x.where, whereTitle(x)));
  if (x.a && x.training) bits.push(trainBadge());
  if (x.a && x.fallback_reason) bits.push(el("span", { class: "ta-fb" }, fallbackText(x.fallback_reason)));
  if (secs) bits.push(secs);
  if (x.a) bits.push("check the sources");
  return bits.flatMap((b, i) => (i ? [" · ", b] : [b]));
}
function turnCopy(x) {
  const src = (x.sources || []).filter((s) => !x.citations?.length || x.citations.includes(s.n))
    .map((s) => `[${s.n}] ${s.title || s.path.split("/").pop()} — ${s.collection}/${s.path}${s.page ? `, p. ${s.page}` : ""}${s.ts ? ` at ${s.ts}` : ""}`);
  return `${x.a.trim()}${src.length ? `\n\nSources:\n${src.join("\n")}` : ""}`;
}

/* ------------------------------------------------------------------ Ask view: no banner above the thread
   Choosing a model lives in the model menu next to the message box (renderModelMenus) and keys live in Settings › Ask.
   With no provider connected at all, one quiet line under the box says so. */
const cs = { msg: null };
function activeProv() { const id = askCfg().provider; return (prov.data?.providers || []).find((p) => p.id === id) || null; }
function cloudReady() { const p = activeProv(); return !!p && (p.has_key === true || p.status === "ok"); }
function renderCloudSetup() { renderModelMenus(); }
if (!MINI && $("#ask-quiet-link")) $("#ask-quiet-link").addEventListener("click", () => showSettings("ask"));

/* the empty chat: what Ask does and example questions, or why it cannot answer yet.
   With cloud models the card above carries the one "Turn on Ask" button; this view adds a second one only for a
   service without cloud models. The memory it names is the local model's, and only when the local model will answer. */
function chatEmpty() {
  const a = state.status?.ask;
  const off = !askReady();
  const head = el("div", { class: "empty-mark", html: ICON.mark });
  loadLocalModels();
  if (off) {
    const cloudBox = cloudApi() && !!a && "route" in a;
    let text, action = null, fine = null;
    if (!state.status) text = "The search service is not running, so there is nothing to ask. Start it with mse service install.";
    else if (!a) text = "This search service has no Ask yet. Update MySearch, then come back here.";
    else if (!a.available) text = "The answer model is not installed on this Mac, so Ask cannot be turned on. Search works as before.";
    else if (cloudBox) {
      const c = askCfg(), localAns = localWillAnswer();
      text = "Ask writes a short answer from your top search results, with numbered references to the documents it used. It is off: nothing is sent. "
        + (localAns
          ? `Once it is on, the model on this Mac writes the answers (${c.route === "local" ? "Settings › Ask is set to Local" : "you allowed it as the fallback"}). It uses about ${localRamHint().replace("~", "")} while loaded.`
          : "Turning it on loads no model. The cloud model you choose in the menu under the message box writes the answers; the model on this Mac is loaded only if you switch to Local.");
      action = el("button", { class: "btn primary", id: "ask-enable", onclick: (e) => enableAsk(e.currentTarget) }, "Turn on Ask");
      fine = localAns ? "The local model loads with the first question (a few seconds) and unloads after 5 idle minutes. Settings › Ask turns Ask off again."
        : "Nothing is loaded. Settings › Ask turns Ask off again.";
    } else {
      text = askRouteKnown() && askCfg().route !== "local"
        ? "Ask writes a short answer from your top search results, with numbered references to the documents it used, using a cloud model or the model on this Mac (Settings › Ask chooses). It is off: nothing is loaded and nothing is sent."
        : "Ask writes a short answer from your top search results, with numbered references to the documents it used. A small language model runs on this Mac; nothing goes to the internet. It is off, so no model is loaded.";
      action = el("button", { class: "btn primary", id: "ask-enable", onclick: (e) => enableAsk(e.currentTarget) }, `Turn on Ask (uses ${localRamHint()} while loaded)`);
      fine = "The model loads with the first question (a few seconds) and unloads after 5 idle minutes. Settings › Ask turns it off again.";
    }
    return el("div", { class: "chat-empty" }, head, el("h2", {}, "Ask your documents"), el("p", {}, text),
      action ? el("div", { class: "actions" }, action) : null,
      fine ? el("p", { class: "fine" }, fine) : null);
  }
  const ex = shortcutExample();
  const route = chatRoute(), pred = predictWhere(state.scope, route);
  const nextLine = nextAnswerNodes(pred);
  const swap = pred && askRouteKnown() && askCfg().route !== "local"
    ? el("button", { class: "linklike", type: "button", onclick: () => setChatRoute(route === "local" ? "cloud" : "local") },
      route === "local" ? "Back to the cloud model" : `Answer this chat on this Mac (${localModelName()}, loads ~2.5 GB)`) : null;
  const examples = ["Which invoices are still unpaid?", "When was the company registered?", "Was steht im Vertrag zur Kündigungsfrist?", `${ex}: what did we agree last month?`];
  return el("div", { class: "chat-empty" }, head,
    el("h2", {}, "Ask your documents"),
    el("p", {}, "Ask in English or German. The answer is written from your top results, with references like ", el("button", { class: "cite", type: "button", tabindex: "-1" }, "1"), " that open the passage on the right. Follow-up questions remember the last three exchanges."),
    nextLine.length ? el("p", { class: "next-model" }, nextLine, swap ? [" ", swap] : null) : null,
    el("div", { class: "examples" }, examples.map((q) => el("button", { class: "chip ex", onclick: () => sendChat(q) }, q))),
    el("p", { class: "fine" }, `Reads ${whereLabel(chatWhere())}: pick a folder with the chip below or type @, or start the question with a name, like `, el("code", {}, `${ex}: …`), "."));
}

async function enableAsk(btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Turning on…";
  try {
    await post("/api/settings", { ask: { enabled: true } });
    if (state.status?.ask) state.status.ask.enabled = true;
    await poll(true);
    renderChat(); $("#ask-q").focus();
    toast(localWillAnswer() ? "Ask is on. The model on this Mac loads with your first question." : "Ask is on. No model is loaded.");
  } catch (e) { btn.disabled = false; btn.textContent = label; toast(cleanError(e.message)); }
}

function renderComposer() {
  const ready = askReady(), busy = !!chat.ctrl;
  const inp = $("#ask-q");
  inp.disabled = !ready;
  inp.placeholder = ready ? (curChat()?.turns.length ? "Follow up…" : "Ask about your documents") : "Ask is off";
  const send = $("#ask-send");
  send.textContent = busy ? "Stop" : "Ask";
  send.classList.toggle("primary", !busy);
  send.disabled = !ready && !busy;
  const sc = splitShortcut(inp.value);
  const eff = sc.tokens ? { scope: sc.tokens.join(","), path: "" } : chatWhere();
  const label = whereLabel(eff);
  const chip = $("#folder-chip");
  $("#folder-label").textContent = label;
  chip.classList.toggle("overridden", !!sc.tokens);
  chip.title = sc.tokens ? `This question reads ${label} (from its shortcut). Choose a folder to change where this chat reads.`
    : `This chat reads ${label}. Click to choose another folder, or type @ to mention one.`;
  const route = chatRoute();
  const pred = predictWhere(eff.scope, route);
  const pick = $("#ask-route");
  const settingsLocal = askCfg().route === "local";
  pick.hidden = !ready || !pred;
  for (const b of $$("button", pick)) {
    const isLocal = b.dataset.route === "local";
    b.setAttribute("aria-checked", String(isLocal ? (route === "local" || settingsLocal) : (route === "cloud" && !settingsLocal)));
    b.title = isLocal ? `This chat is answered on this Mac: ${localModelName()} (loads ~2.5 GB) and nothing is sent${settingsLocal ? " (Settings › Ask is set to Local)" : ""}`
      : settingsLocal ? "Switch back to the cloud model chosen in the menu" : "Answered by the cloud model chosen in the menu";
  }
  renderModelMenus();
  const WHY_SHORT = { chat: " (this chat)", folder: " (folder setting)", offline: " (offline)", nocloud: " (no cloud model ready)" };
  const who = !pred ? ["answers by a small local model"]
    : pred.where === "cloud" && pred.problem ? ["next answer: ", el("b", { class: "warn-t" }, PROBLEM_TEXT[pred.problem] || "no cloud model is ready"), " · pick a model, or switch to Local"]
    : pred.where === "cloud" ? ["next answer ", whereBadge("cloud", `${providerLabel(pred.provider)} · ${pred.model}`), " ", el("b", {}, pred.model), trainsOnInputs(pred.model, pred.provider) ? [" ", trainBadge()] : null]
    : ["next answer ", whereBadge("local", WHY_LOCAL[pred.why] ? `On this Mac: ${WHY_LOCAL[pred.why]}` : "On this Mac"), WHY_SHORT[pred.why] || ""];
  $("#composer-note").replaceChildren(ready
    ? el("span", {}, `Reads ${label}${sc.tokens ? " (from the shortcut)" : ""} · ↵ ask · ⇧↵ new line · @ mention a folder · `, ...who)
    : el("span", {}, "Turn Ask on to ask questions."));
}
/* Cloud (the setting) or Local, per chat: a new chat starts on Cloud */
function chatRoute() { const t = curChat(); return (t ? t.route : chat.draftRoute) === "local" ? "local" : "cloud"; }
function setChatRoute(r) {
  const t = curChat(), local = r === "local";
  if (!local && askCfg().route === "local") saveAsk({ route: "cloud" }, document.createElement("span"));   // Settings said Local: Cloud turns it back
  if (t) { if (local) t.route = "local"; else delete t.route; saveChats(); }
  else chat.draftRoute = local ? "local" : "cloud";
  renderComposer(); renderChatRail();
  if (!t || !t.turns.length) renderChatThread();
  $("#ask-q").focus();
}

function autosize() {
  const t = $("#ask-q");
  if (!t) return;
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, 180)}px`;
}

let chatField = null;
/* ------------------------------------------------------------------ where a chat reads
   The folder chip under the box opens a picker that drills into any subfolder of the indexed folders (GET /api/tree).
   Typing @ mentions a folder or a group. A chat keeps its folder: t.where = {scope, path}, sent as collections plus
   path (a whole-folder scope, relative to the collection; the service also reads filters.path). */
const treeCache = new Map();          // "collection|path" -> promise of that folder's immediate subfolders
const folderIdx = new Map();          // "collection|path" -> {c, path, name}: what the @ list has seen so far
function fetchTree(c, path = "") {
  const key = `${c}|${path}`;
  if (!treeCache.has(key)) {
    const p = api(`/api/tree?c=${enc(c)}&path=${enc(path)}`).then((j) => j.folders || []);
    p.catch(() => treeCache.delete(key));
    treeCache.set(key, p);
  }
  return treeCache.get(key);
}
function chatWhere(t = curChat()) { return (t ? t.where : chat.draftWhere) || { scope: state.scope || "all", path: "" }; }
function whereLabel(w = chatWhere()) {
  const sc = w.scope || "all";
  if (sc === "all") return "All folders";
  if (w.path) return [sc, ...w.path.split("/")].join(" › ");
  return scopeLabel(sc);
}
function setChatWhere(w) {
  const t = curChat();
  if (t) { t.where = w; saveChats(); } else chat.draftWhere = w;
  renderComposer();
}

/* the @ list: collections, groups and the subfolders loaded so far (three levels, a few hundred folders at most) */
const mention = { open: false, items: [], sel: 0, start: -1, q: "", seq: 0 };
function mentionContext(v, caret) {
  const m = /(^|\s)@([^\s@]*)$/.exec(v.slice(0, caret));
  return m ? { start: caret - m[2].length - 1, q: m[2] } : null;
}
function mentionUpdate() {
  if (MINI) return;
  const inp = $("#ask-q"), ctx = askReady() ? mentionContext(inp.value, inp.selectionStart ?? inp.value.length) : null;
  if (!ctx) { mentionClose(); return; }
  Object.assign(mention, { start: ctx.start, q: ctx.q, sel: 0 });
  if (!mention.open) { mention.open = true; mentionIndex(); }
  mentionRender();
}
function mentionClose() { mention.open = false; const p = $("#at-pop"); if (p) { p.hidden = true; p.replaceChildren(); } }
function mentionItems(q) {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const all = [
    ...state.collections.map((c) => ({ scope: c.name, path: "", label: c.name, sub: "folder", hay: c.name })),
    ...(state.groups || []).map((g) => ({ scope: g.name, path: "", label: g.label || g.name, sub: "group", hay: `${g.label || ""} ${g.name}` })),
    ...[...folderIdx.values()].map((f) => ({ scope: f.c, path: f.path, label: [f.c, ...f.path.split("/")].join(" › "), sub: "folder", hay: `${f.c} ${f.path}` })),
  ];
  return all.filter((it) => terms.every((t) => it.hay.toLowerCase().includes(t))).slice(0, 12);
}
function mentionRender() {
  const pop = $("#at-pop");
  if (!pop) return;
  mention.items = mentionItems(mention.q);
  if (!mention.items.length) { pop.hidden = true; pop.replaceChildren(); return; }
  mention.sel = Math.min(mention.sel, mention.items.length - 1);
  pop.hidden = false;
  pop.replaceChildren(el("div", { class: "at-head" }, "Folders and groups", el("span", {}, "↵ read · esc")),
    ...mention.items.map((it, i) => el("div", { class: "at-row" + (i === mention.sel ? " act" : ""), role: "option", "aria-selected": String(i === mention.sel),
      onmousedown: (e) => { e.preventDefault(); mentionAccept(i); } },
      el("span", { class: "at-label" }, it.label), el("span", { class: "at-sub" }, it.sub))));
  $$(".at-row", pop)[mention.sel]?.scrollIntoView({ block: "nearest" });
}
async function mentionIndex() {
  const seq = ++mention.seq;
  const queue = state.collections.map((c) => ({ c: c.name, path: "", depth: 0 }));
  let budget = 300;
  const worker = async () => {
    while (queue.length && budget-- > 0 && seq === mention.seq) {
      const job = queue.shift();
      let kids = [];
      try { kids = await fetchTree(job.c, job.path); } catch { continue; }
      for (const k of kids) {
        folderIdx.set(`${job.c}|${k.path}`, { c: job.c, path: k.path, name: k.name });
        if (k.children && job.depth < 2) queue.push({ c: job.c, path: k.path, depth: job.depth + 1 });
      }
      if (mention.open && seq === mention.seq) mentionRender();
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}
function mentionAccept(i) {
  const it = mention.items[i];
  if (!it) return;
  const inp = $("#ask-q"), v = inp.value, caret = inp.selectionStart ?? v.length;
  inp.value = v.slice(0, mention.start) + v.slice(caret);
  inp.setSelectionRange(mention.start, mention.start);
  mentionClose();
  setChatWhere({ scope: it.scope, path: it.path });
  autosize(); chatField?.update();
  inp.focus();
}
function mentionKey(e) {
  if (!mention.open || !mention.items.length || e.isComposing) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); mention.sel = (mention.sel + (e.key === "ArrowDown" ? 1 : -1) + mention.items.length) % mention.items.length; mentionRender();
  } else if ((e.key === "Enter" && !e.shiftKey) || e.key === "Tab") { e.preventDefault(); mentionAccept(mention.sel); }
  else if (e.key === "Escape") { e.preventDefault(); mentionClose(); }
  else return;
  e.stopImmediatePropagation();
}

/* the folder picker: scopes first (All, folders, groups), then the subfolders of a folder, one level at a time */
const dirPick = { el: null, c: null, path: "", rows: [], filter: "", act: 0, seq: 0 };
function openFolderPicker() {
  if (dirPick.el) { closeFolderPicker(true); return; }
  mentionClose();
  const w = chatWhere();
  const one = w.scope && w.scope !== "all" && !w.scope.includes(",") && !isGroup(w.scope) ? w.scope : null;
  Object.assign(dirPick, { c: one, path: one ? (w.path || "") : "", rows: [], filter: "", act: 0 });
  const input = el("input", { class: "fp-filter", type: "search", placeholder: "Type to filter", "aria-label": "Filter folders", autocomplete: "off", spellcheck: "false" });
  input.addEventListener("input", () => { dirPick.filter = input.value; dirPick.act = 0; dirPickRender(); });
  dirPick.input = input;
  dirPick.el = el("div", { class: "fp-pop", role: "dialog", "aria-label": "Choose the folder this chat reads" },
    el("div", { class: "fp-head" },
      el("button", { class: "fp-back", type: "button", "aria-label": "Up one level", title: "Up one level (←)", onclick: dirPickUp }, "←"),
      el("nav", { class: "fp-crumbs", id: "fp-crumbs", "aria-label": "Current folder" })),
    input,
    el("div", { class: "fp-list", id: "fp-list", role: "listbox", "aria-label": "Folders" }),
    el("div", { class: "fp-foot" }, el("span", { class: "fp-hint" }, "↵ open · → in · ← up · ⌘↵ use"),
      el("button", { class: "btn small primary fp-use", type: "button", id: "fp-use", onclick: dirPickUse }, "Use this folder")));
  dirPick.el.addEventListener("keydown", dirPickKey);
  $("#composer").append(dirPick.el);
  $("#folder-chip").setAttribute("aria-expanded", "true");
  document.addEventListener("mousedown", dirPickAway, true);
  dirPickRender();
  input.focus();
}
function closeFolderPicker(focus) {
  if (!dirPick.el) return;
  dirPick.el.remove(); dirPick.el = null; dirPick.seq++;
  document.removeEventListener("mousedown", dirPickAway, true);
  $("#folder-chip")?.setAttribute("aria-expanded", "false");
  if (focus) $("#folder-chip")?.focus();
}
function dirPickAway(e) { if (dirPick.el && !dirPick.el.contains(e.target) && !e.target.closest("#folder-chip")) closeFolderPicker(false); }
function dirPickGo(c, path) { Object.assign(dirPick, { c, path, filter: "", act: 0 }); if (dirPick.input) dirPick.input.value = ""; dirPickRender(); dirPick.input?.focus(); }
function dirPickUp() {
  if (dirPick.path) { const p = dirPick.path.split("/"); dirPickGo(dirPick.c, p.slice(0, -1).join("/")); }
  else if (dirPick.c) dirPickGo(null, "");
}
function dirPickChoose(scope, path) { setChatWhere({ scope, path }); closeFolderPicker(true); }
function dirPickUse() {
  if (!dirPick.c) dirPickChoose("all", "");
  else dirPickChoose(dirPick.c, dirPick.path);
}
async function dirPickRender() {
  const pop = dirPick.el;
  if (!pop) return;
  const seq = ++dirPick.seq;
  const crumbs = [el("button", { class: "fp-crumb", type: "button", onclick: () => dirPickGo(null, "") }, "All")];
  if (dirPick.c) {
    crumbs.push(el("span", { class: "fp-sep", "aria-hidden": "true" }, "›"), el("button", { class: "fp-crumb", type: "button", onclick: () => dirPickGo(dirPick.c, "") }, dirPick.c));
    const segs = dirPick.path ? dirPick.path.split("/") : [];
    segs.forEach((s, i) => crumbs.push(el("span", { class: "fp-sep", "aria-hidden": "true" }, "›"),
      el("button", { class: "fp-crumb", type: "button", onclick: () => dirPickGo(dirPick.c, segs.slice(0, i + 1).join("/")) }, s)));
  }
  pop.querySelector("#fp-crumbs").replaceChildren(...crumbs);
  pop.querySelector(".fp-back").disabled = !dirPick.c;
  pop.querySelector("#fp-use").textContent = !dirPick.c ? "Use all folders" : dirPick.path ? "Use this folder" : `Use ${dirPick.c}`;
  const list = pop.querySelector("#fp-list");
  let rows;
  if (!dirPick.c) {
    rows = [{ label: "All folders", sub: "every folder marked Include in All", scope: "all", path: "" },
      ...state.collections.map((c) => ({ label: c.name, sub: `${plural(c.stats?.items || 0, "item")}`, drill: true, c: c.name, path: "" })),
      ...(state.groups || []).map((g) => ({ label: g.label || g.name, sub: `group · ${g.collections.join(" + ")}`, scope: g.name, path: "" }))];
  } else {
    list.replaceChildren(el("p", { class: "fp-none" }, "Loading…"));
    let folders;
    try { folders = await fetchTree(dirPick.c, dirPick.path); }
    catch (e) { if (seq === dirPick.seq) list.replaceChildren(el("p", { class: "fp-none" }, `Could not list the folders: ${e.message}`)); return; }
    if (seq !== dirPick.seq) return;
    rows = folders.map((f) => ({ label: f.name, sub: f.children ? "has subfolders" : "", drill: !!f.children, c: dirPick.c, path: f.path }));
  }
  const q = dirPick.filter.trim().toLowerCase();
  if (q) rows = rows.filter((r) => r.label.toLowerCase().includes(q));
  dirPick.rows = rows;
  dirPick.act = Math.min(dirPick.act, Math.max(0, rows.length - 1));
  list.replaceChildren(...(rows.length ? rows.map((r, i) => el("div", { class: "fp-row" + (i === dirPick.act ? " act" : ""), role: "option", "aria-selected": String(i === dirPick.act),
    onmousedown: (e) => { e.preventDefault(); dirPickAct(i); } },
    el("span", { class: "fp-name" }, r.label), el("span", { class: "fp-sub" }, r.sub),
    r.drill ? el("span", { class: "fp-more", "aria-hidden": "true" }, "›") : null)) : [el("p", { class: "fp-none" }, q ? "No folder matches." : dirPick.c ? "No subfolders here." : "Nothing to choose.")]));
}
function dirPickAct(i) {
  const r = dirPick.rows[i];
  if (!r) return;
  if (r.drill) dirPickGo(r.c, r.path);
  else if (r.scope !== undefined) dirPickChoose(r.scope, r.path);
  else dirPickChoose(r.c, r.path);
}
function dirPickKey(e) {
  const rows = dirPick.el.querySelectorAll(".fp-row");
  const input = dirPick.input;
  if (e.key === "Escape") { e.preventDefault(); closeFolderPicker(true); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!rows.length) return;
    dirPick.act = (dirPick.act + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
    rows.forEach((r, k) => { r.classList.toggle("act", k === dirPick.act); r.setAttribute("aria-selected", String(k === dirPick.act)); });
    rows[dirPick.act]?.scrollIntoView({ block: "nearest" });
  }
  else if (e.key === "ArrowRight" && rows.length && dirPick.rows[dirPick.act]?.drill) { e.preventDefault(); dirPickAct(dirPick.act); }
  else if (e.key === "ArrowLeft" && (e.target !== input || !input.value)) { e.preventDefault(); dirPickUp(); }
  else if (e.key === "Enter") {
    e.preventDefault();
    if (e.metaKey) dirPickUse(); else if (rows.length) dirPickAct(dirPick.act);
  }
  else return;
  e.stopPropagation();
}

if (!MINI) {
  chatField = shortcutField({ input: $("#ask-q"), mirror: null, chip: $("#ask-chip"), box: $("#ask-suggest"), place: false, tabs: () => $("#scopes") });
  $("#ask-q").addEventListener("input", () => { autosize(); renderComposer(); mentionUpdate(); });
  $("#ask-q").addEventListener("click", mentionUpdate);
  $("#ask-q").addEventListener("keyup", (e) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) mentionUpdate(); });
  $("#ask-q").addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== $("#ask-q")) mentionClose(); }, 0));
  $("#ask-q").addEventListener("keydown", mentionKey);
  $("#ask-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); e.stopPropagation(); if (!chat.ctrl) submitChat(); }
    else if (e.key === "Escape" && chat.ctrl) { e.preventDefault(); e.stopPropagation(); stopChat(); }
  });
  $("#folder-chip").addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openFolderPicker(); });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); if (chat.ctrl) stopChat(); else submitChat(); });
  $("#ask-route").addEventListener("click", (e) => { const b = e.target.closest("[data-route]"); if (b) setChatRoute(b.dataset.route); });
  for (const b of $$("#mode-switch button")) b.addEventListener("click", () => setMode(b.dataset.mode));
  document.addEventListener("click", (e) => {
    const c = e.target.closest("#chat-thread .cite, #chat-thread .src");
    if (!c) return;
    e.preventDefault();
    const i = Number(c.dataset.turn ?? c.closest("[data-turn]")?.dataset.turn);
    openCited(curChat()?.turns[i]?.sources, c.dataset.n);
  });
}
function submitChat() {
  const inp = $("#ask-q"), q = inp.value.trim();
  if (!q) return;
  inp.value = ""; autosize(); chatField?.update();
  sendChat(q);
}

/* Settings › Ask */
function renderAskSettings() {
  const a = state.status?.ask;
  $('#set-nav [data-sec="ask"]').hidden = !a;
  if (!a) { if (state.section === "ask" && !$("#view-settings").hidden) goSection("search"); return; }
  const f = $("#ask-form");
  if (!f.dataset.busy) f.enabled.checked = !!a.enabled;
  f.enabled.disabled = !a.available;
  const mb = a.ram_mb ? `${Math.round(a.ram_mb).toLocaleString()} MB` : null;
  $("#ask-model").textContent = a.model || "—";
  $("#ask-state").textContent = !a.available
    ? "The answer model is not installed, so Ask cannot be turned on. Search works as before."
    : !a.enabled ? "Off. No model is loaded."
    : a.loaded ? `Loaded now${mb ? `, using ${mb} of memory` : ""}. It unloads after 5 idle minutes.`
    : "route" in a && cloudApi() && !localWillAnswer() ? "On. The cloud model chosen above writes the answers. The model on this Mac is not loaded unless you choose Local."
    : "On. The model loads when you ask (a few seconds the first time) and unloads after 5 idle minutes.";
  if ("local_model" in a) $("#ask-model").textContent = a.local_model || "—";
  else if (a.provider) $("#ask-model").textContent = localModelName();
  if ("route" in a && prov.api === null && !prov.loading) loadProviders();   // a service with cloud models: learn the names
  if (!MINI) loadLocalModels();
  renderAskCloudHead();
}
$("#ask-form").addEventListener("change", async (e) => {
  if (e.target.name !== "enabled") return;
  const f = $("#ask-form"), out = $(".saved", f), on = e.target.checked;
  f.dataset.busy = "1"; out.classList.remove("err"); out.textContent = on ? "Turning on…" : "Turning off and unloading…";
  try {
    await post("/api/settings", { ask: { enabled: on } });
    out.textContent = on ? "On." : "Off. The model was unloaded.";
    if (state.status?.ask) state.status.ask.enabled = on;
  } catch (err) { e.target.checked = !on; out.textContent = err.message; out.classList.add("err"); }
  delete f.dataset.busy;
  await poll(true); renderAsk();
});

/* ------------------------------------------------------------------ Ask: which model answers (cloud providers)
   GET /api/ask/providers -> {route, provider, model, online, providers: [{id, label, kind, base_url, builtin, has_key,
   models, status, error?}]}. A key goes in once with POST /api/ask/providers and lives in the macOS Keychain: the
   service never sends one back and this page never keeps one (the field is cleared as soon as it is saved). A 404
   means a service without cloud models: Settings › Ask then shows only the local model, as before. */

const prov = { api: null, data: null, loading: null, error: null, tests: {}, replacing: new Set(), pick: {}, refreshing: new Set(), connecting: false, connectMsg: null };
try { prov.pick = JSON.parse(localGet("askPick") || "{}") || {}; } catch { prov.pick = {}; }
const ROUTES = [
  ["cloud", "Cloud", "The cloud model chosen in the menu answers. The model on this Mac is never loaded unless you choose Local, a folder keeps its answers here, or you allow the fallback below."],
  ["local", "Local", "Always the model on this Mac (loads about 2.5 GB when used). Nothing is sent anywhere."],
];
const PROBLEM_TEXT = { no_key: "the chosen provider has no key", offline: "the provider is not reachable", unknown_provider: "the chosen provider is not set up", no_model: "no model is chosen" };
const PROV_STATE = { ok: ["ok", "Ready"], no_key: ["warn", "Needs a key"], offline: ["", "Offline"], error: ["danger", "Error"] };
const KIND_NAME = { "openai-chat": "OpenAI-compatible (chat completions)", "openai-responses": "OpenAI Responses API", anthropic: "Anthropic Messages API" };
const KIND_SHORT = { "openai-chat": "OpenAI-compatible", "openai-responses": "OpenAI Responses", anthropic: "Anthropic API" };
const PRESETS = [   // well-known OpenAI-compatible endpoints; anything else goes in by hand
  { label: "DeepSeek", base_url: "https://api.deepseek.com/v1", models: "deepseek-chat, deepseek-reasoner" },
  { label: "OpenRouter", base_url: "https://openrouter.ai/api/v1", models: "" },
  { label: "LM Studio", base_url: "http://localhost:1234/v1", models: "", local: true },
  { label: "Ollama", base_url: "http://localhost:11434/v1", models: "", local: true },
];
const BUILTIN_LABEL = { anthropic: "Claude API" };
const WHERE_ICON = {
  cloud: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.4 12.5h7.3a2.8 2.8 0 0 0 .2-5.6 4 4 0 0 0-7.7-.6 3.1 3.1 0 0 0 .2 6.2z"/></svg>',
  local: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3" width="11" height="7.5" rx="1.2"/><path d="M1 13h14"/></svg>',
};
/* done.fallback_reason (mse/ask.py): why the local model answered although the route allowed the cloud */
const FALLBACK = { offline: "offline", no_key: "no key for the cloud model", ask_cloud_off: "this folder keeps answers on this Mac",
  unknown_provider: "the cloud provider is not set up", no_model: "no cloud model chosen", insecure_url: "the provider's address is not https",
  keychain: "the key could not be read from the Keychain", error: "the cloud model failed",
  training_unconfirmed: "this model's provider may use inputs for training — check the provider's terms, and that is not confirmed" };
const WHY_LOCAL = { route: "Settings › Ask is set to Local only", chat: "this chat is set to Local", folder: "a folder it reads keeps answers on this Mac",
  offline: "this Mac is offline", nocloud: "no cloud model is ready" };

const modelId = (m) => (typeof m === "string" ? m : m?.id || m?.name || "");
const cloudApi = () => prov.api === true && !!prov.data;
const askRouteKnown = () => !!state.status?.ask && "route" in state.status.ask;
const fmtMs = (ms) => (ms == null ? "" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const fallbackText = (r) => { const k = String(r).split(":")[0].trim(); return `answered locally: ${FALLBACK[k] || String(r).replace(/_/g, " ")}`; };
const hostOf = (u) => { try { return new URL(u).host; } catch { return u || ""; } };
function providerLabel(id) {
  if (!id) return "";
  return (prov.data?.providers || []).find((p) => p.id === id)?.label || BUILTIN_LABEL[id] || id;
}
/* route, provider, model, online, where_next: the status (fresh every poll) over the providers list (fresh on load) */
function askCfg() {
  const out = { route: "cloud", provider: null, model: null, online: null, where_next: null, local_fallback: false, next_problem: null };
  for (const src of [prov.data, state.status?.ask]) if (src) for (const k of Object.keys(out)) if (src[k] !== undefined && src[k] !== null) out[k] = src[k];
  if (out.route === "auto") { out.route = "cloud"; out.local_fallback = true; }      // the old name of cloud + fallback
  return out;
}
/* the local model's name: status.ask.model meant exactly that before cloud models existed */
function localModelName() {
  const a = state.status?.ask || {};
  return a.local_model || (!a.provider && a.model) || "the model on this Mac";
}
function cloudRedact() { const v = state.status?.ask?.cloud_redact ?? state.settings?.ask?.cloud_redact; return v === undefined ? null : v !== false; }

/* a small "cloud" or "local" badge */
function whereBadge(where, title) {
  const w = where === "cloud" ? "cloud" : "local";
  return el("span", { class: `wb ${w}`, title: title || (w === "cloud" ? "Answered by a cloud model" : "Answered on this Mac") },
    el("span", { class: "wb-i", html: WHERE_ICON[w] }), w);
}
function whereTitle(x) {    // x: a turn, or the `done` of an answer
  const who = [providerLabel(x.provider), x.model].filter(Boolean).join(" · ");
  const n = typeof x.redacted === "number" ? x.redacted : x.redacted === true ? -1 : 0;   // done.redacted: identifiers masked
  if (x.where === "cloud") return `Answered in the cloud by ${who || "a cloud model"}.${n > 0 ? ` ${plural(n, "personal identifier")} masked before sending.` : n < 0 ? " Personal identifiers were masked before sending." : ""}`;
  const fb = x.fallback_reason ? fallbackText(x.fallback_reason) : "";
  return `Answered on this Mac${x.model ? ` by ${x.model}` : ""}.${fb ? ` ${fb[0].toUpperCase()}${fb.slice(1)}${/:/.test(x.fallback_reason) ? ` (${x.fallback_reason})` : ""}.` : ""}`;
}
/* which model the next question will reach, for a scope ("all", names, a group; null: leave folders out) and the chat's own choice */
function predictWhere(scope, override) {
  const a = state.status?.ask;
  if (!a || !("route" in a)) return null;
  const local = (why) => ({ where: "local", model: localModelName(), why });
  const c = askCfg();
  if (c.route === "local") return local("route");
  if (override === "local") return local("chat");
  // a folder with "Allow cloud answers" off keeps every question that reads it on this Mac
  if (scope != null && scopeCollections(scope).some((n) => state.collections.find((x) => x.name === n)?.ask_cloud === false)) return local("folder");
  if (c.where_next === "cloud") {
    const problem = c.next_problem || (cloudApi() && !menuSelection() ? "no_model" : null);
    return { where: "cloud", model: a.next_model || c.model || "a cloud model", provider: c.provider, ...(problem ? { problem } : {}) };
  }
  return local(c.online === false ? "offline" : "nocloud");
}
function nextAnswerNodes(pred) {
  if (!pred) return [];
  if (pred.where === "cloud" && pred.problem) return ["Next answer: ", el("b", { class: "warn-t" }, PROBLEM_TEXT[pred.problem] || "no cloud model is ready"), " · choose a model in the menu under the message box, or switch the chat to Local"];
  if (pred.where === "cloud") {
    const masked = cloudRedact();
    return ["Next answer: ", el("b", { title: [providerLabel(pred.provider), pred.model].filter(Boolean).join(" · ") }, pred.model), " ", whereBadge("cloud", `${providerLabel(pred.provider)} · ${pred.model}`),
      ...(trainsOnInputs(pred.model, pred.provider) ? [" ", trainBadge()] : []),
      ` · sends your question and the top passages${masked === true ? ", identifiers masked" : masked === false ? ", identifiers not masked" : ""}`];
  }
  return ["Next answer: ", el("b", {}, pred.model), " ", whereBadge("local", "Answers on this Mac"), ` · stays on this Mac${pred.why && pred.why !== "route" ? ` (${WHY_LOCAL[pred.why]})` : ""}`];
}

async function loadProviders() {
  if (prov.loading) return prov.loading;
  prov.loading = (async () => {
    try { prov.data = await api("/api/ask/providers"); prov.api = true; prov.error = null; }
    catch (e) {
      if (e.status === 404) { prov.api = false; prov.data = null; }   // a service without cloud models
      else prov.error = e.message;                                    // down or failing: keep what was shown
    }
    prov.loading = null;
    renderAskCloud(); renderPrivacyLede();
    if (!MINI && state.mode === "ask" && !chat.ctrl) renderChat();
  })();
  return prov.loading;
}

/* Settings › Ask: the lede, the local-model row and the Model section */
const ASK_LEDE = "Type a question and get a short answer written from your top results, with numbered references to the documents it used.";
function renderAskCloud() {
  const on = cloudApi();
  $("#ask-cloud").hidden = !on;            // always shown (Ask on or off, online or not) once the service has cloud models
  $("#ask-route-sec").hidden = !on;
  $("#h-answers").hidden = !on;
  $("#ask-lede").textContent = on
    ? `${ASK_LEDE} A cloud model you connect writes it; the model on this Mac only if you choose Local.`
    : `${ASK_LEDE} A small language model runs on this Mac: nothing goes to the internet.`;
  $("#ask-model-label").textContent = on ? "Model on this Mac" : "Model";
  $("#ask-model-note").textContent = on
    ? "Answers offline, on Local, and for folders that do not allow cloud answers. Pick which installed model does it; the next local answer loads it. It reads only the few documents your search found."
    : "Runs offline. It reads only the few documents your search found.";
  if (!on) return;
  renderAskCloudHead();
  renderProviders();
}
/* the parts that follow the status (polled): online, route, next answer, masking */
function renderAskCloudHead() {
  if (!cloudApi()) return;
  const c = askCfg();
  const net = $("#ask-online");
  net.className = "net " + (c.online === false ? "off" : c.online ? "on" : "");
  net.hidden = c.online == null;                       // not checked (no key yet, or Local only)
  net.replaceChildren(el("i", { class: "dot", "aria-hidden": "true" }), c.online === false ? "Offline" : "Online");
  net.title = c.online === false ? "No internet connection: answers come from the model on this Mac." : c.online ? "This Mac is online: cloud models can answer." : "";
  $("#route-seg").replaceChildren(...ROUTES.map(([id, label, desc]) => el("button", {
    type: "button", role: "radio", "aria-checked": String(c.route === id),
    title: desc,
    onclick: () => { if (c.route !== id) saveAsk({ route: id }); },
  }, label)));
  $("#route-desc").textContent = (ROUTES.find((r) => r[0] === c.route) || ROUTES[0])[2]
    + (c.route === "local" ? ` Model on this Mac: ${localModelName()}.` : " Folders with Allow cloud answers off always use the local model.")
    + " In the Ask view, Cloud / Local under the question switches one chat.";
  const act = $("#cloud-active"), ap = activeProv();
  if (act) {
    fill(act, el("span", { class: "ca-lab" }, "Chosen for cloud answers"), el("b", { class: "mono" }, c.model || "no model"),
      ap ? el("span", { class: "ca-prov" }, ` · ${ap.label || ap.id}`) : null,
      trainsOnInputs(c.model, c.provider) ? [" ", trainBadge()] : null,
      ap && ap.status !== "ok" ? el("span", { class: "ca-prov" }, ap.status === "no_key" ? " · needs a key" : ` · ${(PROV_STATE[ap.status] || ["", ap.status])[1].toLowerCase()}`) : null);
  }
  $("#route-next").replaceChildren(...nextAnswerNodes(predictWhere(null, null)));
  const kept = state.collections.filter((x) => x.ask_cloud === false).map((x) => x.name);
  if (kept.length && c.route !== "local") $("#route-next").append(el("span", { class: "kept" }, `Kept on this Mac by their folder setting: ${kept.join(", ")}.`));
  const f = $("#route-form");
  if (!f.dataset.busy) { f.cloud_redact.checked = cloudRedact() !== false; f.local_fallback.checked = !!c.local_fallback; f.local_fallback.disabled = c.route === "local"; }
  renderModelMenus();
}

/* POST /api/settings {ask: patch}, shown at once and taken back if the service says no */
async function saveAsk(patch, out = $("#route-saved")) {
  const keys = Object.keys(patch);
  const before = Object.fromEntries(keys.map((k) => [k, prov.data?.[k]]));
  const beforeSt = Object.fromEntries(keys.map((k) => [k, state.status?.ask?.[k]]));
  for (const o of [prov.data, state.status?.ask]) if (o) Object.assign(o, patch);
  if (state.settings) state.settings.ask = { ...(state.settings.ask || {}), ...patch };
  renderAskCloudHead(); renderProviders();
  out.classList.remove("err"); out.textContent = "Saving…";
  try {
    await post("/api/settings", { ask: patch });
    out.textContent = "Saved.";
    setTimeout(() => { if (out.textContent === "Saved.") out.textContent = ""; }, 2500);
  } catch (e) {
    if (prov.data) Object.assign(prov.data, before);
    if (state.status?.ask) Object.assign(state.status.ask, beforeSt);
    out.textContent = cleanError(e.message); out.classList.add("err");
    toast(cleanError(e.message));
  }
  await poll(true);
  await loadProviders();
}
$("#route-form").addEventListener("submit", (e) => e.preventDefault());
$("#route-form").addEventListener("change", async (e) => {
  if (e.target.name !== "cloud_redact" && e.target.name !== "local_fallback") return;
  const f = $("#route-form");
  f.dataset.busy = "1";
  await saveAsk({ [e.target.name]: e.target.checked });
  delete f.dataset.busy;
});

/* ------------------------------------------------------------------ choosing a model
   A searchable picker: every model the provider lists (grouped where that helps: by id prefix, Claude by family),
   "Use a model id…" at the end. A model that the provider's training_models patterns match carries a warning badge and
   needs a one-time confirmation, stored as ask.allow_training_models. */
const TRAIN_TEXT = "The provider may use inputs for training — check the provider's terms";
const TRAIN_WHY = "This provider may use the inputs and outputs of this model for training. Ask would send your question, the top passages from your documents and the last turns of the chat. Folders with Allow cloud answers off are never sent.";
const providerRow = (id) => (prov.data?.providers || []).find((x) => x.id === id) || null;
/* a pattern of training_models: * = any characters, ? = one character, case-insensitive (config.trains_on_inputs) */
const globRe = (g) => new RegExp("^" + String(g).replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "is");
/* does the provider (an id or its row) mark this model as one its provider may train on? */
const trainsOnInputs = (model, provider) => {
  const id = String(model || ""), row = typeof provider === "string" ? providerRow(provider) : provider;
  return !!id && (row?.training_models || []).some((g) => globRe(g).test(id));
};
const trainAllowed = (id) => (prov.data?.allow_training_models || []).includes(id);
function fill(node, ...kids) {      // replaceChildren that also takes nested arrays, strings and nulls
  node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false).map((k) => (k.nodeType ? k : document.createTextNode(k))));
}
function trainBadge(text = TRAIN_TEXT) { return el("span", { class: "wb train", title: TRAIN_WHY }, "⚠ ", text); }
function fmtAt(at) {
  if (!at) return "";
  const d = new Date(at * 1000), t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? `today ${t}` : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${t}`;
}
const FAMILIES = [["fable", "Fable"], ["mythos", "Mythos"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"]];
function modelGroups(p, ids, byPrefix) {
  const of = (m) => {
    if (byPrefix) return m.includes("/") ? m.split("/")[0] + "/" : "other";
    if (p?.kind === "anthropic" && ids.length > 8) { const f = /^claude-([a-z]+)/.exec(m); return (f && FAMILIES.find(([k]) => k === f[1])?.[1]) || "Other"; }
    return "";
  };
  const groups = new Map();
  for (const m of ids) { const g = of(m); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(m); }
  if (!byPrefix && p?.kind === "anthropic" && groups.size > 1) {
    const order = [...FAMILIES.map((f) => f[1]), "Other"];
    return [...groups].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }
  return [...groups];
}
let closePicker = null;
function modelCombo({ p, models, info = {}, current, label, onPick, onCustom, placeholder, part = "model", disabled = false }) {
  const ids = [...new Set((models || []).filter(Boolean))];
  const byPrefix = ids.some((m) => m.includes("/")) && new Set(ids.map((m) => (m.includes("/") ? m.split("/")[0] : ""))).size > 1;
  const nameOf = (m) => info?.[m]?.name || "";
  const btn = el("button", { type: "button", class: "mc-btn", "data-part": part, "aria-haspopup": "listbox", "aria-expanded": "false", "aria-label": label || "Model",
    disabled: disabled || (!ids.length && !onCustom), title: current ? `${label || "Model"}: ${current}` : "" },
  el("span", { class: "mc-cur" }, current ? [el("span", { class: "mc-id" }, current), nameOf(current) ? el("span", { class: "mc-name" }, nameOf(current)) : null]
    : el("span", { class: "mc-ph" }, placeholder || "Choose a model")),
  trainsOnInputs(current, p) ? el("span", { class: "mc-warn", title: `${TRAIN_TEXT}. ${TRAIN_WHY}` }, "⚠") : null,
  el("i", { class: "mc-caret", "aria-hidden": "true" }));
  let pop = null, rows = [], act = -1;
  const away = (e) => { if (pop && !pop.contains(e.target) && !btn.contains(e.target)) close(false); };
  function close(focus) {
    if (!pop) return;
    pop.remove(); pop = null; closePicker = null;
    document.removeEventListener("mousedown", away, true); window.removeEventListener("resize", onResize);
    btn.setAttribute("aria-expanded", "false");
    if (focus) btn.focus({ preventScroll: true });
  }
  const onResize = () => close(false);
  function place() {
    const r = btn.getBoundingClientRect(), w = Math.min(Math.max(r.width, 420), innerWidth - 24);
    const below = innerHeight - r.bottom - 14, above = r.top - 14, up = below < 280 && above > below;
    pop.style.width = `${w}px`;
    pop.style.left = `${Math.max(12, Math.min(r.left, innerWidth - w - 12))}px`;
    pop.style.maxHeight = `${Math.max(200, Math.min(460, up ? above : below))}px`;
    if (up) { pop.style.bottom = `${innerHeight - r.top + 4}px`; pop.style.top = "auto"; } else { pop.style.top = `${r.bottom + 4}px`; pop.style.bottom = "auto"; }
  }
  function open() {
    if (pop) { close(true); return; }
    closePicker?.(false);
    const search = el("input", { type: "search", class: "mc-search", placeholder: `Search ${plural(ids.length, "model")}`, "aria-label": "Search models", autocomplete: "off", spellcheck: "false" });
    const list = el("div", { class: "mc-list", role: "listbox", "aria-label": label || "Models" });
    const count = el("span", { class: "mc-count" });
    pop = el("div", { class: "mc-pop", role: "dialog", "aria-label": label || "Choose a model" }, search, list,
      ids.some((m) => trainsOnInputs(m, p)) ? el("p", { class: "mc-legend" }, "⚠ ", TRAIN_TEXT, ". You confirm each marked model once.") : null,
      el("div", { class: "mc-foot" }, count, onCustom ? el("button", { type: "button", class: "linklike", onmousedown: (e) => e.preventDefault(), onclick: () => { close(false); onCustom(); } }, "Use a model id…") : null));
    document.body.append(pop);
    btn.setAttribute("aria-expanded", "true");
    closePicker = close;
    document.addEventListener("mousedown", away, true); window.addEventListener("resize", onResize);
    const pick = (m) => { close(true); if (m !== current) onPick(m); };
    const setAct = (i) => {
      act = Math.max(0, Math.min(rows.length - 1, i));
      rows.forEach((r, k) => r.classList.toggle("act", k === act));
      rows[act]?.scrollIntoView({ block: "nearest" });
    };
    function paint() {
      const terms = search.value.toLowerCase().split(/\s+/).filter(Boolean);
      const hit = ids.filter((m) => terms.every((t) => `${m} ${nameOf(m)}`.toLowerCase().includes(t)));
      const kids = [];
      rows = [];
      for (const [g, items] of modelGroups(p, hit, byPrefix)) {
        if (g) kids.push(el("div", { class: "mc-group", role: "presentation" }, g, el("span", {}, String(items.length))));
        for (const m of items) {
          const row = el("div", { class: "mc-opt" + (m === current ? " cur" : ""), role: "option", "aria-selected": String(m === current), "data-id": m,
            onmousedown: (e) => e.preventDefault(), onclick: () => pick(m) },
          el("span", { class: "mc-line" }, el("span", { class: "mc-id" }, m), nameOf(m) ? el("span", { class: "mc-name" }, nameOf(m)) : null, m === current ? el("span", { class: "mc-tick", "aria-hidden": "true" }, "✓") : null),
          trainsOnInputs(m, p) ? trainBadge() : null);
          rows.push(row); kids.push(row);
        }
      }
      list.replaceChildren(...(kids.length ? kids : [el("p", { class: "mc-none" }, ids.length ? "No model matches." : "No models listed yet.")]));
      count.textContent = ids.length ? `${hit.length} of ${plural(ids.length, "model")}` : "";
      const i = rows.findIndex((r) => r.dataset.id === current);
      setAct(terms.length ? 0 : Math.max(0, i));
    }
    search.addEventListener("input", paint);
    search.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setAct(act + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setAct(act - 1); }
      else if (e.key === "Enter") { e.preventDefault(); if (rows[act]) pick(rows[act].dataset.id); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === "Tab") close(false);
    });
    paint(); place(); search.focus();
    list.querySelector(".act")?.scrollIntoView({ block: "center" });
  }
  btn.addEventListener("click", open);
  btn.addEventListener("keydown", (e) => { if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !pop) { e.preventDefault(); open(); } });
  return el("div", { class: "mcombo" }, btn);
}

/* Use this provider and model for cloud answers; a model its provider may train on asks once first */
function confirmTraining(model) {
  return dialog({ title: `Use ${model}?`, confirm: "Allow and use",
    text: `${TRAIN_TEXT}. ${TRAIN_WHY}`,
    content: el("p", { class: "dlg-warn" }, trainBadge(), ` ${model} stays allowed until you remove ask.allow_training_models from config.json.`) }).then((f) => !!f);
}
async function activateModel(p, model, out) {
  const patch = { provider: p.id, model, route: "cloud" };       // choosing a cloud model means: cloud answers
  if (trainsOnInputs(model, p) && !trainAllowed(model)) {
    if (!(await confirmTraining(model))) return false;
    patch.allow_training_models = [...(prov.data?.allow_training_models || []), model];
  }
  await saveAsk(patch, out);
  return true;
}

/* the answer models installed on this Mac (GET /api/ask/local-models): nothing is loaded to list them */
const lm = { data: null, api: null, at: 0, loading: null };
async function loadLocalModels(force) {
  if (lm.loading) return lm.loading;
  if (!force && lm.api === false) return;
  if (!force && lm.data && Date.now() - lm.at < 30000) return;
  lm.loading = (async () => {
    const before = lm.data?.current;
    try { lm.data = await api("/api/ask/local-models"); lm.api = true; }
    catch (e) { if (e.status === 404) lm.api = false; }
    lm.at = Date.now(); lm.loading = null;
    renderLocalPicker(); renderModelMenus();
    if (!MINI && lm.data && before !== lm.data.current && state.mode === "ask" && !chat.ctrl) renderChat();
  })();
  return lm.loading;
}
function localRamHint() {       // loaded size ≈ 1.15 × the weights on disk, to the nearest half GB
  const m = (lm.data?.models || []).find((x) => x.current);
  return m ? `~${(Math.max(1, Math.round((m.size_mb * 1.15) / 512)) / 2).toFixed(1)} GB` : "~2.5 GB";
}
function renderLocalPicker() {
  const box = $("#ask-local-picker");
  if (!box) return;
  const d = lm.data, many = d && d.models.length > 0;
  box.hidden = !many; $("#ask-model").hidden = !!many;
  if (!many) return;
  const info = Object.fromEntries(d.models.map((m) => [m.name, { name: `${(m.size_mb / 1024).toFixed(1)} GB` }]));
  const open = !!document.querySelector(".mc-pop");
  if (open) return;
  box.replaceChildren(modelCombo({ models: d.models.map((m) => m.name), info, current: d.current, label: "Model on this Mac", part: "local-model", onPick: pickLocalModel }));
}
async function pickLocalModel(name) {
  try {
    await post("/api/settings", { ask: { local_model: name } });
    toast(`The model on this Mac is now ${name}. The next local answer loads it.`);
  } catch (e) { toast(cleanError(e.message)); }
  await loadLocalModels(true); await poll(true);
}
/* will the model on this Mac write the next answer once Ask is on? (status reports "local" while Ask is off, so work it out) */
function localWillAnswer() {
  const c = askCfg();
  if (c.route === "local") return true;
  if (!cloudApi()) return true;                                    // a service without cloud models
  return !!c.local_fallback && (!cloudReady() || c.online === false);
}

/* ------------------------------------------------------------------ one model menu, grouped by connected keys
   Next to the message box (Ask view) and at the top of Settings › Ask. It lists only providers that can answer (a key, a
   key file, or a server that needs none): Claude, custom providers (a provider whose ids carry a prefix such as
   deepseek/... by family), and last "On this Mac". Choosing a cloud model sets provider + model + route cloud in one step; choosing a
   local model sets route local + the local model. Nothing local is loaded by choosing or by browsing the menu. */
const FAMILY_NAMES = { deepseek: "DeepSeek", anthropic: "Claude", claude: "Claude", grok: "Grok", xai: "Grok", kimi: "Kimi", moonshot: "Kimi", qwen: "Qwen",
  openai: "GPT", gpt: "GPT", minimax: "MiniMax", glm: "GLM", zai: "GLM", zhipu: "GLM", mistral: "Mistral",
  gemini: "Gemini", google: "Gemini", llama: "Llama", cohere: "Cohere" };
const FAMILY_ORDER = ["GPT", "Claude", "DeepSeek", "Grok", "Kimi", "Qwen", "MiniMax", "GLM", "Gemini", "Mistral"];
function familyOf(id) {
  const pre = id.includes("/") ? id.split("/")[0] : "";
  // A suffix (-api, -plan, -oauth, -pro, -cli) is dropped from the prefix (or the bare id) before the family word is read:
  // 'example-api/x' is the family 'example'.
  const word = (pre || id).toLowerCase().replace(/[-_](api|plan|oauth|pro|cli)$/, "").split(/[-_.\d]/)[0];
  if (FAMILY_NAMES[word]) return FAMILY_NAMES[word];
  if (!pre && /^(o\d|chatgpt|codex)/i.test(id)) return "GPT";
  if (pre) return pre.replace(/[-_](api|plan|oauth)$/, "").replace(/^./, (c) => c.toUpperCase());
  return "Other";
}
const menuUsable = (p) => p.has_key === true || p.needs_key === false;
const rankFamily = (f) => { const i = FAMILY_ORDER.indexOf(f); return i < 0 ? 100 : i; };
/* [{head, note?, subs: [{sub|null, items: [{provider, model, local?, name?}]}]}] */
function menuSections() {
  const c = askCfg(), out = [];
  for (const p of prov.data?.providers || []) {
    if (!menuUsable(p)) continue;
    const ids = providerModels(p, c.provider === p.id && c.model ? [c.model] : []);
    if (!ids.length) continue;
    const label = p.label || BUILTIN_LABEL[p.id] || p.id;
    const items = ids.map((m) => ({ provider: p.id, model: m, name: p.model_info?.[m]?.name || "" }));
    let subs;
    if (ids.some((m) => m.includes("/"))) {
      const by = new Map();
      for (const it of items) { const f = familyOf(it.model); if (!by.has(f)) by.set(f, []); by.get(f).push(it); }
      subs = [...by].sort((a, b) => rankFamily(a[0]) - rankFamily(b[0]) || a[0].localeCompare(b[0])).map(([sub, its]) => ({ sub, items: its }));
    } else if (p.kind === "anthropic" && ids.length > 4) {
      subs = modelGroups(p, ids, false).map(([sub, ms]) => ({ sub, items: ms.map((m) => items.find((i) => i.model === m)) }));
    } else subs = [{ sub: null, items }];
    out.push({ head: label, status: p.status, subs });
  }
  const loc = lm.data?.models || [];
  if (loc.length) out.push({ head: "On this Mac", note: `local, loads ${localRamHint()} when used`, subs: [{ sub: null,
    items: loc.map((m) => ({ provider: "local", model: m.name, local: true, name: `${(m.size_mb / 1024).toFixed(1)} GB` })) }] });
  return out;
}
function menuSelection() {
  const c = askCfg();
  if (c.route === "local") return { local: true, provider: "local", model: lm.data?.current || localModelName() };
  const p = activeProv();
  if (!p || !menuUsable(p) || !c.model) return null;
  return { provider: p.id, model: c.model };
}
function menuLabel(sel) {
  if (!sel) return el("span", { class: "mc-ph" }, "Choose a model");
  return el("span", { class: "mc-cur" }, el("span", { class: "mc-id" }, sel.model), el("span", { class: "mc-name" }, "· ", sel.local ? "On this Mac" : providerLabel(sel.provider)),
    whereBadge(sel.local ? "local" : "cloud", sel.local ? "Answered on this Mac" : `${providerLabel(sel.provider)} · ${sel.model}`));
}
async function chooseEntry(it) {
  const out = $("#menu-saved") || document.createElement("span");
  if (it.local) {
    await saveAsk({ route: "local", local_model: it.model }, out);
    await loadLocalModels(true);
  } else {
    const p = (prov.data?.providers || []).find((x) => x.id === it.provider);
    if (p) await activateModel(p, it.model, out);
  }
  renderModelMenus();
}
function modelMenuButton() {
  const sel = menuSelection();
  const btn = el("button", { type: "button", class: "mc-btn am-btn" + (sel ? "" : " need"), "data-part": "ask-model-menu", "aria-haspopup": "listbox", "aria-expanded": "false",
    "aria-label": "Model that answers", title: sel ? `${sel.model} · ${sel.local ? "on this Mac" : providerLabel(sel.provider)}` : "Choose the model that writes the answers" },
  menuLabel(sel), trainsOnInputs(sel?.model, sel?.provider) ? el("span", { class: "mc-warn", title: `${TRAIN_TEXT}. ${TRAIN_WHY}` }, "⚠") : null, el("i", { class: "mc-caret", "aria-hidden": "true" }));
  let pop = null, rows = [], act = -1;
  const away = (e) => { if (pop && !pop.contains(e.target) && !btn.contains(e.target)) close(false); };
  const onResize = () => close(false);
  function close(focus) {
    if (!pop) return;
    pop.remove(); pop = null; closePicker = null;
    document.removeEventListener("mousedown", away, true); window.removeEventListener("resize", onResize);
    btn.setAttribute("aria-expanded", "false");
    if (focus) btn.focus({ preventScroll: true });
  }
  function place() {
    const r = btn.getBoundingClientRect(), w = Math.min(Math.max(r.width, 460), innerWidth - 24);
    const below = innerHeight - r.bottom - 14, above = r.top - 14, up = below < 320 && above > below;
    pop.style.width = `${w}px`;
    pop.style.left = `${Math.max(12, Math.min(r.left, innerWidth - w - 12))}px`;
    pop.style.maxHeight = `${Math.max(220, Math.min(520, up ? above : below))}px`;
    if (up) { pop.style.bottom = `${innerHeight - r.top + 4}px`; pop.style.top = "auto"; } else { pop.style.top = `${r.bottom + 4}px`; pop.style.bottom = "auto"; }
  }
  function open() {
    if (pop) { close(true); return; }
    closePicker?.(false);
    const sections = menuSections();
    const all = sections.flatMap((g) => g.subs.flatMap((x) => x.items));
    const search = el("input", { type: "search", class: "mc-search", placeholder: `Search ${plural(all.length, "model")}`, "aria-label": "Search models", autocomplete: "off", spellcheck: "false" });
    const list = el("div", { class: "mc-list", role: "listbox", "aria-label": "Models" });
    const count = el("span", { class: "mc-count" });
    pop = el("div", { class: "mc-pop am-pop", role: "dialog", "aria-label": "Choose a model" }, search, list,
      all.some((i) => trainsOnInputs(i.model, i.provider)) ? el("p", { class: "mc-legend" }, "⚠ ", TRAIN_TEXT, ". You confirm each marked model once.") : null,
      el("div", { class: "mc-foot" }, count, el("button", { type: "button", class: "linklike", onmousedown: (e) => e.preventDefault(), onclick: () => { close(false); showSettings("ask"); } }, "Keys and providers in Settings › Ask")));
    document.body.append(pop);
    btn.setAttribute("aria-expanded", "true");
    closePicker = close;
    document.addEventListener("mousedown", away, true); window.addEventListener("resize", onResize);
    const cur = menuSelection();
    const isCur = (it) => !!cur && cur.provider === it.provider && cur.model === it.model;
    const pick = (it) => { close(true); if (!isCur(it)) chooseEntry(it); };
    const setAct = (i) => {
      act = Math.max(0, Math.min(rows.length - 1, i));
      rows.forEach((r, k) => r.classList.toggle("act", k === act));
      rows[act]?.scrollIntoView({ block: "nearest" });
    };
    function paint() {
      const terms = search.value.toLowerCase().split(/\s+/).filter(Boolean);
      const kids = []; rows = []; let shown = 0;
      for (const g of sections) {
        const body = [];
        for (const sub of g.subs) {
          const hit = sub.items.filter((it) => terms.every((t) => `${it.model} ${it.name} ${g.head} ${sub.sub || ""}`.toLowerCase().includes(t)));
          if (!hit.length) continue;
          if (sub.sub) body.push(el("div", { class: "mc-sub", role: "presentation" }, sub.sub, el("span", {}, String(hit.length))));
          for (const it of hit) {
            const row = el("div", { class: "mc-opt" + (isCur(it) ? " cur" : ""), role: "option", "aria-selected": String(isCur(it)), onmousedown: (e) => e.preventDefault(), onclick: () => pick(it) },
              el("span", { class: "mc-line" }, el("span", { class: "mc-id" }, it.model), it.name ? el("span", { class: "mc-name" }, it.name) : null, isCur(it) ? el("span", { class: "mc-tick", "aria-hidden": "true" }, "✓") : null),
              trainsOnInputs(it.model, it.provider) ? trainBadge() : null);
            row._it = it; rows.push(row); body.push(row); shown++;
          }
        }
        if (!body.length) continue;
        kids.push(el("div", { class: "mc-group", role: "presentation" }, el("b", {}, g.head), g.note ? el("em", {}, g.note) : null,
          g.status === "offline" ? el("em", { class: "warn-t" }, "not reachable now") : null));
        kids.push(...body);
      }
      list.replaceChildren(...(kids.length ? kids : [el("p", { class: "mc-none" }, all.length ? "No model matches." : "No provider is connected yet: add a key in Settings › Ask.")]));
      count.textContent = all.length ? `${shown} of ${plural(all.length, "model")}` : "";
      const i = rows.findIndex((r) => isCur(r._it));
      setAct(terms.length ? 0 : Math.max(0, i));
    }
    search.addEventListener("input", paint);
    search.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setAct(act + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setAct(act - 1); }
      else if (e.key === "Enter") { e.preventDefault(); if (rows[act]) pick(rows[act]._it); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === "Tab") close(false);
    });
    paint(); place(); search.focus();
    list.querySelector(".act")?.scrollIntoView({ block: "center" });
  }
  btn.addEventListener("click", open);
  btn.addEventListener("keydown", (e) => { if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !pop) { e.preventDefault(); open(); } });
  return btn;
}
function renderModelMenus() {
  if (MINI || !$("#ask-menu")) return;
  const on = cloudApi() && !!state.status?.ask && "route" in state.status.ask;
  if ($("#ask-menu-sec")) $("#ask-menu-sec").hidden = !on;
  const quiet = $("#ask-quiet");
  quiet.hidden = !(on && !(prov.data?.providers || []).some(menuUsable));      // the one quiet line when nothing is connected
  $("#ask-menu").hidden = !on;
  if (!on || document.querySelector(".mc-pop")) return;
  const sel = menuSelection();
  for (const host of [$("#ask-menu"), $("#ask-menu-settings")]) host?.replaceChildren(modelMenuButton());
  const ms = $("#menu-sel");
  if (ms) ms.textContent = !sel ? "No model is chosen yet: open the menu and pick one. The model on this Mac is used only if you choose it."
    : sel.local ? `Answers are written on this Mac by ${sel.model}; it loads (about ${localRamHint().replace("~", "")}) when a question needs it.`
    : `Answers are written in the cloud by ${sel.model} (${providerLabel(sel.provider)}). The model on this Mac is not loaded.`;
}

/* the cards: one per provider; keys typed but not saved yet survive a redraw (in the page only) */
function renderProviders() {
  const box = $("#providers");
  if (!cloudApi()) { box.replaceChildren(); return; }
  closePicker?.(false);
  const typed = new Map($$(".prov", box).map((n) => [n.dataset.id, $(".key-input", n)?.value]).filter(([, v]) => v));
  const focus = document.activeElement?.closest?.(".prov") ? [document.activeElement.closest(".prov").dataset.id, document.activeElement.dataset.part] : null;
  const list = prov.data.providers || [];
  box.replaceChildren(...(list.length ? list.map(providerCard) : [el("p", { class: "empty-note" }, "No providers yet: add one.")]));
  for (const [id, v] of typed) { const i = $(`.prov[data-id="${CSS.escape(id)}"] .key-input`, box); if (i) i.value = v; }
  if (focus?.[1]) $(`.prov[data-id="${CSS.escape(focus[0])}"] [data-part="${focus[1]}"]`, box)?.focus({ preventScroll: true });
}
function refreshProv(id) {
  const n = $(`#providers .prov[data-id="${CSS.escape(id)}"]`), p = (prov.data?.providers || []).find((x) => x.id === id);
  if (!n || !p) return;
  closePicker?.(false);
  const typed = $(".key-input", n)?.value;
  const fresh = providerCard(p);
  n.replaceWith(fresh);
  if (typed && $(".key-input", fresh)) $(".key-input", fresh).value = typed;
}
function providerModels(p, extra) {
  return [...new Set([...(p.models || []), ...(p.custom_models || [])].map(modelId).concat(extra || []).filter(Boolean))];
}
function savePicks() { localSet("askPick", JSON.stringify(prov.pick)); }

function providerCard(p) {
  const c = askCfg(), active = c.provider === p.id;
  const [cls, label] = PROV_STATE[p.status] || ["", p.status || "Unknown"];
  const models = providerModels(p, active && c.model ? [c.model] : []);
  const cur = (active && c.model) || (models.includes(prov.pick[p.id]) ? prov.pick[p.id] : models[0]) || "";
  const name = p.label || BUILTIN_LABEL[p.id] || p.id;
  const hasKey = p.has_key || p.status === "ok";
  const picker = modelCombo({ p, models, info: p.model_info, current: cur, label: `Model for ${name}`, part: "model",
    placeholder: hasKey ? "No models listed yet" : "Save a key to list the models",
    onPick: (m) => {
      prov.pick[p.id] = m; savePicks(); delete prov.tests[p.id];
      if (active) activateModel(p, m); else refreshProv(p.id);
    },
    onCustom: () => addModelId(p) });
  const t = prov.tests[p.id];
  const test = el("button", { type: "button", class: "btn small", "data-part": "test", disabled: !!t?.running || !cur,
    title: cur ? `Send a one-line test question to ${cur}` : "Pick a model first", onclick: () => testProvider(p, cur) }, t?.running ? "Testing…" : "Test");
  const testOut = t && !t.running ? el("span", { class: "prov-test " + (t.ok ? "ok" : "err"), role: "status", title: t.error || "" },
    t.ok ? `✓ Answered in ${fmtMs(t.ms)}${t.model && t.model !== cur ? ` · ${t.model}` : ""}` : `✗ ${t.error || "No answer"}`) : null;
  const use = active
    ? el("span", { class: "pill in-use", title: c.route === "local" ? "Chosen for cloud answers; the route is Local only, so it is not used now"
        : p.status === "ok" ? "Cloud answers use this provider" : "Chosen for cloud answers; until it is ready, answers need another model (the local model is not used unless you allow it)" },
      c.route === "local" ? "Chosen (route is Local)" : p.status === "ok" ? "Answers with this" : "Chosen")
    : el("button", { type: "button", class: "btn small", "data-part": "use", disabled: !cur,
      title: cur ? `Cloud answers use ${name} · ${cur}` : "Pick or add a model first", onclick: () => activateModel(p, cur) }, "Use for answers");
  const n = (p.models || []).length, busy = prov.refreshing.has(p.id);
  const count = el("span", { class: "prov-count", role: "status" },
    busy ? "Refreshing…" : n ? `${plural(n, "model")} · ${p.models_at ? `fetched ${fmtAt(p.models_at)}` : "built-in list"}` : "No models listed");
  return el("article", { class: "prov" + (active ? " active" : "") + (hasKey ? " has-key" : ""), "data-id": p.id },
    el("header", { class: "prov-head" },
      el("div", { class: "prov-id" },
        el("h3", {}, name),
        el("span", { class: "prov-sub", title: p.base_url || "" }, [name.toLowerCase().includes((KIND_SHORT[p.kind] || p.kind || "").toLowerCase()) ? null : KIND_SHORT[p.kind] || p.kind, hostOf(p.base_url)].filter(Boolean).join(" · "))),
      el("span", { class: `pill ${cls}`, title: p.error || "" }, label),
      use),
    active && c.model ? el("p", { class: "prov-active" }, "Answering with ", el("b", { class: "mono" }, c.model), trainsOnInputs(c.model, p) ? [" ", trainBadge()] : null) : null,
    p.status === "error" && p.error ? el("p", { class: "prov-err" }, p.error) : null,
    el("div", { class: "prov-rows" },
      el("span", { class: "prov-lab" }, "Model"),
      el("div", { class: "prov-ctl" }, picker, test, testOut,
        el("button", { type: "button", class: "linklike", "data-part": "refresh", disabled: busy || (!hasKey && p.needs_key !== false),
          title: hasKey || p.needs_key === false ? "Ask the provider for every model it offers" : "Save a key first, then fetch the list", onclick: () => refreshModels(p) }, "Refresh list"),
        count),
      el("span", { class: "prov-lab" }, "Key"),
      el("div", { class: "prov-ctl" }, keyRow(p, name))),
    trainsOnInputs(cur, p) ? el("p", { class: "prov-train" }, trainBadge(),
      trainAllowed(cur) ? ` You have confirmed ${cur}.` : " Choosing this model asks you to confirm once.") : null,
    keyFileNote(p),
    p.builtin ? null : el("footer", { class: "prov-foot" },
      el("button", { type: "button", class: "linklike", onclick: () => providerDialog(p) }, "Edit…"),
      el("button", { type: "button", class: "linklike danger", onclick: () => removeProvider(p) }, "Remove…")));
}

function keyRow(p, name) {
  const replacing = prov.replacing.has(p.id);
  if (p.key_source === "file" && !replacing) return fileKeyRow(p, name);
  if (p.has_key && !replacing) return el("div", { class: "prov-key" },
    el("span", { class: "key-ok" }, el("i", { class: "dot", "aria-hidden": "true" }), "Key saved in Keychain"),
    el("button", { type: "button", class: "btn small", "data-part": "replace", onclick: () => {
      prov.replacing.add(p.id); refreshProv(p.id); $(`#providers .prov[data-id="${CSS.escape(p.id)}"] .key-input`)?.focus();
    } }, "Replace"),
    el("button", { type: "button", class: "btn small ghost", "data-part": "forget", onclick: () => forgetKey(p, name) }, "Forget"));
  return manualKeyRow(p, name, replacing);
}
/* a provider with a key file needs nothing from the user: the key is read from that file when a call needs it */
function fileKeyRow(p, name) {
  const off = p.status === "offline";
  return el("div", { class: "prov-key keyfile-key" },
    off ? el("span", { class: "pill warn keyfile-pill", "data-part": "keyfile-state" }, "Server isn't running")
      : el("span", { class: "pill ok keyfile-pill", "data-part": "keyfile-state", title: p.key_file || "" }, "Key read from its file"),
    off ? el("span", { class: "prov-hint" }, p.hint || "Start the server.") : null,
    el("details", { class: "alt keyfile-alt" }, el("summary", {}, "Use a different key…"),
      el("p", { class: "muted" }, "Only for an unusual setup. A key saved here is kept in the macOS Keychain and is used instead of the one in the key file."),
      manualKeyRow(p, name, false)));
}
function manualKeyRow(p, name, replacing) {
  const local = p.needs_key === false || /^(localhost|127\.0\.0\.1|\[::1\])(:|$)/.test(hostOf(p.base_url));
  const inp = el("input", { type: "password", class: "key-input", "data-part": "key", autocomplete: "off", spellcheck: "false", autocapitalize: "off",
    "aria-label": `API key for ${name}`,
    placeholder: p.id === "anthropic" ? "Paste your Anthropic API key" : local && !p.key_file ? "No key needed; add one if the server asks" : "Paste the API key",
    onkeydown: (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveKey(p, inp, name); }
      else if (e.key === "Escape" && replacing) { e.preventDefault(); e.stopPropagation(); prov.replacing.delete(p.id); refreshProv(p.id); }
    } });
  return el("div", { class: "prov-key" }, inp,
    el("button", { type: "button", class: "btn small primary", "data-part": "save-key", onclick: () => saveKey(p, inp, name) }, "Save key"),
    replacing ? el("button", { type: "button", class: "btn small ghost", onclick: () => { prov.replacing.delete(p.id); refreshProv(p.id); } }, "Cancel") : null);
}
function keyFileNote(p) {
  if (!p.key_file) return null;
  if (p.key_source === "file") return el("div", { class: "prov-note" },
    el("p", {}, "Nothing to set up: MySearch reads the key from ", el("code", {}, p.key_file),
      " when a call needs it. It is never copied into MySearch's settings or shown. ", el("b", {}, "Refresh list"), " fetches the models again."));
  if (p.has_key) return null;
  const m = prov.connectMsg;
  return el("div", { class: "prov-note" },
    el("div", { class: "connect-row" },
      el("button", { type: "button", class: "btn small primary", "data-part": "connect", disabled: prov.connecting, onclick: () => connectProvider(p) },
        prov.connecting ? "Connecting…" : "Connect from the key file"),
      m ? el("span", { class: "prov-test " + (m.ok ? "ok" : "err"), role: "status" }, m.text) : null),
    el("p", {}, "One click: MySearch reads the key from ", el("code", {}, p.key_file),
      ", stores it in the macOS Keychain (it is never shown) and lists the provider's models. The file must exist and hold the key on one line."),
    el("details", { class: "alt" }, el("summary", {}, "Add the key another way"),
      el("p", {}, "Paste the key above, or store it from a terminal, where typing stays hidden:"),
      snippet(`mse providers set-key ${p.id}`)));
}
async function connectProvider(p) {
  if (prov.connecting) return;
  prov.connecting = true; prov.connectMsg = null; cs.msg = null;
  refreshProv(p.id); if (!MINI) renderCloudSetup();
  try {
    const r = await post(`/api/ask/providers/${enc(p.id)}/connect`, {});
    prov.connectMsg = r.models_error
      ? { ok: false, text: `The key is stored, but the models could not be listed: ${cleanError(r.models_error)}` }
      : { ok: true, text: `Connected: ${plural(r.fetched, "model")} listed` };
    delete prov.tests[p.id];
  } catch (e) { prov.connectMsg = { ok: false, text: cleanError(e.message) }; }
  prov.connecting = false;
  cs.msg = prov.connectMsg ? { ok: prov.connectMsg.ok, text: prov.connectMsg.text } : null;
  await loadProviders(); poll(true);
}

async function saveKey(p, inp, name) {
  const key = inp.value.trim();
  if (!key) { inp.focus(); return; }
  inp.disabled = true;
  try {
    await post("/api/ask/providers", { id: p.id, key });
    inp.value = "";
    prov.replacing.delete(p.id); delete prov.tests[p.id];
    toast(`The key for ${name} is saved in the Keychain`);
    await loadProviders(); poll(true);
  } catch (e) { inp.disabled = false; inp.focus(); toast(cleanError(e.message)); }
}
async function forgetKey(p, name) {
  const ok = await dialog({ title: `Forget the key for ${name}?`, confirm: "Forget key", danger: true,
    text: `It is deleted from the Keychain. ${name} cannot answer until you save a key again; questions then get a message instead of an answer (the local model is used only if you allowed it).` });
  if (!ok) return;
  try { await post("/api/ask/providers", { id: p.id, forget_key: true }); toast(`Forgot the key for ${name}`); } catch (e) { toast(cleanError(e.message)); }
  delete prov.tests[p.id];
  await loadProviders(); poll(true);
}
async function testProvider(p, model) {
  prov.tests[p.id] = { running: true };
  refreshProv(p.id);
  try {
    const r = await post(`/api/ask/providers/${enc(p.id)}/test`, model ? { model } : {});
    prov.tests[p.id] = { ok: !!r.ok, ms: r.ms, model: r.model, error: r.error ? cleanError(r.error) : null };
  } catch (e) { prov.tests[p.id] = { ok: false, error: cleanError(e.message) }; }
  refreshProv(p.id);
}
async function refreshModels(p) {
  if (prov.refreshing.has(p.id)) return;
  prov.refreshing.add(p.id); refreshProv(p.id); if (!MINI) renderCloudSetup();
  try {
    const r = await post(`/api/ask/providers/${enc(p.id)}/models`, {});
    const n = Array.isArray(r.models) ? r.models.length : r.fetched;
    toast(n == null ? `Asked ${p.label || p.id} for its models` : `${p.label || p.id} lists ${plural(n, "model")}`);
  } catch (e) { toast(cleanError(e.message)); }
  prov.refreshing.delete(p.id);
  await loadProviders();
}
async function addModelId(p) {
  const name = p.label || p.id;
  const input = el("input", { type: "text", name: "model", spellcheck: "false", autocomplete: "off", class: "mono", placeholder: p.kind === "anthropic" ? "e.g. claude-haiku-5-5" : "e.g. deepseek-chat" });
  const f = await dialog({ title: `Add a model id to ${name}`, confirm: "Add model",
    text: "The exact id the provider uses for the model. It is added to this provider's list.",
    content: el("div", { class: "dlg-fields" }, el("label", {}, el("span", {}, "Model id"), input)) });
  const id = f && input.value.trim();
  if (!id) return;
  const base = (p.custom_models ?? p.models ?? []).map(modelId);
  try {
    await post("/api/ask/providers", { id: p.id, models: [...new Set([...base, id])] });
    prov.pick[p.id] = id; savePicks(); delete prov.tests[p.id];
    toast(`Added ${id} to ${name}`);
    if (askCfg().provider === p.id) await activateModel(p, id);
    else await loadProviders();
  } catch (e) { toast(cleanError(e.message)); }
}
async function removeProvider(p) {
  const name = p.label || p.id;
  const f = await dialog({ title: `Remove ${name}?`, confirm: "Remove", danger: true,
    text: askCfg().provider === p.id ? "Cloud answers use it now. After removing it, choose another provider, or the local model answers." : "It goes from this list. You can add it again later.",
    content: el("div", { class: "dlg-fields" }, el("label", { class: "check-line" }, el("input", { type: "checkbox", name: "forget", checked: true }), "Also delete its key from the Keychain")) });
  if (!f) return;
  try { await post(`/api/ask/providers/${enc(p.id)}/remove`, { forget_key: !!f.forget?.checked }); toast(`Removed ${name}`); } catch (e) { toast(cleanError(e.message)); }
  delete prov.tests[p.id]; delete prov.pick[p.id]; savePicks();
  await loadProviders(); poll(true);
}

/* Add provider… / Edit…: any OpenAI-compatible service (kind openai-chat unless chosen otherwise) */
function slugId(s) { return String(s).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "provider"; }
async function providerDialog(p) {
  let v = p ? { label: p.label || p.id, base_url: p.base_url || "", kind: p.kind || "openai-chat", models: (p.custom_models ?? p.models ?? []).map(modelId).join(", "), key: "",
      key_file: p.key_file || "", training_models: (p.training_models || []).join(", ") }
    : { label: "", base_url: "", kind: "openai-chat", models: "", key: "", key_file: "", training_models: "" };
  for (;;) {
    const name = el("input", { type: "text", name: "label", value: v.label, placeholder: "e.g. DeepSeek", spellcheck: "false", autocomplete: "off" });
    const url = el("input", { type: "text", name: "base_url", value: v.base_url, placeholder: "https://api.deepseek.com/v1", spellcheck: "false", autocomplete: "off", class: "mono", inputmode: "url" });
    const key = p ? null : el("input", { type: "password", name: "key", autocomplete: "off", spellcheck: "false", autocapitalize: "off", placeholder: "Paste the API key (none for a server on this Mac)" });
    if (key) key.value = v.key;
    const models = el("input", { type: "text", name: "models", value: v.models, placeholder: "e.g. deepseek-chat, deepseek-reasoner", spellcheck: "false", autocomplete: "off", class: "mono" });
    const kind = el("select", { name: "kind" }, Object.entries(KIND_NAME).map(([k, l]) => el("option", { value: k, selected: k === v.kind }, l)));
    const keyFile = el("input", { type: "text", name: "key_file", value: v.key_file, placeholder: "e.g. ~/.config/myrouter/key", spellcheck: "false", autocomplete: "off", class: "mono" });
    const training = el("input", { type: "text", name: "training_models", value: v.training_models, placeholder: "e.g. *-train", spellcheck: "false", autocomplete: "off", class: "mono" });
    const presets = p ? null : el("div", { class: "presets" }, el("span", {}, "Fill in"),
      PRESETS.map((x) => el("button", { type: "button", class: "chip", onclick: () => {
        name.value = x.label; url.value = x.base_url; models.value = x.models; kind.value = "openai-chat";
        key.placeholder = x.local ? "No key needed for a server on this Mac" : "Paste the API key";
        (x.local ? models : key).focus();
      } }, x.label)));
    const f = await dialog({ title: p ? `Edit ${p.label || p.id}` : "Add a provider", confirm: p ? "Save" : "Add provider",
      text: p ? "To change the key, use Replace on the card." : "Any service that speaks the OpenAI chat API, in the cloud or on this Mac. The key goes to the macOS Keychain.",
      content: el("div", { class: "dlg-fields" }, presets,
        el("label", {}, el("span", {}, "Name"), name),
        el("label", {}, el("span", {}, "Base URL ", el("small", {}, "the address the API answers at, usually ending in /v1")), url),
        key ? el("label", {}, el("span", {}, "API key ", el("small", {}, "stored in the Keychain, never shown again")), key) : null,
        el("label", {}, el("span", {}, "Model ids ", el("small", {}, "comma-separated; Refresh list on the card fetches the rest")), models),
        el("label", {}, el("span", {}, "Kind"), kind),
        el("label", {}, el("span", {}, "Key file ", el("small", {}, "optional: a file that holds the key; it is read when a call needs it")), keyFile),
        el("label", {}, el("span", {}, "Training warning ", el("small", {}, "optional: model ids (patterns with * and ?) whose provider may use inputs for training")), training)) });
    if (!f) return;
    v = { label: name.value.trim(), base_url: url.value.trim(), kind: kind.value, models: models.value, key: key?.value.trim() || "",
      key_file: keyFile.value.trim(), training_models: training.value };
    const err = !v.label ? "Give the provider a name."
      : !/^https?:\/\/[^\s/]+/i.test(v.base_url) ? "The base URL starts with https:// (or http:// for a server on this Mac)." : null;
    if (err) { toast(err); continue; }
    const ids = new Set((prov.data?.providers || []).map((x) => x.id));
    let id = p?.id || slugId(v.label);
    if (!p) for (let i = 2; ids.has(id); i++) id = `${slugId(v.label).slice(0, 29)}-${i}`;
    try {
      const hadKey = !!v.key;
      await post("/api/ask/providers", { id, label: v.label, kind: v.kind, base_url: v.base_url.replace(/\/+$/, ""),
        models: v.models.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean), key_file: v.key_file,
        training_models: v.training_models.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean), ...(hadKey ? { key: v.key } : {}) });
      v.key = "";
      toast(p ? `Saved ${v.label}` : `Added ${v.label}${hadKey ? "; its key is in the Keychain" : ""}`);
      await loadProviders(); poll(true);
      return;
    } catch (e) { toast(cleanError(e.message)); }
  }
}
$("#btn-add-provider").addEventListener("click", () => providerDialog(null));

/* Settings › Privacy: honest about the one thing that can leave the Mac */
const PRIVACY_LEDE = "Everything stays on this Mac: indexes, models and searches. Nothing is uploaded, and searches are not logged.";
function renderPrivacyLede() {
  const n = $("#privacy-lede");
  if (!n) return;
  const c = askCfg();
  const cloud = cloudApi() && c.route !== "local" && (prov.data.providers || []).some((p) => p.has_key || p.status === "ok");
  n.textContent = cloud
    ? "Indexes, models and searches stay on this Mac, and searches are not logged. The one exception is a cloud answer in Ask: it sends your question, the top passages and the last turns of the chat to the model chosen in Settings › Ask."
    : PRIVACY_LEDE;
}

/* ------------------------------------------------------------------ the Mac app (app/AppDelegate.swift)
   The app calls appInfo() with its shortcuts and login-item state; Settings › Mac app talks back through
   window.webkit.messageHandlers.app. The menu-bar item calls appStatusLine() and syncAll(). */

const appBridge = window.webkit?.messageHandlers?.app;
let appState = null, rec = null;   // rec: the shortcut being recorded, {onKey, saving}
function tellApp(msg) { try { appBridge?.postMessage(msg); } catch { /* not in the app */ } }

function appInfo(info) {
  appState = info;
  $('#set-nav [data-sec="app"]').hidden = false;
  $$(".quick-key").forEach((n) => (n.textContent = info.quickKey || "—"));
  $("#app-main-key").textContent = info.mainKey || "⌃⌥Space";
  const f = $("#app-form");
  f.login.checked = info.login === "on" || info.login === "approval";
  f.login.disabled = info.login === "unavailable";
  $("#app-login-note").textContent = {
    on: "MySearch opens when you log in. The search service starts on its own either way.",
    off: "Off. The search service still starts at login; only this window and the menu-bar icon wait for you.",
    approval: "Waiting for your approval in System Settings › General › Login Items.",
    unavailable: "macOS cannot register this copy of the app as a login item.",
  }[info.login] || "";
  const out = $("#app-key-out");
  if (info.error) { out.textContent = info.error; out.classList.add("err"); if (rec) rec.saving = false; }
  else if (info.message) { out.textContent = info.message; out.classList.remove("err"); stopRecording(); }
  return true;
}
function startRecording() {
  if (rec) return;
  tellApp({ type: "pauseHotkeys" });
  const b = $("#app-key-change"), out = $("#app-key-out");
  b.textContent = "Type the new shortcut…"; b.classList.add("recording");
  out.textContent = "Hold ⌘, ⌃ or ⌥ and press a key. Esc cancels."; out.classList.remove("err");
  rec = { saving: false, onKey: (e) => {
    if (["Meta", "Control", "Alt", "Shift", "CapsLock", "Fn"].includes(e.key) || rec.saving) return;
    e.preventDefault(); e.stopPropagation();
    if (e.key === "Escape" && !e.metaKey && !e.ctrlKey && !e.altKey) { stopRecording(); tellApp({ type: "resumeHotkeys" }); out.textContent = ""; return; }
    if (!/^F\d{1,2}$/.test(e.code) && !(e.metaKey || e.ctrlKey || e.altKey)) {
      out.textContent = "Add ⌘, ⌃ or ⌥, so the shortcut does not take over normal typing."; out.classList.add("err"); return;
    }
    rec.saving = true;
    tellApp({ type: "setQuickKey", code: e.code, cmd: e.metaKey, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey });
  } };
  document.addEventListener("keydown", rec.onKey, true);
}
function stopRecording() {
  if (rec) document.removeEventListener("keydown", rec.onKey, true);
  rec = null;
  const b = $("#app-key-change"); b.textContent = "Change…"; b.classList.remove("recording");
}
$("#app-key-change").addEventListener("click", startRecording);
$("#app-key-reset").addEventListener("click", () => { stopRecording(); tellApp({ type: "setQuickKey", reset: true }); });
$("#app-form").addEventListener("change", (e) => {
  if (e.target.name === "login") tellApp({ type: "setLogin", enabled: e.target.checked });
});

/* one line for the menu-bar item: what the indexer is doing */
function appStatusLine() {
  const st = state.status;
  if (!st) return "Search service not running";
  const cur = st.jobs?.current, q = st.jobs?.queued?.length || 0;
  const files = state.collections.reduce((n, c) => n + (c.stats?.files || 0), 0);
  let line = cur ? `${KIND[cur.kind] || cur.kind} ${cur.collection}${cur.total ? ` · ${cur.done.toLocaleString()} of ${cur.total.toLocaleString()}` : ""}` : `Idle · ${files.toLocaleString()} files`;
  if (q) line += ` · ${q} queued`;
  return line;
}

/* Tab or ⌘O in the Quick Search panel: show this search here, with the same result selected */
function openFromMini(o) {
  if (o.ask && localGet("askSetup") === "1") {     // Quick Search, ⌥↵ while Ask is off or has no key: open the Ask view with the key card
    localSet("askSetup", "");
    closeMenu();
    if (!$("#help").hidden) hideHelp();
    setMode("ask");
    renderChat();
    $("#ask-q").focus();
    return true;
  }
  if (o.ask) {
    closeMenu();
    if (!$("#help").hidden) hideHelp();
    const sc = validScope(o.scope);
    if (sc && sc !== state.scope) { state.scope = sc; localSet("scope", sc); renderScopes(); }
    const id = localGet("chat.handoff");
    localSet("chat.handoff", "");
    chat.threads = loadChats().filter((t) => !chat.removed.has(t.id));
    if (id && chat.threads.some((t) => t.id === id)) { setMode("ask"); openChat(id); }
    else askInChat(String(o.q || "").slice(0, 500));
    return true;
  }
  setMode("search", false);
  openQuery({ q: String(o.q || "").slice(0, 500), c: o.scope, select: o.path ? { collection: o.collection, path: o.path } : null });
  return true;
}

/* Links: mysearch://search?q=…&c=…&doc=…&page=… (handled by the app) and http://127.0.0.1:<port>/?q=…&c=…&doc=…&page=….
   Only these four values are read, each is checked, and nothing else in a link is ever acted on. */
function linkParams(search) {
  const p = new URLSearchParams(search || "");
  const c = p.get("c") || "", doc = (p.get("doc") || "").trim(), page = p.get("page") || "";
  return {
    q: (p.get("q") || "").slice(0, 500),
    c: /^[\w\-,]{1,400}$/.test(c) ? c : "",
    doc: /^#?[0-9a-f]{6,16}$/i.test(doc) ? doc.replace(/^#/, "").toLowerCase() : "",
    page: /^\d{1,6}$/.test(page) && +page > 0 ? +page : null,
  };
}
/* a scope from outside counts only if every part is a folder or group we know */
function validScope(c) {
  if (!c) return null;
  if (c === "all") return "all";
  const known = new Set([...state.collections.map((x) => x.name), ...(state.groups || []).map((g) => g.name)]);
  const parts = [...new Set(String(c).split(",").filter(Boolean))];
  return parts.length && parts.every((x) => known.has(x)) ? parts.join(",") : null;
}
function openLink(search) { setMode("search", false); openQuery(linkParams(search)); return true; }
function openQuery(o) {
  closeMenu();
  if (!$("#help").hidden) hideHelp();
  showSearch();
  const sc = validScope(o.c);
  if (sc && sc !== state.scope) { state.scope = sc; localSet("scope", sc); renderScopes(); }
  state.imageQuery = null;
  $("#q").placeholder = "Search for a name, a number, a phrase or a question";
  $("#q").value = o.q || "";
  qField?.update();
  state.pendingSelect = o.select || (o.doc ? { docid: o.doc, page: o.page } : null);
  if (o.q) runSearch();
  else { clearResults(); if (o.doc) openDoc(o.doc, o.page); }
}
/* show one document in the preview, by docid, at a page */
async function openDoc(docid, page) {
  try {
    const d = await api(`/api/doc?ref=${enc("#" + docid)}&max=1`);
    const r = { collection: d.collection, path: d.path, abs_path: d.abs_path, name: d.name, ext: d.ext, kind: d.kind, title: d.title,
      docid: d.docid, date: d.doc_date, size: d.size, pages: d.pages, duration: d.duration, lang: d.lang, metadata_only: d.metadata_only,
      context: d.context, why: [], copies: [], thumb: false,
      hits: page ? [{ page, t0: null, t1: null, modality: "text", why: ["link"], similarity: null, snippet: null }] : [] };
    markSelected(-1);
    renderPreview(r);
    if (NARROW.matches) $("#panes").classList.add("show-preview");
  } catch (e) { toast(`That document is not in the index: ${e.message}`); }
}

/* ------------------------------------------------------------------ status polling */

let pollTimer = null;
const STAGES = { extract: "Reading files", "ocr & media": "OCR, page images, audio & video", "ocr & pages": "OCR, page images & photos", "audio & video": "Transcribing audio & video", "embedding text": "Embedding text", "embedding media": "Embedding images, pages & media", vocabulary: "Building translation vocabulary" };
const KIND = { scan: "Checking for changes", sync: "Syncing", embed: "Embedding", vocab: "Vocabulary", file: "Re-indexing a file" };

async function poll(soon) {
  clearTimeout(pollTimer);
  let st = null;
  try { st = await api("/api/status"); } catch { /* down */ }
  state.status = st;
  if (st && st.pid !== fb.pid) { fb.pid = st.pid; probeFeedback(); }   // a (re)started service may have learned favourites
  renderStatus(st);
  renderAskSettings();
  if (!ask.q) renderAsk();
  const askKey = JSON.stringify([!!st, st?.ask?.available, st?.ask?.enabled, st?.ask?.route, st?.ask?.where_next, st?.ask?.provider, st?.ask?.model, st?.ask?.online, st?.ask?.local_model, st?.ask?.training, st?.ask?.local_fallback, st?.ask?.next_problem]);
  if (!MINI && askKey !== chat.askKey) { chat.askKey = askKey; if (state.mode === "ask" && !chat.ctrl) renderChat(); }
  if (!$("#view-settings").hidden) {
    renderActivity(st);
    if (state.section === "about") renderAbout();
  }
  const busy = st?.jobs?.current || st?.jobs?.queued?.length;
  if ((state.busy && !busy) || (busy && Date.now() - (state.statsAt || 0) > 20000)) { state.statsAt = Date.now(); loadCollections(); }
  state.busy = !!busy;
  pollTimer = setTimeout(poll, busy || soon ? 2000 : 8000);
}

function maybeReload(st) {
  /* the interface was updated: reload now if nothing is being typed or edited, otherwise on the next idle poll */
  if (!st?.ui_version || !UI_VERSION || st.ui_version === UI_VERSION) return;
  const a = document.activeElement;
  const editing = a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) && a.id !== "q" || $("#q").value.trim();
  if (!editing) location.reload();
}

function renderStatus(st) {
  maybeReload(st);
  const s = $("#status");
  if (!st) { s.innerHTML = '<span><i class="dot down"></i>Search service not running — start it with <b>mse service install</b></span>'; return; }
  const cur = st.jobs.current;
  const files = state.collections.reduce((n, c) => n + (c.stats?.files || 0), 0);
  const pending = state.collections.reduce((n, c) => n + (c.stats?.embed_pending || 0) + (c.stats?.heavy_pending || 0), 0);
  const model = Object.values(st.workers).some((w) => w.running)
    ? `model loaded ${Object.values(st.workers).reduce((n, w) => n + (w.rss_mb || 0), 0).toFixed(0)} MB` : "model unloaded";
  s.replaceChildren(...[
    el("span", {}, el("i", { class: "dot" + (cur ? " busy" : "") }),
      cur ? `${KIND[cur.kind] || cur.kind} ${cur.collection}: ${STAGES[cur.stage] || cur.stage || "starting"} ${cur.total ? `${cur.done}/${cur.total}` : ""}` : "Idle"),
    el("span", {}, `${files.toLocaleString()} files`),
    pending ? el("span", { class: "hide-narrow" }, `${pending.toLocaleString()} waiting for the night run`) : null,
    el("span", { class: "hide-narrow" }, `service ${st.daemon_rss_mb} MB · ${model}`),
    el("span", { class: "right" }, `night run ${st.jobs.nightly.enabled ? st.jobs.nightly.at : "off"}${st.jobs.nightly.last_run ? ` · last ${st.jobs.nightly.last_run}` : ""}`),
  ].filter(Boolean));
}

function renderActivity(st) {
  const box = $("#activity");
  box.replaceChildren();
  if (!st) return;
  const cur = st.jobs.current;
  const panel = el("div", { class: "panel" });
  if (cur) {
    const pct = cur.total ? Math.round((100 * cur.done) / cur.total) : 0;
    panel.append(el("div", { class: "job" },
      el("span", { class: "what" }, `${KIND[cur.kind] || cur.kind} ${cur.collection} — ${STAGES[cur.stage] || cur.stage || "starting"}`),
      el("button", { class: "btn small", onclick: async () => { await post("/api/cancel"); poll(true); } }, "Stop"),
      el("span", { class: "detail" }, cur.total ? `${cur.done.toLocaleString()} of ${cur.total.toLocaleString()}${cur.message ? " · " + cur.message : ""}` : ""),
      el("div", { class: "bar" }, el("span", { style: `width:${pct}%` }))));
  }
  for (const j of st.jobs.queued) panel.append(el("div", { class: "job" }, el("span", { class: "detail" }, `Next: ${KIND[j.kind] || j.kind} ${j.collection}`)));
  const last = st.jobs.recent[0];
  if (panel.childElementCount) box.append(panel);
  else if (last) box.append(el("p", { class: "last" },
    `Last: ${KIND[last.kind] || last.kind} ${last.collection} — ${last.status}${last.error ? ": " + last.error : ""}${last.finished_at ? " · " + ago(last.finished_at) : ""}`));
  const eng = $("#engine");
  const W = st.workers, idle = st.idle_unload_min;
  const model = (w, onDemand) => w?.running
    ? `In use · ${Math.round(w.rss_mb || 0)} MB · frees itself after ${idle} min idle`
    : onDemand;
  eng.replaceChildren(
    el("div", {}, el("b", {}, "Search model (text)"), model(W.text, "Sleeping · wakes in about 1 s when you search")),
    el("div", {}, el("b", {}, "Image, page, audio & video model"), model(W.full, "Sleeping · wakes for new photos, scans and media, Sync now and the night run")),
    el("div", {}, el("b", {}, "Speech to text (Whisper)"), st.asr_present ? model(W.asr, "Sleeping · wakes for new recordings") : "Not installed"),
    el("div", {}, el("b", {}, "Re-ranker"), st.rerank_enabled ? model(W.rerank, "On for close calls · wakes when the top results are near-ties") : "Off"),
    el("div", {}, el("b", {}, "Service"), `${st.daemon_rss_mb} MB · running ${Math.round(st.uptime_s / 60)} min`),
    el("div", {}, el("b", {}, "Live updates"), st.jobs.watcher?.running ? `On · ${st.jobs.watcher.events} file changes seen` : "Off — checking every hour instead"));
  // keep the folder pills honest while a job runs
  for (const node of $$(".coll")) if (node._c) { const [cls, label] = collState(node._c); const p = $(".coll-state", node); p.className = "pill coll-state " + cls; p.textContent = label; }
}

/* ------------------------------------------------------------------ wiring */

$("#q").addEventListener("input", () => {
  if (state.imageQuery) { state.imageQuery = null; $("#q").placeholder = "Search for a name, a number, a phrase or a question"; }
  scheduleSearch();
});
if (!MINI) qField = shortcutField({ input: $("#q"), mirror: $("#q-mirror"), chip: $("#q-chip"), box: $("#q-suggest"), tabs: () => $("#scopes") });
for (const s of ["#f-type", "#f-date", "#f-mode"]) $(s).addEventListener("change", runSearch);
for (const s of ["#f-include", "#f-exclude"]) {
  $(s).value = localGet(s.slice(3)) || "";
  $(s).addEventListener("input", () => { localSet(s.slice(3), $(s).value); scheduleSearch(); });
}
$("#btn-settings").addEventListener("click", () => ($("#view-settings").hidden ? showSettings() : showSearch()));
$("#btn-help").addEventListener("click", showHelp);
$("#btn-back").addEventListener("click", showSearch);
$("#btn-pin").addEventListener("click", togglePin);
$("#btn-add").addEventListener("click", addFolder);
async function syncAll() {
  try { await post("/api/sync", { kind: "sync" }); toast("Syncing all folders now"); poll(true); return "ok"; } catch (e) { toast(e.message); return e.message; }
}
$("#btn-sync-all").addEventListener("click", syncAll);

document.addEventListener("keydown", (e) => {
  if (MINI || $(".overlay.dlg")) return;   // mini.js has its own keys; dialogs handle theirs
  const meta = e.metaKey || e.ctrlKey;
  const k = e.key.toLowerCase();
  if (!$("#help").hidden) {
    if (e.key === "Escape" || e.key === "?") { e.preventDefault(); hideHelp(); }
    else if (e.key === "Tab") { e.preventDefault(); $("#help [data-close]").focus(); }   // keep focus in the dialog
    return;
  }
  if (meta && k === "j") { e.preventDefault(); setMode(state.mode === "ask" ? "search" : "ask"); return; }
  if (meta && k === "k") { e.preventDefault(); setMode("search", false); showSearch(); $("#q").select(); return; }
  if (meta && e.key === ",") { e.preventDefault(); showSettings(); return; }
  const a = document.activeElement;
  const typing = a && (["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) || a.isContentEditable);
  if (e.key === "?" && !meta && (!typing || (a.id === "q" && !a.value))) { e.preventDefault(); showHelp(); return; }
  if (!$("#view-settings").hidden) {
    if (e.key === "Escape" && !typing) { e.preventDefault(); showSearch(); }
    return;
  }
  if (state.mode === "ask" && e.key === "Escape" && chat.ctrl) { e.preventDefault(); stopChat(); return; }
  const inField = typing && a.id !== "q";
  if (inField) return;
  if (state.mode === "ask") {
    if (e.key === "Escape" && chat.ctrl) { e.preventDefault(); stopChat(); }
    else if (e.key === "/" || (e.key.length === 1 && !meta && !e.altKey)) { $("#ask-q").focus(); }
    return;
  }
  if (e.altKey && e.key === "Enter" && state.status?.ask?.available) { e.preventDefault(); askInChat($("#q").value.trim()); }
  else if (meta && k === "d") { e.preventDefault(); toggleBest(state.results[state.sel]); }
  else if (e.key === "ArrowDown") { e.preventDefault(); select(state.sel + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); select(state.sel - 1); }
  else if (e.key === "Enter" && state.results[state.sel]) { e.preventDefault(); openFile(state.results[state.sel], meta); }
  else if (e.key === "Escape") {
    if (ask.ctrl) stopAsk();
    else if ($("#panes").classList.contains("show-preview")) $("#panes").classList.remove("show-preview");
    else if ($("#q").value) { $("#q").value = ""; qField?.update(); clearResults(); }
  }
  else if (e.key === "/" && a.id !== "q") { e.preventDefault(); $("#q").focus(); }
});

if (!MINI) {
  showEmpty();
  $("#preview").replaceChildren(previewEmpty("Select a result to preview it here."));
  if (location.pathname.startsWith("/settings")) showSettings(location.pathname.split("/")[2]);
  setMode(localGet("mode") === "ask" ? "ask" : "search", false, true);      // the last view, without leaving Settings
  loadCollections().then(() => {
    poll();
    const link = linkParams(location.search);
    if (link.q || link.doc) openQuery(link);
    if ($("#view-settings").hidden) (state.mode === "ask" ? $("#ask-q") : $("#q")).focus();
    reportLayout();
  });
}
