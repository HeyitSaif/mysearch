/* A small client for the MySearch HTTP API on 127.0.0.1. Pure Node (global fetch): no Raycast imports.

   The service checks three things on every /api call (mse/server.py):
     Host    must be 127.0.0.1:<port> or localhost:<port>. fetch() derives it from the URL, so we always use 127.0.0.1.
     Token   the X-MSE-Token header (or ?t= for URLs that cannot carry headers, such as images in markdown).
     Origin  must be absent or the service's own origin. Node's fetch sends no Origin header. */

import { ConfigError, loadConfig, type ServiceConfig } from "./config";
import type {
  AskEvent,
  AskTurn,
  Collection,
  Group,
  NameIndexStatus,
  NameSearchResponse,
  Scopes,
  SearchResponse,
  Status,
  TreeResponse,
} from "./types";

/** "ask_disabled": Ask is switched off in MySearch (HTTP 409). Its own kind so the Ask command can explain it. */
export type ErrorKind = "unreachable" | "unauthorized" | "config" | "timeout" | "http" | "ask_disabled";

export interface NameParams {
  q: string;
  scope: "mac" | "indexed";
  /** Folders and groups for scope "indexed". Empty or ["all"] means every enabled folder. */
  collections?: string[];
  limit?: number;
}

export interface AskParams {
  q: string;
  /** Folders and groups to read; omitted means every enabled folder. */
  collections?: string[];
  history?: AskTurn[];
  /** "local" answers on this Mac for this question only. */
  route?: "auto" | "cloud" | "local";
}

export class ServiceError extends Error {
  constructor(
    readonly kind: ErrorKind,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ServiceError";
  }
}

export function isAbort(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

export interface SearchParams {
  q: string;
  scope?: string; // "all", a collection, a group, or a comma list of those
  types?: string[]; // pdf | office | text | email | image | audio | video
  limit?: number;
  precise?: boolean; // let the re-ranker re-check close calls (slower)
}

export interface Client {
  readonly config: ServiceConfig;
  search(p: SearchParams, signal?: AbortSignal): Promise<SearchResponse>;
  similar(collection: string, path: string, limit?: number, signal?: AbortSignal): Promise<SearchResponse>;
  collections(signal?: AbortSignal): Promise<Collection[]>;
  /** Folders and groups: everything the scope menu offers. Older services have no groups. */
  scopes(signal?: AbortSignal): Promise<Scopes>;
  status(signal?: AbortSignal): Promise<Status>;
  sync(collection?: string): Promise<{ jobs: { id: number; collection: string | null; status: string }[] }>;
  feedbackAdd(query: string, collection: string, path: string, kind?: "best" | "hide"): Promise<{ ok: boolean }>;
  feedbackRemove(query: string, collection: string, path: string): Promise<{ ok: boolean; removed: number }>;
  thumbUrl(collection: string, path: string, height?: number): string;
  /** Instant file names: the whole Mac (scope "mac") or the indexed folders (scope "indexed"). */
  nameSearch(p: NameParams, signal?: AbortSignal): Promise<NameSearchResponse>;
  /** Whole-Mac name index state: crawling (with progress), ready, disabled. */
  nameStatus(signal?: AbortSignal): Promise<NameIndexStatus>;
  /** Subfolders of one folder of a folder (the scope menu's folder tree, and "in:" paths). */
  tree(collection: string, path?: string, signal?: AbortSignal): Promise<TreeResponse>;
  /** Streams an Ask answer. Resolves when the stream ends; throws ServiceError("ask_disabled") on 409. */
  ask(p: AskParams, onEvent: (ev: AskEvent) => void, signal?: AbortSignal): Promise<void>;
}

const TIMEOUT_MS = 30_000;

function withTimeout(signal: AbortSignal | undefined, ms: number): AbortSignal {
  const t = AbortSignal.timeout(ms);
  return signal ? AbortSignal.any([signal, t]) : t;
}

export function createClient(dataDir?: string): Client {
  let cfg: ServiceConfig;
  try {
    cfg = loadConfig(dataDir);
  } catch (e) {
    if (e instanceof ConfigError) throw new ServiceError("config", e.message);
    throw e;
  }

  async function call<T>(method: "GET" | "POST", path: string, body?: unknown, signal?: AbortSignal, ms = TIMEOUT_MS) {
    let r: Response;
    try {
      r = await fetch(cfg.base + path, {
        method,
        headers: {
          "X-MSE-Token": cfg.token,
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: withTimeout(signal, ms),
      });
    } catch (e) {
      if (isAbort(e) && signal?.aborted) throw e; // superseded by a newer request: not an error
      if (e instanceof Error && e.name === "TimeoutError") {
        throw new ServiceError("timeout", "MySearch did not answer in time.");
      }
      throw new ServiceError("unreachable", "MySearch is not running.");
    }
    const j = (await r.json().catch(() => ({}))) as T & { error?: string };
    if (r.status === 401) throw new ServiceError("unauthorized", "MySearch refused the access token.", 401);
    if (!r.ok) throw new ServiceError("http", j.error || `${r.status} ${r.statusText}`, r.status);
    return j as T;
  }

  const qs = (o: Record<string, string | number | undefined | string[]>) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(o)) {
      if (v === undefined || v === "") continue;
      if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
      else p.set(k, String(v));
    }
    return p.toString();
  };

  return {
    config: cfg,
    search: (p, signal) =>
      call<SearchResponse>(
        "GET",
        "/api/search?" +
          qs({
            q: p.q,
            mode: "hybrid",
            limit: p.limit ?? 40,
            c: p.scope && p.scope !== "all" ? p.scope : undefined,
            type: p.types && p.types.length ? p.types : undefined,
            precise: p.precise ? "1" : undefined,
          }),
        undefined,
        signal,
      ),
    similar: (collection, path, limit = 40, signal) =>
      call<SearchResponse>("GET", "/api/similar?" + qs({ c: collection, path, limit }), undefined, signal),
    collections: async (signal) =>
      (await call<{ collections: Collection[] }>("GET", "/api/collections", undefined, signal)).collections,
    scopes: async (signal) => {
      const r = await call<{ collections: Collection[]; groups?: Group[] }>(
        "GET",
        "/api/collections",
        undefined,
        signal,
      );
      return { collections: r.collections ?? [], groups: r.groups ?? [] };
    },
    status: (signal) => call<Status>("GET", "/api/status", undefined, signal, 5_000),
    sync: (collection) => call("POST", "/api/sync", collection ? { collection, kind: "sync" } : { kind: "sync" }),
    feedbackAdd: (query, collection, path, kind = "best") =>
      call("POST", "/api/feedback", { query, collection, path, kind }),
    feedbackRemove: (query, collection, path) => call("POST", "/api/feedback/remove", { query, collection, path }),
    thumbUrl: (collection, path, height) =>
      `${cfg.base}/api/thumb?${qs({ c: collection, path, t: cfg.token, "raycast-height": height })}`,
    nameSearch: (p, signal) => {
      const scoped =
        p.scope === "indexed" && p.collections?.length && !(p.collections.length === 1 && p.collections[0] === "all");
      return call<NameSearchResponse>(
        "GET",
        "/api/files/search?" +
          qs({
            q: p.q,
            scope: p.scope,
            c: scoped ? p.collections : undefined,
            limit: p.limit ?? 200,
          }),
        undefined,
        signal,
      );
    },
    nameStatus: (signal) => call<NameIndexStatus>("GET", "/api/files/status", undefined, signal, 5_000),
    tree: (collection, path = "", signal) =>
      call<TreeResponse>("GET", "/api/tree?" + qs({ c: collection, path }), undefined, signal),
    ask: async (p, onEvent, signal) => {
      const body = {
        q: p.q,
        ...(p.collections?.length ? { collections: p.collections } : {}),
        ...(p.history?.length ? { history: p.history.slice(-3) } : {}),
        ...(p.route ? { route: p.route } : {}),
      };
      let r: Response;
      try {
        r = await fetch(cfg.base + "/api/ask", {
          method: "POST",
          headers: { "X-MSE-Token": cfg.token, "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal,
        });
      } catch (e) {
        if (isAbort(e) && signal?.aborted) throw e;
        throw new ServiceError("unreachable", "MySearch is not running.");
      }
      if (r.status === 401) throw new ServiceError("unauthorized", "MySearch refused the access token.", 401);
      if (r.status === 409) throw new ServiceError("ask_disabled", "Ask is switched off in MySearch.", 409);
      if (!r.ok || !r.body) {
        const j = (await r.json().catch(() => ({}))) as { error?: string };
        throw new ServiceError("http", j.error || `${r.status} ${r.statusText}`, r.status);
      }
      // NDJSON: one event per line. A line can be split across network chunks, so buffer until the newline.
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) onEvent(JSON.parse(line) as AskEvent);
        }
      }
      buf += dec.decode();
      if (buf.trim()) onEvent(JSON.parse(buf.trim()) as AskEvent);
    },
  };
}
