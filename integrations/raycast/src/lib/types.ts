/* The shapes the MySearch HTTP API returns (mse/server.py, mse/search.py). Only the fields this extension reads. */

export type Kind = "pdf" | "office" | "text" | "email" | "image" | "audio" | "video" | "other";

export interface Hit {
  modality: string; // text | ocr | title | transcript | visual | audio ...
  page: number | null;
  t0: number | null; // seconds, audio/video
  t1: number | null;
  heading: string | null;
  why: string[];
  similarity: number | null;
  snippet?: string; // HTML-escaped, matches wrapped in <mark>
  from?: string; // the version of the document this passage came from
}

export interface Copy {
  path: string;
  docid: string;
  kind: string;
}

export interface SearchResult {
  collection: string;
  path: string; // relative to the collection root
  abs_path: string;
  name: string;
  ext: string | null;
  kind: Kind | string;
  title: string | null;
  docid: string;
  date: string | null;
  size: number | null;
  pages: number | null;
  duration: number | null;
  thumb: boolean;
  metadata_only: boolean;
  lang: string | null;
  score: number;
  similarity: number | null;
  why: string[];
  hits: Hit[];
  copies: Copy[];
  context: string | null;
  relevance: number;
  /** Present only on services that support favourites: "query" (marked for this exact search),
      "similar" (marked for a similar search) or null. Absent on older services. */
  favorite?: "query" | "similar" | null;
}

export interface SearchResponse {
  query: string;
  text: string;
  collections: string[];
  bridge: Record<string, string[]>;
  results: SearchResult[];
  timings: { total_ms: number; rerank_ms?: number; [k: string]: number | string | undefined };
  facets?: { collection: Record<string, number>; kind: Record<string, number>; year: Record<string, number> };
  /** Where the search ran. `shortcut` is there when the query named it ("work: invoice", "invoice in:work"),
      which overrides the dropdown. Older services leave `scope` out. */
  scope?: { collections: string[]; label: string; shortcut?: { names: string[]; tokens: string[] } };
  total: number;
  feedback?: unknown;
  // /api/similar adds these
  similar_to?: string;
  collection?: string;
  note?: string;
}

export interface CollectionStats {
  files: number;
  bytes: number;
  metadata_only: number;
  errors: number;
  heavy_pending: number;
  items: number;
  embed_pending: number;
  last_scan: string | null;
  last_heavy: string | null;
}

export interface Collection {
  name: string;
  path: string;
  description: string | null;
  in_global: boolean;
  enabled: boolean;
  exists: boolean;
  stats: CollectionStats | null;
  /** Short names for the search shortcut ("co: invoice"); absent on older services. */
  aliases?: string[];
}

/** A named set of folders (GET /api/collections -> groups). Searching it searches exactly its members. */
export interface Group {
  name: string; // slug, usable as the search scope
  label: string;
  collections: string[];
  files: number;
  /** Short names for the search shortcut ("w: invoice"); absent on older services. */
  aliases?: string[];
  /** Not shown as a tab in the app; still searchable by name, alias and from menus. */
  hidden?: boolean;
}

export interface Scopes {
  collections: Collection[];
  groups: Group[];
}

export interface Job {
  id: number;
  kind: string;
  collection: string | null;
  path: string | null;
  status: string;
  stage?: string;
  done?: number;
  total?: number;
  message?: string;
  error?: string;
}

export interface Worker {
  running: boolean;
  rss_mb: number | null;
  idle_s: number | null;
}

export interface Status {
  version: string;
  uptime_s: number;
  daemon_rss_mb: number | null;
  workers: Record<string, Worker>;
  jobs: {
    current: Job | null;
    queued: Job[];
    recent: Job[];
    next_scan_in_s: number | null;
    nightly: { at: string; enabled: boolean; last_run: string | null };
    watcher: { running: boolean; folders: number; events: number; pending: number } | null;
  } | null;
  idle_unload_min: number;
  rerank_enabled: boolean;
  asr_present: boolean;
}

/** One row of GET /api/files/search (whole-Mac or indexed-scope file names). `match` holds [start, end] spans of the
    name that matched, in characters of `name`. */
export interface NameItem {
  path: string; // absolute
  name: string;
  parent: string; // absolute folder
  is_dir: boolean;
  kind: Kind | string;
  ext: string | null;
  size: number;
  mtime: number; // unix seconds
  ctime: number;
  score: number;
  match: [number, number][];
  collection?: string;
}

export interface NameSearchResponse {
  items: NameItem[];
  total: number;
  ms: number;
  scope: "mac" | "indexed";
  /** False while the whole-Mac index is still being built: the items are then empty or partial. */
  searchable: boolean;
  truncated: boolean;
  shortcut?: { names: string[]; tokens: string[] };
}

/** GET /api/files/status (mse/macindex.py status()). `progress.fraction` is 0..1 while crawling. */
export interface NameIndexStatus {
  enabled: boolean;
  entries: number;
  crawling: boolean;
  state: "disabled" | "crawling" | "ready" | "waiting" | "not crawled";
  progress: { phase?: string; fraction?: number; entries?: number; units_done?: number; units_total?: number } | null;
  last_crawl: number | null;
  searchable: boolean;
  full_disk_access?: boolean;
  error?: string | null;
}

/** GET /api/tree?c=&path= : the subfolders of one folder. */
export interface TreeResponse {
  collection: string;
  path: string;
  folders: { name: string; path: string; children?: number }[];
}

/** Events of POST /api/ask (NDJSON, one per line). */
export interface AskSource {
  n: number;
  docid: string; // "#abcdef12"
  collection: string;
  path: string;
  title: string | null;
  page: number | null;
  ts: number | null;
  snippet: string;
}

export type AskEvent =
  | { type: "step"; kind: "rewrite" | "search" | "read" | "model" | string; text: string; count?: number; ms?: number }
  | { type: "sources"; sources: AskSource[]; scope?: { collections: string[]; label: string; shortcut?: unknown } }
  | { type: "token"; text: string }
  | {
      type: "done";
      citations: number[];
      model: string;
      provider: string;
      where: "cloud" | "local";
      training?: boolean;
      fallback_reason?: string;
      redacted?: number;
      timings?: Record<string, number>;
      answer: string;
      answered: boolean;
      search_query?: string;
      rewrite?: string;
    }
  | { type: "error"; message: string };

export interface AskTurn {
  q: string;
  a: string;
}
