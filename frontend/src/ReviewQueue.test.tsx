import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReviewQueue from "./ReviewQueue";

const admin = { userId: "admin_v", workspaceId: "ws_verified" };

function mockFetch(impl: () => Promise<Response>) {
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
});
