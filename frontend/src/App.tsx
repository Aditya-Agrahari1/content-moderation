import { useState } from "react";
import type { Identity } from "./api";
import ReviewQueue from "./ReviewQueue";

// Test-harness identities seeded by the API (no real login in this exercise).
const IDENTITIES: Identity[] = [
  { userId: "admin_v", workspaceId: "ws_verified" },
  { userId: "member_v", workspaceId: "ws_verified" },
  { userId: "admin_u", workspaceId: "ws_unverified" },
  { userId: "member_u", workspaceId: "ws_unverified" },
];

export default function App() {
  const [index, setIndex] = useState(0);
  const identity = IDENTITIES[index];
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: 16, maxWidth: 960 }}>
      <h1>Review queue</h1>
      <label>
        Acting as{" "}
        <select value={index} onChange={(e) => setIndex(Number(e.target.value))}>
          {IDENTITIES.map((id, i) => (
            <option key={id.userId} value={i}>{id.userId} ({id.workspaceId})</option>
          ))}
        </select>
      </label>
      <ReviewQueue identity={identity} />
    </main>
  );
}
