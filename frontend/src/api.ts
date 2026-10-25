export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface Identity {
  userId: string;
  workspaceId: string;
}

export interface ReviewItem {
  id: string;
  status: string;
  reasons: string[];
  type: string;
  source: string;
  created_at: string;
}

function headers(identity: Identity): HeadersInit {
  return { "X-User-Id": identity.userId, "X-Workspace-Id": identity.workspaceId };
}

export async function fetchReviewQueue(identity: Identity): Promise<ReviewItem[]> {
  try {
    const response = await fetch(`${API_URL}/review`, { headers: headers(identity) });
    const data = await response.json();
    return Array.isArray(data) ? data : [];
  } catch {
    // BUG 5: a failed request is shown as an empty queue ("Nothing to review").
    return [];
  }
}

export async function decide(identity: Identity, id: string, action: "approve" | "reject"): Promise<void> {
  const response = await fetch(`${API_URL}/review/${id}/${action}`, { method: "POST", headers: headers(identity) });
  if (!response.ok) throw new Error(`${action} failed (HTTP ${response.status})`);
}
