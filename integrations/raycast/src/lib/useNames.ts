/* Instant file names, the way the MySearch app does it on every keystroke: a debounce of 40 ms, and the request a newer
   keystroke supersedes is aborted. Scope "mac" searches every file name under the configured roots (whole-Mac index);
   scope "indexed" searches only the folders of the chosen folder or group. While the whole-Mac index is still being
   built (searchable: false) the caller falls back to content search and shows the progress from `percent`. */

import { useEffect, useState } from "react";
import { isAbort, type NameParams } from "./client";
import { asServiceError, getClient } from "./service";
import type { NameSearchResponse } from "./types";
import { NAME_KINDS } from "./format";
import { MIN_CHARS } from "./useSearch";

export const NAME_DEBOUNCE_MS = 40;

export interface NameState {
  response: NameSearchResponse | null;
  error: ReturnType<typeof asServiceError> | null;
  isLoading: boolean;
  /** Whole-Mac index progress in percent, while it is still building. */
  percent: number | null;
}

export type NameScope = { scope: "mac" } | { scope: "indexed"; collections: string[] };

/** The query the service gets: the typed text plus the file-type filter ("kind:pdf kind:doc kind:sheet"). */
export function nameQuery(text: string, kind: string): string {
  const kinds = (kind ? (NAME_KINDS[kind] ?? []) : []).map((k) => `kind:${k}`);
  return [text.trim(), ...kinds].filter(Boolean).join(" ");
}

const EMPTY: NameState = { response: null, error: null, isLoading: false, percent: null };

export function useNameSearch(text: string, where: NameScope, kind: string) {
  const [state, setState] = useState<NameState>(EMPTY);
  const [nonce, setNonce] = useState(0);
  const scope = where.scope;
  const collections = where.scope === "indexed" ? where.collections.join(",") : "";
  const q0 = text.trim();

  useEffect(() => {
    if (q0.length < MIN_CHARS) {
      setState(EMPTY);
      return;
    }
    const ctrl = new AbortController();
    setState((s) => ({ ...s, isLoading: true }));
    const timer = setTimeout(async () => {
      try {
        const params: NameParams = {
          q: nameQuery(q0, kind),
          scope,
          collections: collections ? collections.split(",") : undefined,
        };
        const r = await getClient().nameSearch(params, ctrl.signal);
        if (ctrl.signal.aborted) return;
        setState({ response: r, error: null, isLoading: false, percent: null });
        if (!r.searchable) {
          // the progress bar: fraction 0..1 from the status endpoint (a failure here is not worth a message)
          const st = await getClient()
            .nameStatus(ctrl.signal)
            .catch(() => null);
          if (ctrl.signal.aborted || !st) return;
          const f = st.progress?.fraction;
          setState((s) => ({ ...s, percent: typeof f === "number" ? Math.round(f * 100) : null }));
        }
      } catch (e) {
        if (isAbort(e) || ctrl.signal.aborted) return;
        setState({ response: null, error: asServiceError(e), isLoading: false, percent: null });
      }
    }, NAME_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [q0, scope, collections, kind, nonce]);

  return { ...state, revalidate: () => setNonce((n) => n + 1) };
}
