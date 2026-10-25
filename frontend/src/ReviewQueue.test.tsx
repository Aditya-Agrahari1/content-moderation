import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReviewQueue from "./ReviewQueue";

const admin = { userId: "admin_v", workspaceId: "ws_verified" };

function mockFetch(impl: (input?: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

function json(status: number, body: unknown): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.unstubAllGlobals());

describe("review queue states (incident bug 5)", () => {
  it("shows a loading state while the request is in flight", () => {
    mockFetch(() => new Promise<Response>(() => {}));
    render(<ReviewQueue identity={admin} />);
    expect(screen.getByRole("status")).toHaveTextContent(/loading/i);
  });

  it("an HTTP 500 is shown as a failure with Retry, never as 'Nothing to review'", async () => {
    mockFetch(() => json(500, { detail: "Internal server error" }));
    render(<ReviewQueue identity={admin} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load/i);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText(/nothing to review/i)).not.toBeInTheDocument();
  });

  it("a network error is shown as a failure, never as 'Nothing to review'", async () => {
    mockFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    render(<ReviewQueue identity={admin} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load/i);
    expect(screen.queryByText(/nothing to review/i)).not.toBeInTheDocument();
  });

  it("only a successful empty list is shown as 'Nothing to review'", async () => {
    mockFetch(() => json(200, []));
    render(<ReviewQueue identity={admin} />);
    expect(await screen.findByText(/nothing to review/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("an unexpected response shape is a failure, not an empty queue", async () => {
    mockFetch(() => json(200, { items: "?" }));
    render(<ReviewQueue identity={admin} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unexpected response/i);
    expect(screen.queryByText(/nothing to review/i)).not.toBeInTheDocument();
  });

  it("Retry reloads after a failure", async () => {
    let calls = 0;
    mockFetch(() => (++calls === 1 ? json(503, {}) : json(200, [])));
    render(<ReviewQueue identity={admin} />);
    fireEvent.click(await screen.findByRole("button", { name: /retry/i }));
    expect(await screen.findByText(/nothing to review/i)).toBeInTheDocument();
  });
});

const heldForReview = {
  id: "m1", status: "held", reasons: ["self_harm_review"], hold_reason: "needs_human_review", reviewable: true,
  type: "image", source: "upload", created_at: "2026-10-25T00:00:00Z",
};
const heldForOutage = { ...heldForReview, id: "m2", reasons: ["provider_unavailable"],
  hold_reason: "provider_unavailable", reviewable: false };

describe("review actions", () => {
  it("lists held items; outage holds cannot be approved", async () => {
    mockFetch(() => json(200, [heldForReview, heldForOutage]));
    render(<ReviewQueue identity={admin} />);
    const rows = await screen.findAllByRole("row");
    expect(rows).toHaveLength(3); // header + 2
    const approve = within(rows[1]).getByRole("button", { name: "Approve" });
    expect(approve).toBeEnabled();
    expect(within(rows[2]).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(rows[2]).getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("a failed approve shows the error and keeps the item", async () => {
    mockFetch(async (input?: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST" ? json(409, { detail: "not_held" }) : json(200, [heldForReview]),
    );
    render(<ReviewQueue identity={admin} />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not approve.*409.*not_held/i);
    expect(screen.getByText("m1")).toBeInTheDocument();
  });
});
