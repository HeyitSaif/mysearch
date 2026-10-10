/* Text helpers shared by the commands. Pure: no Raycast imports. */

import type { Hit, SearchResult } from "./types";

export const KIND_LABEL: Record<string, string> = {
  pdf: "PDF",
  office: "Office",
  text: "Notes & text",
  email: "E-mail",
  image: "Image",
  audio: "Audio",
  video: "Video",
  other: "Other",
};

/** Type filters offered in the UI, in the app's order (mse/ui/app.js KIND_LABEL). */
export const TYPE_FILTERS: { value: string; title: string }[] = [
  { value: "pdf", title: "PDFs" },
  { value: "office", title: "Office Files" },
  { value: "text", title: "Notes & Text" },
  { value: "email", title: "E-mails" },
  { value: "image", title: "Images" },
  { value: "audio", title: "Audio" },
  { value: "video", title: "Video" },
];

export function displayTitle(r: Pick<SearchResult, "title" | "kind" | "name">): string {
  return r.title && r.kind !== "text" ? r.title : r.name;
}

export function folderOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i > 0 ? path.slice(0, i) : "";
}

export function clock(t: number | null | undefined): string {
  const s = Math.round(t || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function formatSize(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2025-03-14" -> "14 Mar 2025"; partial dates ("2025-03", "2025") are shown as far as they go. */
export function formatDate(d: string | null | undefined): string {
  if (!d) return "";
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(d);
  if (!m) return d;
  const [, y, mo, da] = m;
  if (!mo) return y;
  const mon = MONTHS[Number(mo) - 1] || mo;
  return da ? `${Number(da)} ${mon} ${y}` : `${mon} ${y}`;
}

/** The best passage of a result: the first hit with a snippet, else the first hit. */
export function topHit(r: SearchResult): Hit | undefined {
  return r.hits.find((h) => h.snippet) || r.hits[0];
}

/** "p. 3" for a page, "1:23" for a time in audio/video, "" otherwise. */
export function whereOf(h: Hit | undefined, long = false): string {
  if (!h) return "";
  if (h.t0 !== null && h.t0 !== undefined) return long ? `${clock(h.t0)}–${clock(h.t1)}` : clock(h.t0);
  if (h.page) return long ? `page ${h.page}` : `p. ${h.page}`;
  return "";
}

const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

function unescapeHtml(s: string): string {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m] ?? m);
}

/** Snippets arrive HTML-escaped with <mark> around matches. Plain text, as the app's "Copy for AI" uses it. */
export function plainSnippet(html: string | undefined): string {
  return unescapeHtml((html || "").replace(/<\/?mark[^>]*>/g, ""));
}

/** Escapes text so markdown renders it literally (snippets come from arbitrary documents). */
export function escapeMarkdown(s: string): string {
  return s
    .replace(/[\\`*_{}[\]<>#|~!]/g, "\\$&")
    .replace(/^(\s*)([-+])(\s)/gm, "$1\\$2$3")
    .replace(/^(\s*)(\d+)\.(\s)/gm, "$1$2\\.$3")
    .replace(/\s+/g, " ");
}

/** A snippet as markdown: matches in bold, everything else literal. */
export function snippetToMarkdown(html: string | undefined): string {
  const parts = (html || "").split(/<mark[^>]*>(.*?)<\/mark>/g);
  return parts
    .map((p, i) => {
      const t = escapeMarkdown(unescapeHtml(p));
      return i % 2 === 1 && t.trim() ? `**${t.trim()}**` : t;
    })
    .join("")
    .trim();
}

/** The same text as the app's "Copy for AI" button (mse/ui/app.js citation()). */
export function citation(r: SearchResult): string {
  const lines = [`Source: ${r.name} — ${r.collection}/${r.path} (#${r.docid}${r.date ? ", " + r.date : ""})`];
  for (const h of r.hits.filter((x) => x.snippet).slice(0, 2)) {
    const where = h.page ? `p.${h.page}: ` : h.t0 !== null && h.t0 !== undefined ? `${clock(h.t0)}: ` : "";
    lines.push(`> ${where}${plainSnippet(h.snippet)}`);
  }
  lines.push(`Full text: mse get '#${r.docid}'`);
  return lines.join("\n");
}

export interface ResultSection {
  title: string;
  subtitle?: string;
  results: SearchResult[];
}

/** Favourites and the best few results first (whatever folder they are in), then the rest by folder, folders in
    the order their best result ranks. Inside a section the service's ranking is kept. */
export function sectionize(results: SearchResult[], topN = 3): ResultSection[] {
  if (!results.length) return [];
  const fav = results.filter((r) => r.favorite === "query");
  const rest = results.filter((r) => r.favorite !== "query");
  const sections: ResultSection[] = [];
  if (fav.length) sections.push({ title: "Favourites", results: fav });
  const top = rest.slice(0, Math.max(0, topN - fav.length));
  if (top.length) sections.push({ title: "Top Hits", results: top });
  const byColl = new Map<string, SearchResult[]>();
  for (const r of rest.slice(top.length)) {
    if (!byColl.has(r.collection)) byColl.set(r.collection, []);
    byColl.get(r.collection)!.push(r);
  }
  for (const [c, list] of byColl) sections.push({ title: c, subtitle: String(list.length), results: list });
  return sections;
}

/* ------------------------------------------------------------------ the scope shortcut ("work: invoice", "invoice in:work")
   The service reads it (mse/config.py scope_shortcut) and reports the result in response.scope. The extension only
   needs to know when an `in:` token names nothing yet (while typing "in:wo"), so it can wait instead of failing. */

/** The search filters of the query language: never a folder, group or alias name (mse/config.py FILTER_KEYS). */
export const RESERVED = new Set(["type", "kind", "ext", "in", "after", "before", "path", "has", "include", "exclude"]);

/** Names after `in:` that are not a known folder, group, alias or "all" (lower case). */
export function unknownInNames(text: string, known: Set<string>): string[] {
  const quoted = [...text.matchAll(/"[^"]*"|“[^”]*”|„[^“”]*[“”]/g)].map((m) => [
    m.index ?? 0,
    (m.index ?? 0) + m[0].length,
  ]);
  const out: string[] = [];
  for (const m of text.matchAll(/(?<!\S)in:(?:"([^"]*)"|([\w-]+(?:,[\w-]+)*))(?=\s|$)/gi)) {
    const at = m.index ?? 0;
    if (quoted.some(([a, b]) => a <= at && at < b)) continue;
    const raw = (m[1] ?? m[2] ?? "").trim();
    if (raw.startsWith("/") || raw.startsWith("~")) continue; // a folder path (in:"/Users/…"), never a name
    for (const n of raw.split(",").map((x) => x.trim().toLowerCase())) {
      if (n && !known.has(n) && !out.includes(n)) out.push(n);
    }
  }
  return out;
}

/** The file kinds of the whole-Mac name search (mse/filequery.py KINDS) behind each type filter. Several kinds mean
    "any of them", so the query carries one kind: term per entry. */
export const NAME_KINDS: Record<string, string[]> = {
  pdf: ["pdf"],
  office: ["doc", "sheet", "slides"],
  text: ["doc"],
  email: ["email"],
  image: ["image"],
  audio: ["audio"],
  video: ["video"],
};

/** "Work · work, w" for the dropdown: the label, then what can be typed for it when that adds something. */
export function scopeTitle(label: string, name: string, aliases: string[] = [], note = ""): string {
  const typed = [name, ...aliases].filter((x, i, a) => a.indexOf(x) === i && (i > 0 || x !== label));
  return `${label}${typed.length ? ` · ${typed.join(", ")}` : ""}${note}`;
}

/** A stable id for a result row (Raycast keeps the selection on it when the list re-orders). */
export function resultId(r: Pick<SearchResult, "collection" | "path">): string {
  return `${r.collection}/${r.path}`;
}

/* ------------------------------------------------------------------ links into the MySearch app */

export interface AppLink {
  q?: string; // the search to run (the app reads at most 500 characters)
  scope?: string; // "all", a collection, a group, or a comma list
  docid?: string; // 8-hex document id, with or without a leading "#"
  page?: number | null; // page to show in the preview
}

/** mysearch://search?q=…&c=…&doc=…&page=… — the main window at that query and scope, previewing that document.
    Every value is percent-encoded (spaces as %20, not "+"), and values the app would reject are left out. */
export function appLink({ q, scope, docid, page }: AppLink): string {
  const parts: string[] = [];
  const add = (k: string, v: string) => parts.push(`${k}=${encodeURIComponent(v)}`);
  const query = (q || "").trim().slice(0, 500);
  if (query) add("q", query);
  if (scope && /^[\w\-,]{1,400}$/.test(scope)) add("c", scope);
  const doc = (docid || "").replace(/^#/, "").toLowerCase();
  if (/^[0-9a-f]{6,16}$/.test(doc)) add("doc", doc);
  if (page && Number.isInteger(page) && page > 0) add("page", String(page));
  return `mysearch://search${parts.length ? "?" + parts.join("&") : ""}`;
}

/* ------------------------------------------------------------------ whole-Mac name results */

/** Removes every `in:` token (a folder or group shortcut, quoted or not) so a new one can replace it. An `in:` inside
    a quoted phrase is words, not a shortcut, and stays. */
export function stripIn(text: string): string {
  return text
    .replace(/"[^"]*"|(?<!\S)in:(?:"[^"]*"|\S+)/gi, (m) => (m.startsWith('"') ? m : ""))
    .replace(/\s+/g, " ")
    .trim();
}

/** The `in:` token that searches exactly one folder: in:"/Users/…/Folder" (quoted, so spaces are safe). */
export function inFolderToken(folder: string): string {
  return `in:"${folder.replace(/"/g, "")}"`;
}

/** "/Users/alex/Documents" -> "~/Documents" (the home folder shown as ~). */
export function homeRelative(path: string, home: string): string {
  if (home && path === home) return "~";
  return home && path.startsWith(home + "/") ? "~" + path.slice(home.length) : path;
}

/** Markdown for a file name with the matched spans in bold. `spans` are [start, end) character offsets. */
export function boldSpansMarkdown(name: string, spans: [number, number][]): string {
  const ranges = [...spans].sort((a, b) => a[0] - b[0]);
  let out = "";
  let at = 0;
  for (const [s, e] of ranges) {
    if (s < at || e <= s || e > name.length) continue;
    out += escapeMarkdown(name.slice(at, s));
    out += `**${escapeMarkdown(name.slice(s, e))}**`;
    at = e;
  }
  return out + escapeMarkdown(name.slice(at));
}
