import { Action, ActionPanel, Icon, List, showHUD } from "@raycast/api";
import { useCachedState, usePromise } from "@raycast/utils";
import { useMemo, useState } from "react";
import type { Client } from "./lib/client";
import { displayTitle, inFolderToken, sectionize, stripIn, TYPE_FILTERS, unknownInNames } from "./lib/format";
import { NameItemView } from "./lib/names";
import { ResultItem } from "./lib/results";
import {
  asServiceError,
  errorText,
  getClient,
  openClassicMySearch,
  openMySearchApp,
  ServiceErrorActions,
  useScopes,
  WHOLE_MAC,
} from "./lib/service";
import { useNameSearch } from "./lib/useNames";
import { MIN_CHARS, useSearch } from "./lib/useSearch";
import type { SearchResult } from "./lib/types";

function useClient(): Client | null {
  return useMemo(() => {
    try {
      return getClient();
    } catch {
      return null; // reported by the search itself
    }
  }, []);
}

function TypeFilterMenu({ kind, setKind }: { kind: string; setKind: (k: string) => void }) {
  return (
    <ActionPanel.Submenu title="Filter by Type" icon={Icon.Filter} shortcut={{ modifiers: ["cmd"], key: "t" }}>
      <Action title="All Types" icon={kind ? Icon.Circle : Icon.CheckCircle} onAction={() => setKind("")} />
      {TYPE_FILTERS.map((t) => (
        <Action
          key={t.value}
          title={t.title}
          icon={kind === t.value ? Icon.CheckCircle : Icon.Circle}
          onAction={() => setKind(t.value)}
        />
      ))}
    </ActionPanel.Submenu>
  );
}

export default function SearchFiles() {
  const [text, setText] = useState("");
  const [chosenScope, setScope] = useState(WHOLE_MAC);
  const [kind, setKind] = useState("");
  const [showDetail, setShowDetail] = useCachedState("search-files.show-detail", true);
  const client = useClient();
  const scopes = useScopes();
  const scope = scopes.resolve(chosenScope); // a remembered folder or group that is gone falls back to Whole Mac
  const isMac = scope === WHOLE_MAC;
  const scopeLabel = [...scopes.groups, ...scopes.folders].find((o) => o.value === scope)?.title ?? scope;
  // "invoice in:wo" while typing: wait for a name the service knows instead of showing an error
  const pendingIn = scopes.names ? unknownInNames(text, scopes.names) : [];
  const query = pendingIn.length ? "" : text;

  // instant file names: the whole Mac, or the names in the chosen folders and groups
  const names = useNameSearch(
    query,
    isMac ? { scope: "mac" } : { scope: "indexed", collections: scope === "all" ? [] : [scope] },
    kind,
  );
  // the whole-Mac index is still building: show the indexed folders' content until it is ready
  const fallback = isMac && names.response?.searchable === false;
  const content = useSearch(isMac && !fallback ? "" : query, fallback ? "all" : scope, kind ? [kind] : []);

  const nameItems = names.response?.items ?? [];
  const results = content.response?.results ?? [];
  const hasNames = nameItems.length > 0;
  const sections = sectionize(results).map((sec) =>
    hasNames && !isMac ? { ...sec, title: `Content matches · ${sec.title}` } : sec,
  );
  // where the content search ran: the dropdown, or a shortcut in the query ("work: invoice") that overrides it
  const where = content.response?.scope?.label;
  const viaShortcut = !!content.response?.scope?.shortcut;
  const kindTitle = TYPE_FILTERS.find((t) => t.value === kind)?.title;
  const nameStats = names.response
    ? `${names.response.total.toLocaleString()} ${isMac ? "matches" : "names"} · ${names.response.ms} ms`
    : undefined;
  const contentStats = content.response
    ? `${content.response.total.toLocaleString()} matches${where ? ` in ${where}` : ""} · ${content.response.timings.total_ms} ms${content.refined ? " · re-ranked" : ""}`
    : undefined;
  const answered = !!(names.response || content.response);
  const nothing = !names.isLoading && !content.isLoading && answered && !hasNames && !results.length;

  // "Search Only in This Folder": the same words, in:"<folder>" in place of any folder shortcut
  const searchOnly = (folder: string) => setText([stripIn(text), inFolderToken(folder)].filter(Boolean).join(" "));

  const viewActions = (
    <>
      <Action title="Open Classic MySearch" icon={Icon.Window} onAction={() => openClassicMySearch()} />
      <TypeFilterMenu kind={kind} setKind={setKind} />
      <Action
        title={showDetail ? "Hide Details" : "Show Details"}
        icon={Icon.Sidebar}
        shortcut={{ modifiers: ["cmd", "shift"], key: "d" }}
        onAction={() => setShowDetail(!showDetail)}
      />
    </>
  );

  const err = names.error ?? content.error;
  const retry = () => {
    names.revalidate();
    content.revalidate();
  };

  return (
    <List
      isLoading={names.isLoading || content.isLoading}
      filtering={false}
      searchText={text}
      onSearchTextChange={setText}
      searchBarPlaceholder={kindTitle ? `Search ${kindTitle}…` : "Search your files: a name, a question, a number…"}
      isShowingDetail={showDetail && (hasNames || results.length > 0)}
      navigationTitle={[
        "MySearch",
        isMac ? "Whole Mac" : where && (viaShortcut || scope !== "all") ? `in ${where}` : "",
        kindTitle ?? "",
      ]
        .filter(Boolean)
        .join(" · ")}
      searchBarAccessory={
        <List.Dropdown tooltip="Search in" storeValue onChange={setScope}>
          <List.Dropdown.Item
            title="Whole Mac"
            value={WHOLE_MAC}
            icon={Icon.HardDrive}
            keywords={["mac", "everything", "all files", "names"]}
          />
          <List.Dropdown.Item
            title="All Indexed Folders"
            value="all"
            icon={Icon.Folder}
            keywords={["all", "indexed", "content"]}
          />
          {scopes.groups.length ? (
            <List.Dropdown.Section title="Groups">
              {scopes.groups.map((g) => (
                <List.Dropdown.Item
                  key={g.value}
                  title={g.title}
                  value={g.value}
                  keywords={g.keywords}
                  icon={Icon.Layers}
                />
              ))}
            </List.Dropdown.Section>
          ) : null}
          <List.Dropdown.Section title="Folders">
            {scopes.folders.map((f) => (
              <List.Dropdown.Item
                key={f.value}
                title={f.title}
                value={f.value}
                keywords={f.keywords}
                icon={Icon.Folder}
              />
            ))}
          </List.Dropdown.Section>
        </List.Dropdown>
      }
    >
      {err ? (
        <List.EmptyView {...errorText(err)} actions={<ServiceErrorActions onRetry={retry} />} />
      ) : pendingIn.length ? (
        <List.EmptyView
          icon={Icon.Folder}
          title={`No folder or group is called “${pendingIn.join("”, “")}”`}
          description={`After in: type one of: ${[...(scopes.names ?? [])].sort().join(", ")}.`}
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : text.trim().length < MIN_CHARS ? (
        <List.EmptyView
          icon={{ source: "icon.png" }}
          title="Search your files"
          description={
            "Whole Mac finds file names everywhere on this Mac as you type. All Indexed Folders also finds the words inside documents, scans, mail and media, in English and German.\n" +
            "Narrow it down with the folder menu, ⌘T for a file type, or words like “last month”.\n" +
            "Type “work: invoice” or “invoice in:work” to search one folder or group (its name or short name), " +
            "whatever the menu says."
          }
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : null}
      {fallback ? (
        <List.Section title="Whole Mac">
          <List.Item
            icon={{ source: Icon.CircleProgress }}
            title={names.percent !== null ? `Indexing your Mac… ${names.percent}%` : "Indexing your Mac…"}
            subtitle="Showing the content of your indexed folders until it is ready"
            actions={
              <ActionPanel>
                <Action
                  title="Open MySearch App"
                  icon={Icon.AppWindow}
                  onAction={() => openMySearchApp().catch((e: Error) => showHUD(e.message))}
                />
              </ActionPanel>
            }
          />
        </List.Section>
      ) : null}
      {nothing ? (
        <List.EmptyView
          icon={Icon.MagnifyingGlass}
          title="Nothing found"
          description={
            kindTitle
              ? `No ${kindTitle.toLowerCase()} match. Press ⌘T and choose All Types, or try other words.`
              : isMac
                ? "No file name on this Mac matches. Try other words, or choose All Indexed Folders to search inside documents."
                : viaShortcut
                  ? `Nothing in ${where}. Take the shortcut out of the search to use the folder menu, or try other words.`
                  : scope === "all"
                    ? "Try other words or another language. Files waiting for the night run (OCR, audio, video) are not searchable by content yet."
                    : `Nothing in ${where ?? scopeLabel}. Try All Indexed Folders, or other words.`
          }
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : null}
      {hasNames ? (
        <List.Section title={isMac ? "Whole Mac" : "File names"} subtitle={nameStats}>
          {nameItems.map((item) => (
            <NameItemView
              key={item.path}
              item={item}
              showDetail={showDetail}
              query={query}
              scope={scope}
              viewActions={viewActions}
              onSearchFolder={searchOnly}
            />
          ))}
        </List.Section>
      ) : null}
      {sections.map((sec, i) => (
        <List.Section
          key={sec.title}
          title={sec.title}
          subtitle={!hasNames && i === 0 && contentStats ? contentStats : sec.subtitle}
        >
          {sec.results.map((r: SearchResult) => (
            <ResultItem
              key={`${r.collection}/${r.path}`}
              r={r}
              client={client}
              showDetail={showDetail}
              bridge={content.response?.bridge?.[r.collection]}
              query={content.query}
              scope={fallback ? "all" : scope}
              onChanged={content.revalidate}
              onSearchFolder={searchOnly}
              similar={<SimilarList r={r} />}
              viewActions={viewActions}
            />
          ))}
        </List.Section>
      ))}
    </List>
  );
}

/** Documents like this one (by meaning, or by picture for images and scans). */
export function SimilarList({ r }: { r: SearchResult }) {
  const client = useClient();
  const [showDetail, setShowDetail] = useCachedState("search-files.show-detail", true);
  const { data, isLoading, error, revalidate } = usePromise(
    async (collection: string, path: string) => getClient().similar(collection, path, 40),
    [r.collection, r.path],
    { failureToastOptions: { title: "Could not find similar files" } },
  );
  const results = data?.results ?? [];
  const err = error ? asServiceError(error) : null;
  return (
    <List
      isLoading={isLoading}
      navigationTitle={`Similar to ${displayTitle(r)}`}
      searchBarPlaceholder="Filter similar files"
      isShowingDetail={showDetail && results.length > 0}
    >
      {err ? (
        <List.EmptyView {...errorText(err)} actions={<ServiceErrorActions onRetry={revalidate} />} />
      ) : !isLoading && !results.length ? (
        <List.EmptyView
          icon={Icon.MagnifyingGlass}
          title="Nothing similar"
          description={data?.note ? `${data.note}.` : "No other document is close enough."}
        />
      ) : null}
      <List.Section title={`Similar to ${r.name}`} subtitle={results.length ? String(results.length) : undefined}>
        {results.map((x) => (
          <ResultItem
            key={`${x.collection}/${x.path}`}
            r={x}
            client={client}
            showDetail={showDetail}
            similar={<SimilarList r={x} />}
            viewActions={
              <Action
                title={showDetail ? "Hide Details" : "Show Details"}
                icon={Icon.Sidebar}
                shortcut={{ modifiers: ["cmd", "shift"], key: "d" }}
                onAction={() => setShowDetail(!showDetail)}
              />
            }
          />
        ))}
      </List.Section>
    </List>
  );
}
