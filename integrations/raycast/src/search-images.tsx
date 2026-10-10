import { Action, ActionPanel, Color, Grid, Icon } from "@raycast/api";
import { usePromise } from "@raycast/utils";
import { useMemo, useState, type ReactNode } from "react";
import type { Client } from "./lib/client";
import { displayTitle, formatDate, resultId, sectionize, unknownInNames } from "./lib/format";
import { ResultActions } from "./lib/results";
import { asServiceError, errorText, getClient, ServiceErrorActions, useScopes } from "./lib/service";
import { MIN_CHARS, useSearch } from "./lib/useSearch";
import type { SearchResult } from "./lib/types";

type What = "both" | "image" | "pdf";
const WHAT: { value: What; title: string; types: string[] }[] = [
  { value: "both", title: "Pictures & Scanned Pages", types: ["image", "pdf"] },
  { value: "image", title: "Pictures Only", types: ["image"] },
  { value: "pdf", title: "PDF Pages Only", types: ["pdf"] },
];

/** A grid shows pictures: every image (a file icon until its thumbnail exists), and PDFs that have a page image. */
const showable = (r: SearchResult) => r.kind === "image" || (r.thumb && !r.metadata_only);

function useClient(): Client | null {
  return useMemo(() => {
    try {
      return getClient();
    } catch {
      return null;
    }
  }, []);
}

function GridItem({
  r,
  client,
  query,
  scope,
  onChanged,
  viewActions,
}: {
  r: SearchResult;
  client: Client | null;
  query?: string;
  scope?: string;
  onChanged?: () => void;
  viewActions?: ReactNode;
}) {
  const content =
    r.thumb && !r.metadata_only && client
      ? { source: client.thumbUrl(r.collection, r.path), fallback: Icon.Image }
      : { fileIcon: r.abs_path };
  return (
    <Grid.Item
      id={resultId(r)}
      content={content}
      title={displayTitle(r)}
      subtitle={[formatDate(r.date), r.collection].filter(Boolean).join(" · ")}
      quickLook={{ path: r.abs_path, name: displayTitle(r) }}
      accessory={
        r.favorite === "query"
          ? { icon: { source: Icon.Star, tintColor: Color.Yellow }, tooltip: "Favourite for this search" }
          : r.kind === "pdf"
            ? { icon: { source: Icon.Document, tintColor: Color.Red }, tooltip: "PDF page" }
            : undefined
      }
      actions={
        <ResultActions
          r={r}
          client={client}
          query={query}
          scope={scope}
          onChanged={onChanged}
          similar={<SimilarGrid r={r} />}
          viewActions={viewActions}
        />
      }
    />
  );
}

export default function SearchImages() {
  const [text, setText] = useState("");
  const [chosenScope, setScope] = useState("all");
  const [what, setWhat] = useState<What>("both");
  const client = useClient();
  const scopes = useScopes();
  const scope = scopes.resolve(chosenScope); // a remembered folder or group that is gone falls back to All
  const types = WHAT.find((w) => w.value === what)!.types;
  const pendingIn = scopes.names ? unknownInNames(text, scopes.names) : []; // typing "in:wo": wait for a known name
  const s = useSearch(pendingIn.length ? "" : text, scope, types);
  const where = s.response?.scope?.label; // the dropdown, or a shortcut in the query ("work: receipt")
  const viaShortcut = !!s.response?.scope?.shortcut;
  const results = (s.response?.results ?? []).filter(showable);
  const sections = sectionize(results, 5);
  const whatTitle = WHAT.find((w) => w.value === what)!.title;

  const viewActions = (
    <ActionPanel.Submenu title="Show" icon={Icon.Filter} shortcut={{ modifiers: ["cmd"], key: "t" }}>
      {WHAT.map((w) => (
        <Action
          key={w.value}
          title={w.title}
          icon={what === w.value ? Icon.CheckCircle : Icon.Circle}
          onAction={() => setWhat(w.value)}
        />
      ))}
    </ActionPanel.Submenu>
  );

  return (
    <Grid
      columns={5}
      aspectRatio="1"
      fit={Grid.Fit.Contain}
      inset={Grid.Inset.Small}
      isLoading={s.isLoading}
      filtering={false}
      onSearchTextChange={setText}
      navigationTitle={["MySearch", where && (viaShortcut || scope !== "all") ? `in ${where}` : "", whatTitle]
        .filter(Boolean)
        .join(" · ")}
      searchBarPlaceholder="Describe a picture or a scanned page: “receipt”, “whiteboard”, “Rechnung”…"
      searchBarAccessory={
        <Grid.Dropdown tooltip="Search in" storeValue onChange={setScope}>
          <Grid.Dropdown.Item title="All Folders" value="all" icon={Icon.HardDrive} />
          {scopes.groups.length ? (
            <Grid.Dropdown.Section title="Groups">
              {scopes.groups.map((g) => (
                <Grid.Dropdown.Item
                  key={g.value}
                  title={g.title}
                  value={g.value}
                  keywords={g.keywords}
                  icon={Icon.Layers}
                />
              ))}
            </Grid.Dropdown.Section>
          ) : null}
          <Grid.Dropdown.Section title="Folders">
            {scopes.folders.map((f) => (
              <Grid.Dropdown.Item
                key={f.value}
                title={f.title}
                value={f.value}
                keywords={f.keywords}
                icon={Icon.Folder}
              />
            ))}
          </Grid.Dropdown.Section>
        </Grid.Dropdown>
      }
    >
      {s.error ? (
        <Grid.EmptyView {...errorText(s.error)} actions={<ServiceErrorActions onRetry={s.revalidate} />} />
      ) : pendingIn.length ? (
        <Grid.EmptyView
          icon={Icon.Folder}
          title={`No folder or group is called “${pendingIn.join("”, “")}”`}
          description={`After in: type one of: ${[...(scopes.names ?? [])].sort().join(", ")}.`}
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : text.trim().length < MIN_CHARS ? (
        <Grid.EmptyView
          icon={{ source: "icon.png" }}
          title="Search pictures and scanned pages"
          description="Photos, screenshots and scans are matched by what they show and by the text in them (OCR). ⌘T switches between pictures and PDF pages. “work: receipt” or “receipt in:work” searches one folder or group."
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : !s.isLoading && s.response && !results.length ? (
        <Grid.EmptyView
          icon={Icon.Image}
          title={where && (viaShortcut || scope !== "all") ? `No pictures found in ${where}` : "No pictures found"}
          description="Try other words. New scans and photos get their picture index within minutes; large batches wait for the night run."
          actions={<ActionPanel>{viewActions}</ActionPanel>}
        />
      ) : null}
      {sections.map((sec, i) => (
        <Grid.Section
          key={sec.title}
          title={sec.title}
          subtitle={i === 0 && where ? [sec.subtitle, `in ${where}`].filter(Boolean).join(" · ") : sec.subtitle}
        >
          {sec.results.map((r) => (
            <GridItem
              key={`${r.collection}/${r.path}`}
              r={r}
              client={client}
              query={s.query}
              scope={scope}
              onChanged={s.revalidate}
              viewActions={viewActions}
            />
          ))}
        </Grid.Section>
      ))}
    </Grid>
  );
}

/** Pictures that look like this one (MySearch compares images by their picture embedding). */
export function SimilarGrid({ r }: { r: SearchResult }) {
  const client = useClient();
  const { data, isLoading, error, revalidate } = usePromise(
    async (collection: string, path: string) => getClient().similar(collection, path, 40),
    [r.collection, r.path],
    { failureToastOptions: { title: "Could not find similar pictures" } },
  );
  const results = (data?.results ?? []).filter(showable);
  const err = error ? asServiceError(error) : null;
  return (
    <Grid
      columns={5}
      aspectRatio="1"
      fit={Grid.Fit.Contain}
      inset={Grid.Inset.Small}
      isLoading={isLoading}
      navigationTitle={`Similar to ${displayTitle(r)}`}
      searchBarPlaceholder="Filter similar pictures"
    >
      {err ? (
        <Grid.EmptyView {...errorText(err)} actions={<ServiceErrorActions onRetry={revalidate} />} />
      ) : !isLoading && !results.length ? (
        <Grid.EmptyView
          icon={Icon.Image}
          title="Nothing similar"
          description={data?.note ? `${data.note}.` : "No other picture is close enough."}
        />
      ) : null}
      <Grid.Section title={`Similar to ${r.name}`}>
        {results.map((x) => (
          <GridItem key={`${x.collection}/${x.path}`} r={x} client={client} />
        ))}
      </Grid.Section>
    </Grid>
  );
}
