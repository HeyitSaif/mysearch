"use strict";
/* MySearch Quick Search: the page inside MySearch.app's floating panel (/?mini=1).
   It runs on top of app.js (same page, same helpers and globals) and talks to the app through
   window.webkit.messageHandlers.mini: size, hide, copy, quicklook, openInApp. In a plain browser those
   messages are skipped, so the page can be previewed and tested at http://127.0.0.1:<port>/?mini=1. */

const miniBridge = window.webkit?.messageHandlers?.mini;
const M = { results: [], sel: 0, q: "", res: null, req: null, timer: 0, mode: "results", scope: localGet("scope.mini") || "all", statusTimer: 0, qlOpen: false, sizeRaf: 0 };
const KIND_ICON = {
  pdf: '<svg viewBox="0 0 24 24"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 15.5h5"/></svg>',
  office: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16M4 14.5h16M10 5v14"/></svg>',
  text: '<svg viewBox="0 0 24 24"><path d="M6 5h12M6 9.5h12M6 14h12M6 18.5h7"/></svg>',
  email: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="m4 7 8 6 8-6"/></svg>',
  image: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5.5-5 4 3.5 2.5-2 4 3.5"/></svg>',
  audio: '<svg viewBox="0 0 24 24"><path d="M5 10v4M8.5 7v10M12 4v16M15.5 8v8M19 11v2"/></svg>',
  video: '<svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="m10.5 9.5 4 2.5-4 2.5z"/></svg>',
  other: '<svg viewBox="0 0 24 24"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/></svg>',
};

function tell(msg) {
  if (!miniBridge) return false;
  try { miniBridge.postMessage(msg); return true; } catch { return false; }
}
function setStatus(text, sticky) {
  $("#m-status").textContent = text || "";
  clearTimeout(M.statusTimer);
  if (!sticky && text) M.statusTimer = setTimeout(() => { $("#m-status").textContent = M.base || ""; }, 2600);
  else M.base = text;
  renderMiniFoot();
}

function miniInit() {
  if (miniBridge) document.documentElement.classList.add("in-panel");
  const root = el("div", { id: "mini", class: "mini-root" },
    el("div", { class: "m-bar" },
      el("span", { class: "m-glass", html: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>' }),
      el("span", { class: "m-field" },
        el("span", { class: "m-mirror", id: "m-mirror", "aria-hidden": "true" }),
        el("input", { id: "mq", type: "search", autocomplete: "off", spellcheck: "false", placeholder: "Search your folders · w: for one folder", "aria-label": "Search",
          "aria-autocomplete": "list", "aria-controls": "m-suggest", "aria-expanded": "false" })),
      el("button", { type: "button", class: "m-chip", id: "m-chip", hidden: true, onmousedown: (e) => e.preventDefault() }),
      el("nav", { class: "m-scopes", id: "m-scopes", "aria-label": "Where to search" }),
      el("button", { type: "button", class: "m-pick", id: "m-pick", title: "Folders and groups (⌘F)", "aria-label": "Folders and groups", "aria-haspopup": "dialog", "aria-expanded": "false",
        onmousedown: (e) => e.preventDefault(), onclick: (e) => { e.stopPropagation(); openMiniPicker(); },
        html: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5"/></svg>' })),
    el("div", { class: "m-pop", id: "m-menu", hidden: true }),
    el("div", { class: "m-pop", hidden: true }, el("div", { class: "m-suggest", id: "m-suggest", role: "listbox", "aria-label": "Folders and groups" })),
    el("div", { class: "m-answer", id: "m-answer", hidden: true, "aria-live": "polite" }),
    el("ol", { class: "m-list", id: "m-list", role: "listbox", "aria-label": "Results" }),
    el("footer", { class: "m-foot", id: "m-foot", hidden: true }, el("span", { id: "m-status", class: "m-status" }), el("span", { class: "m-keys", id: "m-keys" })));
  document.body.append(root);
  const q = $("#mq");
  q.value = localGet("mini.q") || "";
  M.field = shortcutField({ input: q, mirror: $("#m-mirror"), chip: $("#m-chip"), box: $("#m-suggest"), inline: true,
    tabs: () => $("#m-scopes"), onSuggest: () => { $("#m-suggest").parentElement.hidden = $("#m-suggest").hidden; } });
  q.addEventListener("input", () => { clearTimeout(M.timer); M.timer = setTimeout(miniSearch, 120); });
  document.addEventListener("keydown", miniKey);
  $("#m-answer").addEventListener("click", (e) => {
    const c = e.target.closest(".cite");
    if (c) { const i = ask.sources.findIndex((s) => String(s.n) === c.dataset.n); if (i >= 0) { M.sel = i; markMini(); } }
  });
  new ResizeObserver(reportSize).observe(root);
  new MutationObserver(reportSize).observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
  miniRefresh().then(() => { if (q.value.trim()) miniSearch(); });
  probeFeedback();
  if (!miniBridge) q.focus();
  tell({ type: "ready" });
}

/* The panel's height follows the content. The list is the only part that shrinks when the panel is at its
   maximum height, so its natural height is its scroll height. */
function reportSize() {
  if (M.sizeRaf) return;
  M.sizeRaf = requestAnimationFrame(() => {
    M.sizeRaf = 0;
    let h = 0;
    for (const c of $("#mini").children) {
      if (c.hidden || getComputedStyle(c).display === "none") continue;
      h += c.id === "m-list" ? c.scrollHeight + (c.offsetHeight - c.clientHeight) : c.getBoundingClientRect().height;
    }
    tell({ type: "size", height: Math.ceil(h) });
  });
}

/* the app calls this each time the panel opens */
function miniShow() {
  const q = $("#mq");
  q.focus(); q.select();
  miniRefresh();
  return true;
}

/* mysearch://mini?q=… : start Quick Search with this query (the app validates it; it is only ever text) */
function miniOpenQuery(q) {
  const inp = $("#mq");
  inp.value = String(q || "").slice(0, 500);
  M.field?.update();
  if (M.mode === "answer") miniCloseAnswer();
  miniSearch();
  inp.focus();
  return true;
}

async function miniRefresh() {
  try {
    const [st, cs] = await Promise.all([api("/api/status"), api("/api/collections")]);
    if (st.ui_version && UI_VERSION && st.ui_version !== UI_VERSION) { location.reload(); return; }   // the interface was updated
    state.status = st;
    state.collections = cs.collections || [];
    state.groups = cs.groups || [];
    renderMiniScopes(); renderMiniFoot(); M.field?.update();
    if (st.ask && "route" in st.ask && prov.api === null && !prov.loading) loadProviders();   // provider names for the answer label
  } catch {
    state.status = null;
    setStatus("The search service is not running. Start it with mse service install.", true);
  }
}

/* ------------------------------------------------------------------ scopes */

function miniScopeList() {
  const sel = new Set(M.scope.split(","));
  const groups = (state.groups || []).filter((g) => !g.hidden || sel.has(g.name))      // switched off as a tab: menu only
    .map((g) => ({ id: g.name, label: g.label || g.name, group: true }));
  return [{ id: "all", label: "All" }, ...groups, ...state.collections.map((c) => ({ id: c.name, label: c.name }))];
}
function renderMiniScopes() {
  const list = miniScopeList();
  const known = new Set(["all", ...state.collections.map((c) => c.name), ...(state.groups || []).map((g) => g.name)]);
  if (!M.scope.split(",").every((x) => known.has(x))) M.scope = "all";
  state.scope = M.scope;
  const sel = new Set(M.scope.split(","));
  $("#m-scopes").replaceChildren(...list.map((s, i) => el("button", {
    "aria-pressed": String(sel.has(s.id)), class: s.group ? "group" : null, tabindex: "-1",
    title: `${s.id === "all" ? "Every folder marked Include in All" : s.group ? `Group ${s.label}` : `Only ${s.label} · ⌘-click to add it`}${i < 9 ? ` (⌘${i + 1})` : ""}`,
    onmousedown: (e) => e.preventDefault(), onclick: (e) => setMiniScope(s.id, e.metaKey),
  }, s.label)));
}
/* the Folders and groups menu, inline under the bar so the panel grows with it */
function openMiniPicker() {
  openScopeMenu({ get: () => M.scope, set: (sc, additive) => setMiniScope(sc, additive), anchor: $("#m-pick"), inline: $("#m-menu"),
    onClose: () => { $("#m-menu").hidden = true; $("#mq").focus(); } });
}

/* plain click: just this one; ⌘-click on a folder: search it together with the others ticked */
function setMiniScope(id, additive) {
  const groups = new Set((state.groups || []).map((g) => g.name));
  if (additive && id !== "all" && !groups.has(id)) {
    const cur = M.scope === "all" || M.scope.split(",").some((x) => groups.has(x)) ? [] : M.scope.split(",");
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    id = next.length ? next.join(",") : "all";
  }
  M.scope = id; state.scope = id; localSet("scope.mini", id);
  renderMiniScopes(); M.field?.update();
  $("#m-scopes [aria-pressed=true]")?.scrollIntoView({ inline: "nearest", block: "nearest" });
  if (M.mode === "answer") miniCloseAnswer();
  miniSearch();
}

/* ------------------------------------------------------------------ search: instant results, then the precise re-order */

async function miniSearch() {
  const q = $("#mq").value.trim();
  localSet("mini.q", q);
  if (M.mode === "answer" && q !== ask.q) miniCloseAnswer();
  if (M.req) M.req.abort();
  if (!q) { M.results = []; M.q = ""; renderMiniList(); setStatus("", true); return; }
  const sc = splitShortcut(q);
  if (sc.unknown.length) { setStatus(`No folder or group is called “${sc.unknown.join("”, “")}”`, true); return; }   // typing "in:wo"
  const ctrl = new AbortController(); M.req = ctrl;
  const p = new URLSearchParams({ q, mode: "hybrid", limit: "8" });
  if (M.scope !== "all") p.set("c", M.scope);
  const get = async (params) => {
    const r = await fetch(`/api/search?${params}`, { signal: ctrl.signal, headers: { "X-MSE-Token": TOKEN } });
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  };
  if (!M.results.length) setStatus("Searching…", true);
  try {
    const res = await get(p);
    if (M.req !== ctrl) return;
    miniResults(res, q, false);
    if (res.results.length > 1) {
      p.set("precise", "1");
      const res2 = await get(p);
      if (M.req !== ctrl) return;
      if (res2.results.map((x) => x.docid).join() !== res.results.map((x) => x.docid).join()) miniResults(res2, q, true);
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    M.results = []; renderMiniList();
    setStatus(e.message.includes("fetch") ? "The search service is not running." : e.message, true);
  }
}

function miniResults(res, q, refined) {
  const keep = M.results[M.sel]?.docid;
  M.results = res.results.slice(0, 8); M.q = q; M.res = res;
  state.query = q;
  state.bridge = [...new Set(Object.values(res.bridge || {}).flat())];
  state.terms = (res.text || "").split(/\s+/).filter(Boolean);
  const i = refined ? M.results.findIndex((r) => r.docid === keep) : -1;
  M.sel = Math.max(0, i);
  if (M.mode === "results") renderMiniList();
  const n = M.results.length;
  setStatus(n ? `${res.total > n ? `Top ${n} of ${res.total}` : `${n} result${n === 1 ? "" : "s"}`} · ${res.timings.total_ms} ms${refined ? " · re-ranked" : ""}` : "Nothing found. Try other words, or All.", true);
}

/* ------------------------------------------------------------------ list */

function renderMiniList() {
  const answer = M.mode === "answer";
  const items = answer ? ask.sources : M.results;
  const multi = !answer && new Set(M.results.map((r) => r.collection)).size > 1;
  $("#m-list").replaceChildren(...items.map((r, i) => (answer ? sourceRow(r, i) : miniRow(r, i, multi))));
  markMini(false);
}

function rowEvents(i, item) {
  return {
    onclick: () => { M.sel = i; markMini(false); },
    ondblclick: () => miniOpen(item, false),
    onmousedown: (e) => e.preventDefault(),        // keep the caret in the search field
  };
}

function miniRow(r, i, multi) {
  const hit = r.hits.find((h) => h.snippet) || r.hits[0];
  const where = hit?.page ? `p.${hit.page}` : hit?.t0 != null ? clock(hit.t0) : "";
  const dir = r.path.includes("/") ? r.path.slice(0, r.path.lastIndexOf("/")) : "";
  const folder = [multi ? r.collection : null, dir].filter(Boolean).join(" › ");
  const icon = r.thumb ? `<img src="${thumbUrl(r.collection, r.path)}" alt="" loading="lazy">` : KIND_ICON[r.kind] || KIND_ICON.other;
  return el("li", { class: "m-r", role: "option", "aria-selected": "false", ...rowEvents(i, r) },
    el("span", { class: "m-ico" + (r.thumb ? " thumb" : ""), html: icon, title: r.ext ? r.ext.toUpperCase() : r.kind }),
    el("div", { class: "m-main" },
      el("div", { class: "m-l1" },
        el("span", { class: "m-title" }, r.title && r.kind !== "text" ? r.title : r.name),
        folder ? el("span", { class: "m-path", title: `${r.collection}/${r.path}` }, el("bdi", {}, folder)) : null),
      el("div", { class: "m-snip", html: hit?.snippet
        ? (where ? `<span class="where">${esc(where)}</span>` : "") + markBridge(hit.snippet)
        : esc(r.metadata_only ? "File name only: contents are not indexed" : hit ? `${where ? `${where} · ` : ""}${hit.modality === "title" ? "name" : hit.modality} match` : "") })),
    el("div", { class: "m-side" },
      r.favorite ? el("span", { class: "m-fav" + (r.favorite === "similar" ? " sim" : ""), html: ICON.star,
        title: r.favorite === "query" ? "Best result for this search" : "Best result for a similar search" }) : null,
      el("span", { class: "m-date" }, r.date || "")));
}

function sourceRow(s, i) {
  const dir = s.path.includes("/") ? s.path.slice(0, s.path.lastIndexOf("/")) : "";
  return el("li", { class: "m-r", role: "option", "aria-selected": "false", ...rowEvents(i, s) },
    el("span", { class: "m-ico m-n" }, String(s.n)),
    el("div", { class: "m-main" },
      el("div", { class: "m-l1" }, el("span", { class: "m-title" }, s.title || s.path.split("/").pop()),
        el("span", { class: "m-path", title: `${s.collection}/${s.path}` }, el("bdi", {}, [s.collection, dir].filter(Boolean).join(" › ")))),
      s.snippet ? el("div", { class: "m-snip" }, s.snippet) : null),
    el("div", { class: "m-side" }, el("span", { class: "m-date" }, s.page ? `p. ${s.page}` : s.ts || "")));
}

function markMini(scroll = true) {
  const rows = $$(".m-r");
  rows.forEach((x, k) => x.setAttribute("aria-selected", String(k === M.sel)));
  if (scroll) rows[M.sel]?.scrollIntoView({ block: "nearest" });
  renderMiniFoot();
}
function miniCurrent() { return (M.mode === "answer" ? ask.sources : M.results)[M.sel]; }
function moveMini(d) {
  const n = (M.mode === "answer" ? ask.sources : M.results).length;
  if (!n) return;
  M.sel = (M.sel + d + n) % n;
  markMini();
  if (M.qlOpen) { const it = (M.mode === "answer" ? ask.sources : M.results)[M.sel]; if (it) miniQuickLook(it, true); }
}

function renderMiniFoot() {
  const q = $("#mq").value.trim();
  const foot = $("#m-foot");
  foot.hidden = !q && !$("#m-status").textContent;
  const K = (keys, label) => el("span", { class: "k" }, ...keys.map((k) => el("kbd", {}, k)), label);
  const hasItem = (M.mode === "answer" ? ask.sources : M.results).length > 0;
  const keys = [];
  if (hasItem) keys.push(K(["↵"], M.mode === "answer" ? "Open source" : "Open"), K(["⌘", "↵"], "Reveal"), K(["⌘", "Y"], "Quick Look"));
  if (M.mode === "answer") keys.push(K(["⇥"], "Continue in Ask"), K(["esc"], ask.ctrl ? "Stop" : "Back"));
  else {
    if (q && askReady()) keys.push(K(["⌥", "↵"], "Ask"));
    else if (q && state.status?.ask?.available) keys.push(K(["⌥", "↵"], "Set up Ask"));
    if (q) keys.push(K(["⇥"], "Open in MySearch"));
  }
  $("#m-keys").replaceChildren(...keys);
}

/* ------------------------------------------------------------------ actions */

async function absPath(item) {
  if (item.abs_path) return item.abs_path;
  const d = await api(`/api/doc?c=${enc(item.collection)}&ref=${enc(item.path)}&max=1`);
  return d.abs_path;
}
function hitOf(item) {
  if (item.n != null) return { page: item.page || null, t0: tsToSec(item.ts) };   // a cited source
  const h = item.hits?.find((x) => x.snippet) || item.hits?.[0];
  return { page: h?.page || null, t0: h?.t0 ?? null };
}

async function miniOpen(item, reveal) {
  if (!item) return;
  try {
    // `page` is ignored by services that cannot open a file at a page yet
    await post("/api/open", { collection: item.collection, path: item.path, reveal, page: hitOf(item).page });
    if (!tell({ type: "hide" })) setStatus(reveal ? "Shown in Finder" : "Opened");
  } catch (e) { setStatus(e.message); }
}
async function miniCopy(item) {
  try {
    const p = await absPath(item);
    if (!tell({ type: "copy", text: p })) await navigator.clipboard.writeText(p);
    setStatus("Path copied");
  } catch (e) { setStatus(e.message); }
}
async function miniQuickLook(item, follow) {
  if (!follow && M.qlOpen) { closeQuickLook(); return; }               // ⌘Y again closes it, like Finder
  if (item.metadata_only) { if (!follow) setStatus("This file is name-only: open it to look inside."); return; }
  try {
    const p = await absPath(item);
    if (tell({ type: "quicklook", path: p, update: !!follow })) M.qlOpen = true;
    else if (!follow) setStatus("Quick Look opens in the app");
  } catch (e) { setStatus(e.message); }
}
function closeQuickLook() { M.qlOpen = false; tell({ type: "quicklookClose" }); }
/* the app reports that Quick Look was closed */
function miniQuickLookClosed() { M.qlOpen = false; return true; }
async function miniFav(r) {
  if (fb.available !== true) { setStatus(fb.available === false ? "Favourites need a newer search service." : "Favourites are not available right now."); return; }
  try {
    if (r.favorite === "query") { await post("/api/feedback/remove", { query: M.q, collection: r.collection, path: r.path }); r.favorite = null; setStatus("No longer the best result"); }
    else { await post("/api/feedback", { query: M.q, collection: r.collection, path: r.path, kind: "best" }); r.favorite = "query"; setStatus("Best result for this search"); }
    renderMiniList();
  } catch (e) { setStatus(e.message); }
}
/* Tab / ⌘O on an answer: it goes on as a chat in the main window's Ask view. A finished answer is saved as a chat
   (the same storage as the main window) and opened there by id; one still being written is asked again there. */
function handOffAnswer() {
  if (ask.ctrl) { stopAsk(true); return; }
  if (!ask.q || (!ask.text && !ask.error)) return;
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const sc = splitShortcut(ask.q);
  chat.threads = loadChats();
  chat.threads.unshift({ id, title: (sc.rest || ask.q).slice(0, 80), created: Date.now(), updated: Date.now(), turns: [{
    q: ask.q, a: ask.text, sources: ask.sources, citations: ask.done?.citations || [], model: ask.done?.model,
    ...(ask.done?.where ? { where: ask.done.where, provider: ask.done.provider || null, fallback_reason: ask.done.fallback_reason || null, redacted: ask.done.redacted ?? null, ...(ask.done.training ? { training: true } : {}) } : {}),
    ms: ask.done?.timings?.total_ms, answered: ask.done?.answered, ...(ask.error ? { error: ask.error } : {}),
    scope: sc.label || scopeLabel(M.scope), at: Date.now() }] });
  saveChats();
  localSet("chat.handoff", id);
}

/* the main web UI, for a plain browser (no app bridge): the page is served only with the token, so the link carries it */
function homePage() { return "/?t=" + encodeURIComponent(TOKEN); }

function miniToApp(item) {
  const q = $("#mq").value.trim();
  if (M.mode === "answer") handOffAnswer();
  const msg = { type: "openInApp", q, scope: M.scope, collection: item?.collection || null, path: item?.path || null, ask: M.mode === "answer" };
  if (!tell(msg)) location.href = homePage();
}

/* ------------------------------------------------------------------ Ask, compact */

function miniAsk() {
  const q = $("#mq").value.trim();
  if (!q) return;
  if (!askReady()) {
    const a = state.status?.ask;
    if (a?.available) {      // off: one line, with a link to the main window's Ask view (the key field and Turn on Ask are there)
      setStatus(cloudApi() && !cloudReady() ? "Ask is off and has no cloud key yet." : "Ask is off.", true);
      $("#m-status").append(" ", miniSetupLink("Set up Ask in MySearch"));
    } else setStatus(!a ? "Ask is not part of this search service yet." : "The Ask model is not installed.");
    return;
  }
  M.mode = "answer"; M.sel = 0;
  state.scope = M.scope;
  startAsk(q);
  setStatus("Asking…", true);
  miniRenderAsk();
}
/* a link to the main window's Ask view, where the cloud key goes in and Ask is turned on */
function miniSetupLink(label) {
  return el("button", { type: "button", class: "linklike m-setup", onmousedown: (e) => e.preventDefault(), onclick: miniOpenAskSetup }, label);
}
function miniOpenAskSetup() {
  localSet("askSetup", "1");                               // openFromMini (app.js) reads this and opens the key card
  if (!tell({ type: "openInApp", q: "", scope: M.scope, collection: null, path: null, ask: true })) { localSet("askSetup", ""); location.href = homePage(); }
}
function miniCloseAnswer() {
  stopAsk(true);
  M.mode = "results"; M.sel = 0;
  $("#m-answer").hidden = true;
  renderMiniList();
  if (M.res) setStatus(`${M.results.length} result${M.results.length === 1 ? "" : "s"}`, true);
}
function miniRenderAsk() {
  const box = $("#m-answer");
  if (M.mode !== "answer") { box.hidden = true; return; }
  box.hidden = false;
  const streaming = !!ask.ctrl;
  const body = ask.error ? el("p", { class: "ask-error" }, ask.error)
    : !ask.text ? el("p", { class: "ask-wait" }, streaming ? (ask.sources.length ? `Reading ${ask.sources.length} documents…` : "Finding the documents to read…") : ask.stopped ? "Stopped." : "No answer.")
    : el("div", { class: "ask-text" + (streaming ? " streaming" : ""), html: answerHtml(ask.text, ask.sources) });
  const d = ask.done;
  box.replaceChildren(
    el("div", { class: "m-ans-head" },
      el("span", { class: "m-ans-label" }, el("span", { class: "ask-label" }, "Answer"),
        d?.where && ask.text ? [d.model ? el("span", { class: "m-ans-model", title: whereTitle(d) }, d.model) : null, whereBadge(d.where, whereTitle(d)), d.training ? trainBadge() : null] : null),
      !streaming && ask.text ? el("button", { class: "btn small", onmousedown: (e) => e.preventDefault(), onclick: () => miniCopyAnswer() }, "Copy answer") : null),
    body,
    (ask.done || ask.stopped) && ask.text ? el("p", { class: "m-ans-foot" }, `${answerFooter(ask)} · check the sources`) : null,
    cloudApi() && !cloudReady() ? el("p", { class: "m-ans-foot" }, "Answered on this Mac. For a cloud model, add an API key: ", miniSetupLink("Set up in MySearch")) : null);
  if (M.sel >= ask.sources.length) M.sel = 0;
  if (ask.sources.length) setStatus(`${ask.sources.length} source${ask.sources.length === 1 ? "" : "s"}${streaming ? " · answering…" : ""}`, true);
  else if (ask.error) setStatus("", true);
  renderMiniList();
}
function miniCopyAnswer() {
  const text = answerCopy(ask);
  if (!tell({ type: "copy", text })) navigator.clipboard?.writeText(text);
  setStatus("Answer copied with its sources");
}

/* ------------------------------------------------------------------ keys */

function miniKey(e) {
  const meta = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
  const items = M.mode === "answer" ? ask.sources : M.results;
  const cur = items[M.sel];
  if (e.key === "ArrowDown" || (e.ctrlKey && k === "n")) { e.preventDefault(); moveMini(1); }
  else if (e.key === "ArrowUp" || (e.ctrlKey && k === "p")) { e.preventDefault(); moveMini(-1); }
  else if (e.key === "Enter" && e.altKey) { e.preventDefault(); miniAsk(); }
  else if (e.key === "Enter") { e.preventDefault(); miniOpen(cur, meta); }
  else if (meta && k === "y") { e.preventDefault(); if (cur) miniQuickLook(cur); }
  else if (meta && k === "c") {
    const inp = $("#mq");
    if (!cur || inp.selectionStart !== inp.selectionEnd) return;      // copying typed text works as usual
    e.preventDefault(); miniCopy(cur);
  }
  else if (meta && k === "d") { e.preventDefault(); if (cur && M.mode === "results") miniFav(cur); }
  else if ((e.key === "Tab" && !e.shiftKey && !meta) || (meta && k === "o")) { e.preventDefault(); if ($("#mq").value.trim()) miniToApp(cur); }
  else if (meta && /^[1-9]$/.test(e.key)) { e.preventDefault(); const s = miniScopeList()[Number(e.key) - 1]; if (s) setMiniScope(s.id); }
  else if (meta && k === "f") { e.preventDefault(); openMiniPicker(); }
  else if (e.key === "Escape") {
    e.preventDefault();
    if (M.qlOpen) closeQuickLook();
    else if (M.mode === "answer") { if (ask.ctrl) stopAsk(); else miniCloseAnswer(); }
    else if ($("#mq").value) { $("#mq").value = ""; M.field?.update(); miniSearch(); }
    else tell({ type: "hide" });
  }
}

if (MINI) miniInit();
