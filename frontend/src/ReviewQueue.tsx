import { useCallback, useEffect, useState } from "react";
import { decide, fetchReviewQueue, type Identity, type ReviewItem } from "./api";

type QueueState =
  | { kind: "loading" }
  | { kind: "failed"; message: string }
  | { kind: "loaded"; items: ReviewItem[] };

const HOLD_LABEL: Record<string, string> = {
  needs_human_review: "Needs review",
  provider_unavailable: "Waiting for re-screen (provider unavailable)",
  screening_failed: "Screening failed (operator action)",
};

export default function ReviewQueue({ identity }: { identity: Identity }) {
  const [state, setState] = useState<QueueState>({ kind: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "loaded", items: await fetchReviewQueue(identity) });
    } catch (err) {
      setState({ kind: "failed", message: err instanceof Error ? err.message : String(err) });
    }
  }, [identity]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(id: string, action: "approve" | "reject") {
    setActionError(null);
    try {
      await decide(identity, id, action);
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  if (state.kind === "loading") return <p role="status">Loading…</p>;
  if (state.kind === "failed") {
    return (
      <div role="alert">
        <p>{state.message}</p>
        <button onClick={() => void load()}>Retry</button>
      </div>
    );
  }
  if (state.items.length === 0) return <p>Nothing to review</p>;
  return (
    <div>
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
