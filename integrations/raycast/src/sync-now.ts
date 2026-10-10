import { showHUD } from "@raycast/api";
import { asServiceError, errorText, getClient, RESTART_COMMAND } from "./lib/service";

/** Asks MySearch to check every enabled folder for changes now (the app's "Sync now"). */
export default async function SyncNow() {
  try {
    const { jobs } = await getClient().sync();
    const n = jobs.length;
    await showHUD(n === 1 ? `Syncing ${jobs[0].collection ?? "1 folder"}` : `Syncing ${n} folders`);
  } catch (e) {
    const err = asServiceError(e);
    const hint = err.kind === "unreachable" || err.kind === "timeout" ? ` — run ${RESTART_COMMAND}` : "";
    await showHUD(`MySearch: ${errorText(err).title}${hint}`);
  }
}
