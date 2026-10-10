/* Menu-bar status of the MySearch service. Refreshed every minute in the background with a single
   GET /api/status (≈ 30 ms); the per-folder numbers (GET /api/collections, slower) are only fetched when the
   menu is opened. */

import {
  Clipboard,
  Color,
  environment,
  Icon,
  type Image,
  Keyboard,
  launchCommand,
  LaunchType,
  MenuBarExtra,
  openExtensionPreferences,
  showHUD,
} from "@raycast/api";
import { useCachedPromise } from "@raycast/utils";
import {
  asServiceError,
  errorText,
  getClient,
  openClassicMySearch,
  openMySearchApp,
  RESTART_COMMAND,
} from "./lib/service";
import type { Job, NameIndexStatus, Status, Worker } from "./lib/types";

const KIND: Record<string, string> = {
  scan: "Checking for changes",
  sync: "Syncing",
  embed: "Embedding",
  vocab: "Vocabulary",
  file: "Re-indexing a file",
  paths: "Indexing changed files",
};
const STAGES: Record<string, string> = {
  extract: "Reading files",
  "ocr & media": "OCR, page images, audio & video",
  "ocr & pages": "OCR, page images & photos",
  "audio & video": "Transcribing audio & video",
  "embedding text": "Embedding text",
  "embedding media": "Embedding images, pages & media",
  vocabulary: "Building translation vocabulary",
};
const WORKERS: [string, string, string][] = [
  ["text", "Search model", "sleeping, wakes in ~1 s when you search"],
  ["full", "Image, page & media model", "sleeping"],
  ["asr", "Speech to text", "sleeping"],
  ["rerank", "Re-ranker", "sleeping"],
];

const n = (x: number | null | undefined) => (x ?? 0).toLocaleString();
const mb = (x: number | null | undefined) => (x ? `${Math.round(x)} MB` : "—");

function jobLine(j: Job): string {
  const what = `${KIND[j.kind] || j.kind} ${j.collection ?? "all folders"}`;
  if (j.status !== "running") return what;
  const stage = j.stage ? STAGES[j.stage] || j.stage : "starting";
  const progress = j.total ? ` ${n(j.done)}/${n(j.total)}` : "";
  return `${what}: ${stage}${progress}`;
}

function progressIcon(j: Job | null | undefined): Image.ImageLike {
  if (!j) return Icon.MagnifyingGlass;
  if (!j.total) return Icon.CircleProgress;
  const pct = (j.done ?? 0) / j.total;
  return pct >= 0.75
    ? Icon.CircleProgress75
    : pct >= 0.5
      ? Icon.CircleProgress50
      : pct >= 0.25
        ? Icon.CircleProgress25
        : Icon.CircleProgress;
}

/** One line for the whole-Mac name index (GET /api/files/status). */
function wholeMacLine(nm: NameIndexStatus): { title: string; icon: Image.ImageLike } {
  const frac = nm.progress?.fraction;
  const pct = typeof frac === "number" ? Math.round(frac * 100) : null;
  if (nm.state === "disabled") return { title: "Whole-Mac search is off", icon: Icon.MinusCircle };
  if (nm.state === "crawling") {
    return {
      title: pct !== null ? `Indexing your Mac… ${pct}%` : "Indexing your Mac…",
      icon: { source: Icon.CircleProgress, tintColor: Color.Orange },
    };
  }
  if (nm.state === "ready")
    return { title: "Whole Mac is ready", icon: { source: Icon.CheckCircle, tintColor: Color.Green } };
  return { title: "Whole-Mac index not built yet", icon: Icon.Clock };
}

function workerLine(w: Worker | undefined, idle: string, unloadMin: number): string {
  if (!w) return "not available";
  return w.running ? `${mb(w.rss_mb)} · unloads after ${unloadMin} min idle` : idle;
}

async function syncNow() {
  try {
    const { jobs } = await getClient().sync();
    await showHUD(`Syncing ${jobs.length === 1 ? jobs[0].collection : `${jobs.length} folders`}`);
  } catch (e) {
    await showHUD(`MySearch: ${errorText(asServiceError(e)).title}`);
  }
}

export default function MySearchStatus() {
  const opened = environment.launchType === LaunchType.UserInitiated;
  const status = useCachedPromise(() => getClient().status(), [], { keepPreviousData: true });
  const colls = useCachedPromise(() => getClient().collections(), [], { execute: opened, keepPreviousData: true });
  // an older service without the whole-Mac index, or a stopped one, must not toast every minute: the menu says so
  const names = useCachedPromise(() => getClient().nameStatus(), [], {
    keepPreviousData: true,
    onError: () => undefined,
  });
  const nm: NameIndexStatus | undefined = names.error ? undefined : names.data;
  const nmPct = nm && typeof nm.progress?.fraction === "number" ? Math.round(nm.progress.fraction * 100) : null;

  const err = status.error ? asServiceError(status.error) : null;
  const st: Status | undefined = err ? undefined : status.data;
  const cur = st?.jobs?.current ?? null;
  const queued = st?.jobs?.queued ?? [];
  const pct = cur?.total ? Math.round((100 * (cur.done ?? 0)) / cur.total) : null;

  const icon: Image.ImageLike = err
    ? { source: Icon.MagnifyingGlass, tintColor: Color.Red }
    : cur
      ? progressIcon(cur)
      : Icon.MagnifyingGlass;
  const title = cur
    ? pct !== null
      ? `${pct}%`
      : "…"
    : queued.length
      ? `${queued.length} queued`
      : nm?.state === "crawling"
        ? nmPct !== null
          ? `Mac ${nmPct}%`
          : "Mac"
        : undefined;
  const tooltip = err ? `MySearch: ${errorText(err).title}` : cur ? `MySearch: ${jobLine(cur)}` : "MySearch: idle";

  return (
    <MenuBarExtra icon={icon} title={title} tooltip={tooltip} isLoading={status.isLoading || colls.isLoading}>
      {err ? (
        <MenuBarExtra.Section title={errorText(err).title}>
          <MenuBarExtra.Item
            title="Copy Restart Command"
            subtitle={RESTART_COMMAND}
            icon={Icon.Terminal}
            onAction={async () => {
              await Clipboard.copy(RESTART_COMMAND);
              await showHUD(`Copied: ${RESTART_COMMAND}`);
            }}
          />
          <MenuBarExtra.Item title="Try Again" icon={Icon.ArrowClockwise} onAction={() => status.revalidate()} />
        </MenuBarExtra.Section>
      ) : st ? (
        <>
          <MenuBarExtra.Section title="Indexing">
            <MenuBarExtra.Item
              icon={cur ? progressIcon(cur) : { source: Icon.CheckCircle, tintColor: Color.Green }}
              title={cur ? jobLine(cur) : "Idle"}
              subtitle={cur?.message || undefined}
            />
            {queued.slice(0, 5).map((j) => (
              <MenuBarExtra.Item key={j.id} icon={Icon.Clock} title={`Next: ${jobLine(j)}`} />
            ))}
            {queued.length > 5 ? <MenuBarExtra.Item title={`…and ${queued.length - 5} more`} /> : null}
            {st.jobs?.watcher ? (
              <MenuBarExtra.Item
                icon={Icon.Eye}
                title={st.jobs.watcher.running ? "Live updates on" : "Live updates off"}
                subtitle={
                  st.jobs.watcher.running
                    ? `${n(st.jobs.watcher.events)} changes seen${st.jobs.watcher.pending ? `, ${n(st.jobs.watcher.pending)} pending` : ""}`
                    : "checking every hour instead"
                }
              />
            ) : null}
            {st.jobs?.nightly ? (
              <MenuBarExtra.Item
                icon={Icon.Moon}
                title={st.jobs.nightly.enabled ? `Night run at ${st.jobs.nightly.at}` : "Night run off"}
                subtitle={st.jobs.nightly.last_run ? `last ${st.jobs.nightly.last_run}` : undefined}
              />
            ) : null}
          </MenuBarExtra.Section>
          {nm ? (
            <MenuBarExtra.Section title="Whole Mac">
              <MenuBarExtra.Item
                icon={wholeMacLine(nm).icon}
                title={wholeMacLine(nm).title}
                subtitle={nm.entries ? `${n(nm.entries)} names` : undefined}
              />
            </MenuBarExtra.Section>
          ) : null}
          <MenuBarExtra.Section title="Memory">
            <MenuBarExtra.Item
              icon={Icon.MemoryChip}
              title={`Service ${mb(st.daemon_rss_mb)}`}
              subtitle={`up ${Math.round(st.uptime_s / 60)} min · v${st.version}`}
            />
            {WORKERS.map(([key, label, idle]) =>
              key === "asr" && !st.asr_present ? null : key === "rerank" && !st.rerank_enabled ? null : (
                <MenuBarExtra.Item
                  key={key}
                  icon={st.workers[key]?.running ? { source: Icon.Bolt, tintColor: Color.Orange } : Icon.Moon}
                  title={label}
                  subtitle={workerLine(st.workers[key], idle, st.idle_unload_min)}
                />
              ),
            )}
          </MenuBarExtra.Section>
          {opened && colls.data?.length ? (
            <MenuBarExtra.Section title="Folders">
              {colls.data.map((c) => {
                const s = c.stats;
                const waiting = (s?.embed_pending ?? 0) + (s?.heavy_pending ?? 0);
                return (
                  <MenuBarExtra.Item
                    key={c.name}
                    icon={c.exists ? Icon.Folder : { source: Icon.Folder, tintColor: Color.Red }}
                    title={c.name}
                    subtitle={
                      s
                        ? `${n(s.files)} files${waiting ? ` · ${n(waiting)} waiting` : ""}${s.errors ? ` · ${n(s.errors)} unreadable` : ""}`
                        : "not indexed yet"
                    }
                  />
                );
              })}
            </MenuBarExtra.Section>
          ) : null}
        </>
      ) : null}
      <MenuBarExtra.Section>
        <MenuBarExtra.Item
          title="Search Files…"
          icon={Icon.MagnifyingGlass}
          shortcut={{ modifiers: ["cmd"], key: "f" }}
          onAction={() => launchCommand({ name: "search-files", type: LaunchType.UserInitiated })}
        />
        {err ? null : (
          <MenuBarExtra.Item
            title="Sync Now"
            icon={Icon.ArrowClockwise}
            shortcut={Keyboard.Shortcut.Common.Refresh}
            onAction={syncNow}
          />
        )}
        <MenuBarExtra.Item
          title="Open MySearch"
          icon={Icon.AppWindow}
          shortcut={Keyboard.Shortcut.Common.Open}
          onAction={() => openMySearchApp().catch((e: Error) => showHUD(e.message))}
        />
        <MenuBarExtra.Item title="Open Classic MySearch" icon={Icon.Window} onAction={() => openClassicMySearch()} />
        <MenuBarExtra.Item title="Preferences…" icon={Icon.Gear} onAction={openExtensionPreferences} />
      </MenuBarExtra.Section>
    </MenuBarExtra>
  );
}
