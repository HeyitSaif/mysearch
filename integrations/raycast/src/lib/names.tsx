/* One file name from the instant name search (whole Mac or indexed folders): the list row, its detail pane and actions.
   Rows of content results live in results.tsx. */

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
} from "@raycast/api";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import type { ReactNode } from "react";
import { boldSpansMarkdown, formatSize, homeRelative, KIND_LABEL } from "./format";
import { openInApp } from "./service";
import type { NameItem } from "./types";

const HOME = homedir();

export function nameKind(item: NameItem): string {
  if (item.is_dir) return "Folder";
  const k = KIND_LABEL[item.kind] || item.kind;
  return item.ext && item.ext.toUpperCase() !== k.toUpperCase() ? `${k} · ${item.ext.toUpperCase()}` : k;
}

/** The folder that holds this item, or the item itself when it is a folder ("Search only in this folder"). */
export function searchFolderOf(item: NameItem): string {
  return item.is_dir ? item.path : item.parent;
}

export function NameItemView({
  item,
  showDetail,
  query,
  scope,
  viewActions,
  onSearchFolder,
}: {
  item: NameItem;
  showDetail: boolean;
  /** The text the list searched for (opened in the app as the query). */
  query: string;
  /** "mac", "all", a folder or a group: where the search ran, passed on to the app. */
  scope?: string;
  viewActions?: ReactNode;
  onSearchFolder?: (folder: string) => void;
}) {
  const folder = homeRelative(item.parent, HOME);
  const exists = () => existsSync(item.path);
  return (
    <List.Item
      id={item.path}
      icon={{ fileIcon: item.path }}
      title={item.name}
      subtitle={showDetail ? undefined : folder}
      accessories={
        showDetail
          ? undefined
          : [
              ...(item.is_dir ? [] : [{ text: formatSize(item.size), tooltip: "Size" }]),
              { date: new Date(item.mtime * 1000), tooltip: "Modified" },
              { tag: { value: nameKind(item), color: Color.SecondaryText }, tooltip: "Kind" },
            ]
      }
      quickLook={item.is_dir ? undefined : { path: item.path, name: item.name }}
      detail={
        showDetail ? (
          <List.Item.Detail
            markdown={`### ${boldSpansMarkdown(item.name, item.match)}`}
            metadata={
              <List.Item.Detail.Metadata>
                <List.Item.Detail.Metadata.Label title="Folder" text={folder} icon={Icon.Folder} />
                <List.Item.Detail.Metadata.Label title="Kind" text={nameKind(item)} />
                {item.is_dir ? null : <List.Item.Detail.Metadata.Label title="Size" text={formatSize(item.size)} />}
                <List.Item.Detail.Metadata.Label title="Modified" text={new Date(item.mtime * 1000).toLocaleString()} />
                <List.Item.Detail.Metadata.Label title="Path" text={homeRelative(item.path, HOME)} />
              </List.Item.Detail.Metadata>
            }
          />
        ) : undefined
      }
      actions={
        <ActionPanel title={item.name}>
          <ActionPanel.Section>
            <Action
              title="Open"
              icon={Icon.ArrowNe}
              onAction={async () => {
                if (!exists()) {
                  await showToast({
                    style: Toast.Style.Failure,
                    title: "File not found",
                    message: "It was moved or deleted since the name was indexed.",
                  });
                  return;
                }
                await open(item.path);
                await closeMainWindow();
              }}
            />
            <Action.ShowInFinder path={item.path} />
            {onSearchFolder ? (
              <Action
                title="Search Only in This Folder"
                icon={Icon.MagnifyingGlass}
                shortcut={{ modifiers: ["cmd", "alt"], key: "f" }}
                onAction={() => onSearchFolder(searchFolderOf(item))}
              />
            ) : null}
            <Action
              title="Show in MySearch"
              icon={Icon.AppWindowSidebarRight}
              shortcut={Keyboard.Shortcut.Common.Open}
              onAction={() => openInApp({ q: query, scope: scope && scope !== "mac" ? scope : undefined })}
            />
            {item.is_dir ? null : <Action.ToggleQuickLook shortcut={Keyboard.Shortcut.Common.ToggleQuickLook} />}
          </ActionPanel.Section>
          <ActionPanel.Section>
            <Action.CopyToClipboard
              title="Copy Path"
              content={item.path}
              shortcut={Keyboard.Shortcut.Common.CopyPath}
            />
          </ActionPanel.Section>
          {viewActions ? <ActionPanel.Section>{viewActions}</ActionPanel.Section> : null}
        </ActionPanel>
      }
    />
  );
}
