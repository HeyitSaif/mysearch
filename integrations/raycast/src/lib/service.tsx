/* Raycast glue shared by the commands: the client from preferences, folders and groups for the scope menu,
   the "service down" view, kind icons and opening the MySearch app. */

import {
  Action,
  ActionPanel,
  Clipboard,
  closeMainWindow,
  Color,
  getPreferenceValues,
  Icon,
  Image,
  Keyboard,
  open,
  showHUD,
  showToast,
  Toast,
} from "@raycast/api";
import { useCachedPromise } from "@raycast/utils";
import { execFile } from "node:child_process";
import { createClient, ServiceError, type Client } from "./client";
import { appLink, RESERVED, scopeTitle, type AppLink } from "./format";
import type { Collection, Scopes } from "./types";

export const APP_BUNDLE_ID = "com.mysearch.app";
export const RESTART_COMMAND = "mse service restart";
/** The dropdown value for the whole-Mac file-name search (the default scope of Search Files). */
export const WHOLE_MAC = "mac";

/** Opens the classic window (the old web UI) in MySearch.app: mysearch://classic (AppDelegate.openURL). */
export async function openClassicMySearch(): Promise<void> {
  try {
    await open("mysearch://classic");
  } catch {
    await showToast({
      style: Toast.Style.Failure,
      title: "Could not open Classic MySearch",
      message: "Is MySearch.app installed? scripts/build_app.sh builds it.",
    });
  }
}

export function getClient(): Client {
  const { dataDir } = getPreferenceValues<Preferences>();
  return createClient(dataDir);
}

export function asServiceError(e: unknown): ServiceError {
  if (e instanceof ServiceError) return e;
  return new ServiceError("http", e instanceof Error ? e.message : String(e));
}

export interface ScopeOption {
  value: string; // what the search `c` parameter takes
  title: string;
  keywords: string[];
}

function folderTitle(c: Collection): string {
  return scopeTitle(c.name, c.name, c.aliases ?? [], c.in_global ? "" : " (not in All)");
}

/** Groups and folders for the scope dropdown, cached across launches so the dropdown fills instantly.
    `resolve` turns a remembered scope that no longer exists (a removed folder or group) back into "all".
    `names` holds everything a search shortcut accepts ("co: …", "in:work"): names, aliases and "all". */
export function useScopes() {
  const r = useCachedPromise(async () => getClient().scopes(), [], {
    keepPreviousData: true,
    onError: () => undefined, // no toast: the search itself reports a service that is down
  });
  // a cache written by the first version of this extension holds a plain list of folders: ignore it
  const data: Scopes | undefined = r.data && !Array.isArray(r.data) ? r.data : undefined;
  const groups: ScopeOption[] = (data?.groups ?? []).map((g) => ({
    value: g.name,
    title: scopeTitle(g.label || g.name, g.name, g.aliases ?? []),
    keywords: [g.name, ...(g.aliases ?? []), ...g.collections],
  }));
  const folders: ScopeOption[] = (data?.collections ?? []).map((c) => ({
    value: c.name,
    title: folderTitle(c),
    keywords: [c.name, ...(c.aliases ?? [])],
  }));
  const known = data ? new Set(["all", ...groups.map((g) => g.value), ...folders.map((f) => f.value)]) : null;
  // "mac" (the whole-Mac name search) is not a folder: it is always kept
  const resolve = (scope: string) => (scope === WHOLE_MAC || !known || known.has(scope) ? scope : "all");
  const names = data
    ? new Set(
        [
          ...(known ?? []),
          ...[...(data.collections ?? []), ...(data.groups ?? [])].flatMap((x) => x.aliases ?? []),
        ].filter((n) => n === "all" || !RESERVED.has(n)),
      )
    : null;
  return { groups, folders, resolve, names, isLoading: r.isLoading };
}

/** Shows a result in the MySearch app: mysearch://search with the query, scope, document and page. */
export async function openInApp(link: AppLink): Promise<void> {
  try {
    await open(appLink(link));
    await closeMainWindow();
  } catch {
    await showToast({
      style: Toast.Style.Failure,
      title: "Could not open MySearch",
      message: "Is MySearch.app installed? scripts/build_app.sh builds it.",
    });
  }
}

export function kindIcon(kind: string): Image.ImageLike {
  switch (kind) {
    case "pdf":
      return { source: Icon.Document, tintColor: Color.Red };
    case "office":
      return { source: Icon.BarChart, tintColor: Color.Blue };
    case "text":
      return { source: Icon.Text, tintColor: Color.SecondaryText };
    case "email":
      return { source: Icon.Envelope, tintColor: Color.Orange };
    case "image":
      return { source: Icon.Image, tintColor: Color.Purple };
    case "audio":
      return { source: Icon.Microphone, tintColor: Color.Green };
    case "video":
      return { source: Icon.Video, tintColor: Color.Magenta };
    default:
      return { source: Icon.Document, tintColor: Color.SecondaryText };
  }
}

/** Opens MySearch.app wherever it is installed (it is found by its bundle id). */
export function openMySearchApp(): Promise<void> {
  return new Promise((resolve, reject) =>
    execFile("/usr/bin/open", ["-b", APP_BUNDLE_ID], (err) =>
      err ? reject(new Error("MySearch.app is not installed (scripts/build_app.sh builds it).")) : resolve(),
    ),
  );
}

export interface ErrorText {
  title: string;
  description: string;
  icon: Image.ImageLike;
}

export function errorText(e: ServiceError): ErrorText {
  switch (e.kind) {
    case "unreachable":
      return {
        title: "MySearch is not running",
        description: `Open the MySearch app, or run \`${RESTART_COMMAND}\` in a terminal. Then press ⌘R to try again.`,
        icon: { source: Icon.WifiDisabled, tintColor: Color.Red },
      };
    case "timeout":
      return {
        title: "MySearch is busy",
        description: `It did not answer in time. Try again in a moment; if it stays stuck, run \`${RESTART_COMMAND}\`.`,
        icon: { source: Icon.Clock, tintColor: Color.Orange },
      };
    case "unauthorized":
      return {
        title: "MySearch refused the access token",
        description:
          "The token in config.json no longer matches the running service. Restart it with " +
          `\`${RESTART_COMMAND}\`, or check the MySearch Data Folder in this extension's preferences.`,
        icon: { source: Icon.Lock, tintColor: Color.Red },
      };
    case "config":
      return {
        title: "MySearch is not set up",
        description: `${e.message} Install it (\`mse service install\`) or set the MySearch Data Folder in this extension's preferences.`,
        icon: { source: Icon.Gear, tintColor: Color.Orange },
      };
    default:
      return { title: "Search failed", description: e.message, icon: { source: Icon.Warning, tintColor: Color.Red } };
  }
}

/** Actions for the "service down" state. */
export function ServiceErrorActions({ onRetry }: { onRetry?: () => void }) {
  return (
    <ActionPanel>
      {onRetry ? (
        <Action
          title="Try Again"
          icon={Icon.ArrowClockwise}
          onAction={onRetry}
          shortcut={Keyboard.Shortcut.Common.Refresh}
        />
      ) : null}
      <Action
        title="Open MySearch App"
        icon={Icon.AppWindow}
        onAction={() => openMySearchApp().catch((err: Error) => showHUD(err.message))}
      />
      <Action
        title="Copy Restart Command"
        icon={Icon.Terminal}
        onAction={async () => {
          await Clipboard.copy(RESTART_COMMAND);
          await showHUD(`Copied: ${RESTART_COMMAND}`);
        }}
      />
    </ActionPanel>
  );
}
