/* Ask MySearch: a question, answered from your folders with citations. The answer streams in a Detail view, with the
   steps the service took and the sources it read. Needs Ask switched on in MySearch (otherwise the service answers 409). */

import { Action, ActionPanel, Color, Detail, Icon, Keyboard, List } from "@raycast/api";
import { useEffect, useState } from "react";
import { isAbort } from "./lib/client";
import { clock, escapeMarkdown } from "./lib/format";
import {
  asServiceError,
  getClient,
  openClassicMySearch,
  openInApp,
  openMySearchApp,
  useScopes,
  WHOLE_MAC,
} from "./lib/service";
import type { AskEvent, AskSource, AskTurn } from "./lib/types";
import { MIN_CHARS } from "./lib/useSearch";

type DoneEvent = Extract<AskEvent, { type: "done" }>;

interface AnswerState {
  steps: string[];
  text: string;
  sources: AskSource[];
  done: DoneEvent | null;
  error: string | null;
  disabled: boolean;
  loading: boolean;
}

const INITIAL: AnswerState = {
  steps: [],
  text: "",
  sources: [],
  done: null,
  error: null,
  disabled: false,
  loading: true,
};

function apply(s: AnswerState, ev: AskEvent): AnswerState {
  switch (ev.type) {
    case "step":
      return { ...s, steps: [...s.steps, ev.text] };
    case "sources":
      return { ...s, sources: ev.sources };
    case "token":
      return { ...s, text: s.text + ev.text };
    case "done":
      return { ...s, done: ev, text: ev.answer || s.text, loading: false };
    case "error":
      return { ...s, error: ev.message, loading: false };
    default:
      return s;
  }
}

/** The answer came from a model whose provider may use inputs for training (the service marks it: `training`). */
function isTrainingModel(d: DoneEvent): boolean {
  return d.training === true;
}

function answerMarkdown(s: AnswerState): string {
  if (s.disabled) {
    return "### Ask is off\n\nTurn Ask on in MySearch Settings, then try again. The button below opens MySearch.";
  }
  if (s.error) return `### Could not answer\n\n${escapeMarkdown(s.error)}`;
  const parts: string[] = [];
  if (s.done && isTrainingModel(s.done)) {
    parts.push("> **Warning:** the provider of this model may use inputs for training — check the provider's terms.");
  }
  if (s.steps.length) parts.push(`_${s.steps.map(escapeMarkdown).join(" · ")}_`);
  parts.push(s.text.trim() || (s.loading ? "_Searching your folders…_" : "_No answer._"));
  if (s.sources.length) {
    const lines = s.sources.map((src) => {
      const name = src.title || src.path.split("/").pop() || src.path;
      const where = [src.page ? `p. ${src.page}` : "", src.ts != null ? clock(src.ts) : ""].filter(Boolean).join(", ");
      return `${src.n}. **${escapeMarkdown(name)}**  \n${escapeMarkdown(`${src.collection}/${src.path}`)}${where ? ` · ${where}` : ""}`;
    });
    parts.push(`### Sources\n\n${lines.join("\n\n")}`);
  }
  return parts.join("\n\n");
}

function AnswerView({
  question,
  scope,
  history = [],
  route,
}: {
  question: string;
  scope: string;
  history?: AskTurn[];
  route?: "local";
}) {
  const [s, setS] = useState<AnswerState>(INITIAL);
  const historyKey = JSON.stringify(history);

  useEffect(() => {
    const ctrl = new AbortController();
    setS(INITIAL);
    (async () => {
      try {
        await getClient().ask(
          {
            q: question,
            collections: scope === "all" || scope === WHOLE_MAC ? undefined : [scope],
            history: JSON.parse(historyKey) as AskTurn[],
            route,
          },
          (ev) => {
            if (!ctrl.signal.aborted) setS((prev) => apply(prev, ev));
          },
          ctrl.signal,
        );
        if (!ctrl.signal.aborted) setS((prev) => ({ ...prev, loading: false }));
      } catch (e) {
        if (isAbort(e) || ctrl.signal.aborted) return;
        const err = asServiceError(e);
        setS((prev) => ({
          ...prev,
          loading: false,
          disabled: err.kind === "ask_disabled",
          error: err.kind === "ask_disabled" ? null : err.message,
        }));
      }
    })();
    return () => ctrl.abort();
  }, [question, scope, route, historyKey]);

  const d = s.done;
  const turns: AskTurn[] = [...(JSON.parse(historyKey) as AskTurn[]), { q: question, a: s.text.trim() }];

  return (
    <Detail
      isLoading={s.loading}
      navigationTitle={`Ask · ${question}`}
      markdown={answerMarkdown(s)}
      metadata={
        d ? (
          <Detail.Metadata>
            <Detail.Metadata.Label title="Model" text={d.model} />
            <Detail.Metadata.Label
              title="Where"
              text={d.where === "cloud" ? "Cloud" : "On this Mac"}
              icon={{ source: Icon.Globe, tintColor: d.where === "cloud" ? Color.Blue : Color.Green }}
            />
            {d.fallback_reason ? (
              <Detail.Metadata.Label title="Answered locally because" text={d.fallback_reason} />
            ) : null}
            {d.timings?.total_ms ? (
              <Detail.Metadata.Label title="Time" text={`${(d.timings.total_ms / 1000).toFixed(1)} s`} />
            ) : null}
            <Detail.Metadata.Label title="Sources" text={String(s.sources.length)} />
          </Detail.Metadata>
        ) : undefined
      }
      actions={
        <ActionPanel>
          {s.text.trim() ? (
            <Action.CopyToClipboard
              title="Copy Answer"
              content={s.text.trim()}
              shortcut={Keyboard.Shortcut.Common.Copy}
            />
          ) : null}
          {s.sources.length ? (
            <ActionPanel.Submenu title="Open Source" icon={Icon.Document}>
              {s.sources.map((src) => (
                <Action
                  key={src.n}
                  title={`${src.n}. ${src.title || src.path.split("/").pop() || src.path}`}
                  icon={Icon.AppWindowSidebarRight}
                  onAction={() => openInApp({ q: question, scope: src.collection, docid: src.docid, page: src.page })}
                />
              ))}
            </ActionPanel.Submenu>
          ) : null}
          {d && !route ? (
            <Action.Push
              title="Answer Locally"
              icon={Icon.ComputerChip}
              shortcut={{ modifiers: ["cmd"], key: "l" }}
              target={<AnswerView question={question} scope={scope} history={history} route="local" />}
            />
          ) : null}
          {d ? (
            <Action.Push
              title="Follow up"
              icon={Icon.Message}
              shortcut={{ modifiers: ["cmd", "shift"], key: "u" }}
              target={<FollowUp scope={scope} history={turns.slice(-3)} />}
            />
          ) : null}
          {s.disabled ? (
            <Action
              title="Open MySearch"
              icon={Icon.AppWindow}
              onAction={() => openMySearchApp().catch(() => undefined)}
            />
          ) : null}
          <Action title="Open Classic MySearch" icon={Icon.Window} onAction={() => openClassicMySearch()} />
          <Action
            title="Open Question in MySearch"
            icon={Icon.AppWindowSidebarRight}
            onAction={() =>
              openInApp({ q: question, scope: scope === "all" || scope === WHOLE_MAC ? undefined : scope })
            }
          />
        </ActionPanel>
      }
    />
  );
}

/** Type the follow-up, then ask it with the last three turns as context. */
function FollowUp({ scope, history }: { scope: string; history: AskTurn[] }) {
  const [text, setText] = useState("");
  const q = text.trim();
  return (
    <List
      navigationTitle="Follow Up"
      searchBarPlaceholder="Ask a follow-up question…"
      filtering={false}
      searchText={text}
      onSearchTextChange={setText}
    >
      {q.length >= MIN_CHARS ? (
        <List.Item
          title={q}
          icon={Icon.QuestionMark}
          actions={
            <ActionPanel>
              <Action.Push title="Ask Follow-Up" target={<AnswerView question={q} scope={scope} history={history} />} />
            </ActionPanel>
          }
        />
      ) : (
        <List.EmptyView
          icon={Icon.Message}
          title="Ask a follow-up"
          description="The last questions and answers are sent along as context."
        />
      )}
    </List>
  );
}

export default function AskMySearch() {
  const [text, setText] = useState("");
  const [chosen, setChosen] = useState("all");
  const scopes = useScopes();
  const scope = scopes.resolve(chosen);
  const q = text.trim();
  const label = [...scopes.groups, ...scopes.folders].find((o) => o.value === scope)?.title ?? "All Indexed Folders";

  return (
    <List
      searchBarPlaceholder="Ask your folders a question, in English or German…"
      filtering={false}
      searchText={text}
      onSearchTextChange={setText}
      searchBarAccessory={
        <List.Dropdown tooltip="Ask in" storeValue onChange={setChosen}>
          <List.Dropdown.Item title="All Indexed Folders" value="all" icon={Icon.Folder} />
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
      {q.length >= MIN_CHARS ? (
        <List.Item
          title={q}
          subtitle={label}
          icon={Icon.QuestionMark}
          actions={
            <ActionPanel>
              <Action.Push title="Ask" icon={Icon.Stars} target={<AnswerView question={q} scope={scope} />} />
              <Action.Push
                title="Answer Locally"
                icon={Icon.ComputerChip}
                shortcut={{ modifiers: ["cmd"], key: "l" }}
                target={<AnswerView question={q} scope={scope} route="local" />}
              />
              <Action title="Open Classic MySearch" icon={Icon.Window} onAction={() => openClassicMySearch()} />
            </ActionPanel>
          }
        />
      ) : (
        <List.EmptyView
          icon={Icon.QuestionMark}
          title="Ask MySearch"
          description="Type a question. MySearch finds the passages in your folders and answers from them, with the sources. Ask has to be switched on in MySearch Settings."
        />
      )}
    </List>
  );
}
