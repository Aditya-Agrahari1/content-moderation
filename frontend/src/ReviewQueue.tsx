import { useCallback, useEffect, useState } from "react";
import { decide, fetchReviewQueue, type Identity, type ReviewItem } from "./api";

type QueueState =
  | { kind: "loading" }
  | { kind: "failed"; message: string }
  | { kind: "loaded"; items: ReviewItem[] };

/** The queue re-checks the API on its own, so a page left open never shows a stale "Nothing to review". */
export const REFRESH_MS = 10_000;

const HOLD_LABEL: Record<string, string> = {
  needs_human_review: "Needs review",
  provider_unavailable: "Waiting for re-screen (provider unavailable)",
  screening_failed: "Screening failed (operator action)",
};

export default function ReviewQueue({ identity }: { identity: Identity }) {
  const [state, setState] = useState<QueueState>({ kind: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);

  // `background` refreshes keep the current list on screen until the answer arrives;
  // a failure always replaces it with the error state.
  const load = useCallback(
    async (background = false) => {
      if (!background) setState({ kind: "loading" });
      try {
        const items = await fetchReviewQueue(identity);
        setState({ kind: "loaded", items });
      } catch (err) {
        setState({ kind: "failed", message: err instanceof Error ? err.message : String(err) });
      }
      setCheckedAt(new Date());
    },
    [identity],
  );

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(true), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function act(id: string, action: "approve" | "reject") {
    setActionError(null);
    try {
      await decide(identity, id, action);
      await load(true);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  const toolbar = (
    <p>
      <button onClick={() => void load()}>Refresh</button>{" "}
      <small>
        {checkedAt ? `Last checked ${checkedAt.toLocaleTimeString()}` : ""} · refreshes every {REFRESH_MS / 1000}s
      </small>
    </p>
  );

  if (state.kind === "loading") return <p role="status">Loading…</p>;
  if (state.kind === "failed") {
    return (
      <div>
        {toolbar}
        <div role="alert">
          <p>{state.message}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      </div>
    );
  }
  if (state.items.length === 0) {
    return (
      <div>
        {toolbar}
        <p>Nothing to review</p>
      </div>
    );
  }
  return (
    <div>
      {toolbar}
      {actionError && <p role="alert">{actionError}</p>}
      <table>
        <thead>
          <tr><th>Media</th><th>Type</th><th>Held because</th><th>Reasons</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {state.items.map((item) => (
            <tr key={item.id}>
              <td><code>{item.id}</code></td>
              <td>{item.type} / {item.source}</td>
              <td>{HOLD_LABEL[item.hold_reason ?? ""] ?? item.hold_reason ?? "—"}</td>
              <td>{item.reasons.join(", ") || "—"}</td>
              <td>
                <button
                  disabled={item.reviewable !== true}
                  title={item.reviewable ? undefined : "Not screened yet: only reject is possible"}
                  onClick={() => void act(item.id, "approve")}
                >
                  Approve
                </button>{" "}
                <button onClick={() => void act(item.id, "reject")}>Reject</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
