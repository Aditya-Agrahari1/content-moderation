export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface Identity {
  userId: string;
  workspaceId: string;
}

export interface ReviewItem {
  id: string;
  status: string;
  reasons: string[];
  hold_reason: string | null;
  reviewable: boolean;
  type: string;
  source: string;
  created_at: string;
}

export class ApiError extends Error {}

function headers(identity: Identity): HeadersInit {
  return { "X-User-Id": identity.userId, "X-Workspace-Id": identity.workspaceId };
}

function isReviewItem(value: unknown): value is ReviewItem {
  const item = value as ReviewItem;
  return typeof item === "object" && item !== null && typeof item.id === "string" && Array.isArray(item.reasons);
}

/** Resolves only with a real list from the API. Every failure throws: an error is never an empty queue. */
export async function fetchReviewQueue(identity: Identity): Promise<ReviewItem[]> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/review`, { headers: headers(identity) });
  } catch {
    throw new ApiError("Could not load the review queue: the API is unreachable.");
  }
  if (!response.ok) {
    throw new ApiError(`Could not load the review queue (HTTP ${response.status}).`);
  }
  const data: unknown = await response.json().catch(() => undefined);
  if (!Array.isArray(data) || !data.every(isReviewItem)) {
    throw new ApiError("Could not load the review queue: unexpected response from the API.");
  }
  return data;
}

export async function decide(identity: Identity, id: string, action: "approve" | "reject"): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/review/${id}/${action}`, { method: "POST", headers: headers(identity) });
  } catch {
    throw new ApiError(`Could not ${action}: the API is unreachable.`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(`Could not ${action} (HTTP ${response.status}${body.detail ? `: ${body.detail}` : ""}).`);
  }
}
