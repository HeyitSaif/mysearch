/* Where the MySearch service listens and the token it expects. Read from the service's own config.json at run
   time, so the token never lives in this extension or in Raycast's preferences. Pure Node: no Raycast imports. */

import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_DATA_DIR = join(homedir(), "Library", "Application Support", "MySearch");

export interface ServiceConfig {
  base: string; // http://127.0.0.1:<port>
  port: number;
  token: string;
  dataDir: string;
}

export class ConfigError extends Error {
  constructor(
    message: string,
    readonly configPath: string,
  ) {
    super(message);
    this.name = "ConfigError";
  }
}

export function resolveDataDir(dir?: string): string {
  const d = (dir || "").trim();
  if (!d) return DEFAULT_DATA_DIR;
  return d.startsWith("~/") ? join(homedir(), d.slice(2)) : d;
}

let cached: { path: string; mtimeMs: number; cfg: ServiceConfig } | null = null;

/** Reads <dataDir>/config.json (cached until the file changes). Throws ConfigError when it is missing or unusable. */
export function loadConfig(dataDir?: string): ServiceConfig {
  const dir = resolveDataDir(dataDir);
  const path = join(dir, "config.json");
  let mtimeMs: number;
  try {
    mtimeMs = statSync(path).mtimeMs;
  } catch {
    throw new ConfigError("MySearch is not set up on this Mac (no config.json found).", path);
  }
  if (cached && cached.path === path && cached.mtimeMs === mtimeMs) return cached.cfg;
  let raw: { port?: unknown; token?: unknown };
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    throw new ConfigError("MySearch's config.json could not be read.", path);
  }
  const port = Number(raw.port);
  const token = typeof raw.token === "string" ? raw.token : "";
  if (!Number.isInteger(port) || port <= 0 || port > 65535 || !token) {
    throw new ConfigError("MySearch's config.json has no port or token.", path);
  }
  const cfg = { base: `http://127.0.0.1:${port}`, port, token, dataDir: dir };
  cached = { path, mtimeMs, cfg };
  return cfg;
}
