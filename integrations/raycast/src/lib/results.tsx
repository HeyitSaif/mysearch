/* One search result in Raycast: the list row, its detail pane, and its actions. */

import {
  Action,
  ActionPanel,
  closeMainWindow,
  Color,
  Icon,
  Keyboard,
  List,
  open,
  showToast,
  Toast,
  type Image,
} from "@raycast/api";
import { existsSync } from "node:fs";
import type { ReactElement, ReactNode } from "react";
import type { Client } from "./client";
import {
  citation,
  clock,
  displayTitle,
  folderOf,
  formatDate,
  formatSize,
  KIND_LABEL,
  resultId,
  snippetToMarkdown,
  topHit,
  whereOf,
} from "./format";
import { asServiceError, kindIcon, openInApp } from "./service";
import type { SearchResult } from "./types";

const WHY_LABEL: Record<string, string> = {
  keyword: "words",
  name: "file name",
  meaning: "meaning",
  translated: "translation",
  visual: "picture",
  audio: "sound",
  favourite: "favourite",
};

export function kindLabel(r: SearchResult): string {
  const k = KIND_LABEL[r.kind] || r.kind;
  return r.ext && r.ext.toUpperCase() !== k.toUpperCase() ? `${k} · ${r.ext.toUpperCase()}` : k;
}

export function favouriteIcon(r: SearchResult): Image.ImageLike | undefined {
  if (r.favorite === "query") return { source: Icon.Star, tintColor: Color.Yellow };
  if (r.favorite === "similar") return { source: Icon.Star, tintColor: Color.SecondaryText };
  return undefined;
}

function accessories(r: SearchResult): List.Item.Accessory[] {
  const out: List.Item.Accessory[] = [];
  const fav = favouriteIcon(r);
  if (fav) {
    out.push({
      icon: fav,
      tooltip: r.favorite === "query" ? "Favourite for this search" : "Favourite for a similar search",
    });
  }
  if (r.metadata_only) out.push({ icon: Icon.Lock, tooltip: "File name only: contents are not indexed" });
  const where = whereOf(topHit(r));
  if (where) out.push({ tag: where, tooltip: "Where the best passage is" });
  if (r.copies.length)
    out.push({ icon: Icon.Layers, text: String(r.copies.length + 1), tooltip: "Versions of this document" });
  out.push({ icon: kindIcon(r.kind), tooltip: kindLabel(r) });
  if (r.date) out.push({ text: formatDate(r.date), tooltip: `Document date ${r.date}` });
  return out;
}

/** The detail pane: thumbnail, the matching passages, and the facts about the file. */
export function detailMarkdown(r: SearchResult, client: Client | null): string {
  const md: string[] = [];
  if (r.thumb && !r.metadata_only && client) md.push(`![](${client.thumbUrl(r.collection, r.path, 190)})`);
  if (r.metadata_only) {
    md.push("_File name only. This folder's rules keep the contents of this file out of the index._");
  } else {
    const hits = r.hits.filter((h) => h.snippet);
    for (const h of hits.slice(0, 3)) {
      const where = whereOf(h, true);
      md.push(`> ${where ? `**${where}** · ` : ""}${snippetToMarkdown(h.snippet)}`);
    }
    if (!hits.length) {
      const h = r.hits[0];
      const how = h ? h.why.map((w) => WHY_LABEL[w] || w).join(" + ") || h.modality : "";
      md.push(
        `_Matched by ${how || "relevance"}${whereOf(h) ? `, ${whereOf(h, true)}` : ""}. No text passage to show._`,
      );
    }
  }
  if (r.context) md.push(`_${r.context.replace(/[_*`]/g, "")}_`);
  return md.join("\n\n");
}

export function ResultDetail({ r, client, bridge }: { r: SearchResult; client: Client | null; bridge?: string[] }) {
  const h = topHit(r);
  const where = whereOf(h, true);
  return (
    <List.Item.Detail
      markdown={detailMarkdown(r, client)}
      metadata={
        <List.Item.Detail.Metadata>
          <List.Item.Detail.Metadata.Label title="Folder" text={r.collection} icon={Icon.HardDrive} />
          <List.Item.Detail.Metadata.Label title="Path" text={folderOf(r.path) || "(top level)"} icon={Icon.Folder} />
          <List.Item.Detail.Metadata.Label title="Kind" text={kindLabel(r)} icon={kindIcon(r.kind)} />
          {r.date ? <List.Item.Detail.Metadata.Label title="Date" text={formatDate(r.date)} /> : null}
          {where ? <List.Item.Detail.Metadata.Label title={h?.t0 != null ? "Time" : "Page"} text={where} /> : null}
          {r.pages ? <List.Item.Detail.Metadata.Label title="Pages" text={String(r.pages)} /> : null}
          {r.duration ? <List.Item.Detail.Metadata.Label title="Length" text={clock(r.duration)} /> : null}
          {r.size ? <List.Item.Detail.Metadata.Label title="Size" text={formatSize(r.size)} /> : null}
          {r.copies.length ? (
            <List.Item.Detail.Metadata.Label
              title="Versions"
              text={`${r.copies.length + 1} (copies, other formats or digests folded into this one)`}
            />
          ) : null}
          <List.Item.Detail.Metadata.Separator />
          <List.Item.Detail.Metadata.TagList title="Matched By">
            {r.why.map((w) => (
              <List.Item.Detail.Metadata.TagList.Item key={w} text={WHY_LABEL[w] || w} />
            ))}
          </List.Item.Detail.Metadata.TagList>
          {bridge && bridge.length ? (
            <List.Item.Detail.Metadata.TagList title="Also Searched">
              {bridge.slice(0, 6).map((b) => (
                <List.Item.Detail.Metadata.TagList.Item key={b} text={b} color={Color.Purple} />
              ))}
            </List.Item.Detail.Metadata.TagList>
          ) : null}
          <List.Item.Detail.Metadata.Label title="Relevance" text={`${r.relevance}%`} />
          {r.favorite ? (
            <List.Item.Detail.Metadata.Label
              title="Favourite"
              text={r.favorite === "query" ? "For this search" : "For a similar search"}
              icon={favouriteIcon(r)}
            />
          ) : null}
          <List.Item.Detail.Metadata.Label title="Document ID" text={`#${r.docid}`} />
        </List.Item.Detail.Metadata>
      }
    />
  );
}

async function guardExists(path: string): Promise<boolean> {
  if (existsSync(path)) return true;
  await showToast({
    style: Toast.Style.Failure,
    title: "File not found",
    message: "It was moved or deleted since it was indexed.",
  });
  return false;
}

function FavouriteAction({
  r,
  query,
  client,
  onChanged,
}: {
  r: SearchResult;
  query?: string;
  client: Client | null;
  onChanged?: () => void;
}) {
  // Only services with the favourites API return a `favorite` field; only searches have a query to attach it to.
  if (!client || !query || !("favorite" in r)) return null;
  const marked = r.favorite === "query";
  return (
    <Action
      title={marked ? "Remove Favourite" : "Mark as Favourite"}
      icon={marked ? Icon.StarDisabled : Icon.Star}
      shortcut={Keyboard.Shortcut.Common.Pin}
      onAction={async () => {
        const toast = await showToast({ style: Toast.Style.Animated, title: marked ? "Removing…" : "Marking…" });
        try {
          if (marked) await client.feedbackRemove(query, r.collection, r.path);
          else await client.feedbackAdd(query, r.collection, r.path, "best");
          toast.style = Toast.Style.Success;
          toast.title = marked ? "Favourite removed" : "Marked as favourite";
          toast.message = marked ? undefined : `It comes first when you search “${query}” again.`;
          onChanged?.();
        } catch (e) {
          const err = asServiceError(e);
          toast.style = Toast.Style.Failure;
          toast.title =
            err.status === 404 && /endpoint/.test(err.message)
              ? "This MySearch has no favourites yet"
              : "Could not save";
          toast.message = err.message;
        }
      }}
    />
  );
}

export interface ResultActionsProps {
  r: SearchResult;
  client: Client | null;
  query?: string;
  /** The scope the result was found in ("all", a folder or a group), passed on to the app. */
  scope?: string;
  onChanged?: () => void;
  /** The "Find Similar" view to push (a List for files, a Grid for pictures). */
  similar?: ReactElement;
  /** View options of the hosting command (type filter, detail toggle). */
  viewActions?: ReactNode;
  /** Re-queries the command with `in:"<folder>"` (the folder of this result, absolute path). */
  onSearchFolder?: (folder: string) => void;
}

export function ResultActions({
  r,
  client,
  query,
  scope,
  onChanged,
  similar,
  viewActions,
  onSearchFolder,
}: ResultActionsProps) {
  return (
    <ActionPanel title={displayTitle(r)}>
      <ActionPanel.Section>
        <Action
          title="Open"
          icon={Icon.ArrowNe}
          onAction={async () => {
            if (!(await guardExists(r.abs_path))) return;
            await open(r.abs_path);
            await closeMainWindow();
          }}
        />
        <Action.ShowInFinder path={r.abs_path} />
        <Action
          title="Open in MySearch"
          icon={Icon.AppWindowSidebarRight}
          shortcut={Keyboard.Shortcut.Common.Open}
          onAction={() => openInApp({ q: query, scope, docid: r.docid, page: topHit(r)?.page })}
        />
        <Action.ToggleQuickLook shortcut={Keyboard.Shortcut.Common.ToggleQuickLook} />
      </ActionPanel.Section>
      <ActionPanel.Section>
        <Action.CopyToClipboard
          title="Copy Citation for AI"
          content={citation(r)}
          shortcut={Keyboard.Shortcut.Common.Copy}
        />
        <Action.CopyToClipboard title="Copy Path" content={r.abs_path} shortcut={Keyboard.Shortcut.Common.CopyPath} />
      </ActionPanel.Section>
      <ActionPanel.Section>
        {onSearchFolder ? (
          <Action
            title="Search Only in This Folder"
            icon={Icon.MagnifyingGlass}
            shortcut={{ modifiers: ["cmd", "alt"], key: "f" }}
            onAction={() => onSearchFolder(r.abs_path.slice(0, r.abs_path.lastIndexOf("/")) || "/")}
          />
        ) : null}
        {similar && !r.metadata_only ? (
          <Action.Push
            title="Find Similar"
            icon={Icon.MagnifyingGlass}
            target={similar}
            shortcut={{ modifiers: ["cmd", "shift"], key: "f" }}
          />
        ) : null}
        <FavouriteAction r={r} query={query} client={client} onChanged={onChanged} />
      </ActionPanel.Section>
      {viewActions ? <ActionPanel.Section>{viewActions}</ActionPanel.Section> : null}
    </ActionPanel>
  );
}

export function ResultItem({
  r,
  client,
  showDetail,
  bridge,
  ...actions
}: Omit<ResultActionsProps, "r"> & { r: SearchResult; showDetail: boolean; bridge?: string[] }) {
  return (
    <List.Item
      id={resultId(r)}
      icon={{ fileIcon: r.abs_path }}
      title={displayTitle(r)}
      subtitle={showDetail ? undefined : folderOf(r.path)}
      accessories={showDetail ? undefined : accessories(r)}
      quickLook={{ path: r.abs_path, name: displayTitle(r) }}
      detail={showDetail ? <ResultDetail r={r} client={client} bridge={bridge} /> : undefined}
      actions={<ResultActions r={r} client={client} {...actions} />}
    />
  );
}
