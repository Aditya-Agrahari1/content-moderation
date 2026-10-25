import { useEffect, useState } from "react";
import { decide, fetchReviewQueue, type Identity, type ReviewItem } from "./api";

export default function ReviewQueue({ identity }: { identity: Identity }) {
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function load() {
    setItems(null);
    setItems(await fetchReviewQueue(identity));
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity.userId, identity.workspaceId]);

  async function act(id: string, action: "approve" | "reject") {
    setActionError(null);
    try {
      await decide(identity, id, action);
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  if (items === null) return <p role="status">Loading…</p>;
  if (items.length === 0) return <p>Nothing to review</p>;
  return (
    <div>
      {actionError && <p role="alert">{actionError}</p>}
      <table>
        <thead>
          <tr><th>Media</th><th>Type</th><th>Reasons</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td><code>{item.id}</code></td>
              <td>{item.type} / {item.source}</td>
              <td>{item.reasons.join(", ") || "—"}</td>
              <td>
                <button onClick={() => void act(item.id, "approve")}>Approve</button>{" "}
                <button onClick={() => void act(item.id, "reject")}>Reject</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
