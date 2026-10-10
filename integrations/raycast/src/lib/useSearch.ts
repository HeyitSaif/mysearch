/* Search-as-you-type against MySearch, the way the app does it (mse/ui/app.js runSearch + refine):
   debounce, abort the request a newer keystroke supersedes, show the instant results, then ask again with
   precise=1 so the re-ranker re-checks close calls, and swap the list only if the order changed. */

import { useEffect, useRef, useState } from "react";
import { isAbort, ServiceError } from "./client";
import { asServiceError, getClient } from "./service";
import type { SearchResponse } from "./types";

export const DEBOUNCE_MS = 150;
/** One character matches half the index and costs a full search on the service (aborting a request does not
    stop the work there), so searching starts at two. */
export const MIN_CHARS = 2;

export interface SearchState {
  response: SearchResponse | null;
  error: ServiceError | null;
  isLoading: boolean;
  refined: boolean; // the precise pass re-ordered the list
  /** The query the shown results belong to (favourites are recorded against it). */
  query: string;
}

const order = (r: SearchResponse) => r.results.map((x) => `${x.collection}/${x.path}`).join("\n");

export function useSearch(text: string, scope: string, types: string[], refine = true) {
  const [state, setState] = useState<SearchState>({
    response: null,
    error: null,
    isLoading: false,
    refined: false,
    query: "",
  });
  const [nonce, setNonce] = useState(0);
  const ctrlRef = useRef<AbortController | null>(null);
  const typesKey = types.join(",");

  useEffect(() => {
    const q = text.trim();
    ctrlRef.current?.abort();
    if (q.length < MIN_CHARS) {
      setState({ response: null, error: null, isLoading: false, refined: false, query: "" });
      return;
    }
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setState((s) => ({ ...s, isLoading: true }));
    const timer = setTimeout(async () => {
      let phase: "instant" | "precise" = "instant";
      try {
        const client = getClient();
        const params = { q, scope, types: typesKey ? typesKey.split(",") : [] };
        const first = await client.search(params, ctrl.signal);
        if (ctrl.signal.aborted) return;
        const more = refine && first.results.length >= 2;
        setState({ response: first, error: null, isLoading: more, refined: false, query: q });
        if (!more) return;
        phase = "precise";
        const second = await client.search({ ...params, precise: true }, ctrl.signal);
        if (ctrl.signal.aborted) return;
        const changed = order(first) !== order(second);
        setState({ response: changed ? second : first, error: null, isLoading: false, refined: changed, query: q });
      } catch (e) {
        if (isAbort(e) || ctrl.signal.aborted) return;
        if (phase === "precise") {
          setState((s) => ({ ...s, isLoading: false })); // keep the instant results
          return;
        }
        setState({ response: null, error: asServiceError(e), isLoading: false, refined: false, query: q });
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [text, scope, typesKey, refine, nonce]);

  return { ...state, revalidate: () => setNonce((n) => n + 1) };
}
