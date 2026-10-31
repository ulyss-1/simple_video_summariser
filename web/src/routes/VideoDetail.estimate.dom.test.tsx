import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api";
import { FakePlayerProvider } from "../components/FakePlayerProvider";
import { setupUser } from "../test/user";
import { VideoDetail } from "./VideoDetail";

// The "Estimated analysis cost" group of #153 on the video page.
type VideoDetailOut = components["schemas"]["VideoDetail"];
type Estimate = components["schemas"]["AnalysisEstimate"];
type Row = components["schemas"]["ModelEstimate"];

const ID = "dQw4w9WgXcQ";
const user = setupUser;

function video(over: Partial<VideoDetailOut> = {}): VideoDetailOut {
  return {
    video_id: ID,
    title: "My Video",
    channel_id: "UC" + "a".repeat(22),
    channel_title: "My Channel",
    published_at: null,
    duration_sec: null,
    latest_analysis_at: null,
    has_transcript: true,
    origin: "manual",
    unavailable: null,
    status: "idle",
    active_job: null,
    last_failure: null,
    last_cancelled: null,
    transcript: {
      id: 1,
      language: "en",
      segment_count: 3,
      source: "whisper",
      speaker_source: "none",
    },
    analysis: null,
    progress: null,
    ...over,
  };
}

const estimated = (over: Partial<Row> = {}): Row => ({
  model: "claude-haiku-4-5",
  state: "estimated",
  cost_usd: 0.0227,
  reason: null,
  samples: 12,
  ...over,
});

function estimate(estimates: Row[], over: Partial<Estimate> = {}): Estimate {
  return {
    video_id: ID,
    summarizer: "anthropic",
    transcript_chars: 17000,
    batch: false,
    estimates,
    ...over,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let estimateCalls: number;
type EstimateResponder = (n: number) => Response | Promise<Response>;

function stubApi(respond: EstimateResponder, v: VideoDetailOut = video()) {
  estimateCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/analysis-estimate")) {
        estimateCalls += 1;
        return Promise.resolve(respond(estimateCalls));
      }
      if (url.pathname.endsWith("/analyses"))
        return Promise.resolve(
          json({ items: [], total: 0, offset: 0, limit: 1 }),
        );
      if (init?.method === "POST")
        return Promise.resolve(json({ job_id: 1 }, 202));
      return Promise.resolve(json(v));
    }),
  );
}

async function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      {
        path: "/videos/:videoId",
        element: (
          <FakePlayerProvider seeks={[]}>
            <VideoDetail />
          </FakePlayerProvider>
        ),
      },
    ],
    { initialEntries: [`/videos/${ID}`] },
  );
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1, name: "My Video" });
}

const group = () =>
  screen.findByRole("region", { name: "Estimated analysis cost" });

beforeEach(() => {
  vi.useRealTimers();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("estimated analysis cost (#153)", () => {
  it("shows an estimated row flagged as an estimate in USD, with the sample hint", async () => {
    stubApi(() => json(estimate([estimated()])));
    await mount();
    const g = await group();
    expect(
      await within(g).findByText(
        "claude-haiku-4-5: about $0.0227 (estimate, USD)",
      ),
    ).toBeTruthy();
    expect(
      within(g).getByText(
        "Based on the median of the last 12 recorded analyses",
      ),
    ).toBeTruthy();
    expect(g.textContent).not.toContain("batch pricing");
  });

  it('adds "batch pricing" to the hint when batch is on', async () => {
    stubApi(() => json(estimate([estimated()], { batch: true })));
    await mount();
    const g = await group();
    expect(
      await within(g).findByText(
        "Based on the median of the last 12 recorded analyses · batch pricing",
      ),
    ).toBeTruthy();
  });

  it.each([
    ["no_transcript", "No transcript yet"],
    ["no_cost_samples", "No recorded costs for this model yet"],
    ["no_price", "No price known for this model"],
  ] as const)(
    'shows "no estimate" and the sentence for %s',
    async (reason, sentence) => {
      stubApi(() =>
        json(
          estimate([
            {
              model: "claude-x",
              state: "unknown",
              cost_usd: null,
              reason,
              samples: 0,
            },
          ]),
        ),
      );
      await mount();
      const g = await group();
      expect(await within(g).findByText("claude-x: no estimate")).toBeTruthy();
      expect(within(g).getByText(sentence)).toBeTruthy();
      expect(g.textContent).not.toContain("$");
    },
  );

  it('shows "no API cost" for a free model', async () => {
    stubApi(() =>
      json(
        estimate(
          [
            {
              model: "qwen3.5:4b",
              state: "free",
              cost_usd: null,
              reason: null,
              samples: 0,
            },
          ],
          {
            summarizer: "ollama",
            transcript_chars: null,
          },
        ),
      ),
    );
    await mount();
    const g = await group();
    expect(await within(g).findByText("qwen3.5:4b: no API cost")).toBeTruthy();
    expect(g.textContent).not.toContain("estimate");
  });

  it("lists one row per model in the API order", async () => {
    stubApi(() =>
      json(
        estimate([
          estimated(),
          estimated({ model: "claude-other", cost_usd: 0.5, samples: 3 }),
        ]),
      ),
    );
    await mount();
    const g = await group();
    await within(g).findByText(
      "claude-haiku-4-5: about $0.0227 (estimate, USD)",
    );
    const items = within(g)
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    expect(items[0]).toContain("claude-haiku-4-5");
    expect(items[1]).toContain("claude-other: about $0.5000 (estimate, USD)");
  });

  it("shows a status while loading and the page keeps working", async () => {
    stubApi(() => new Promise<Response>(() => {}));
    await mount();
    const g = await group();
    expect(within(g).getByRole("status").textContent).toBe(
      "Loading cost estimate…",
    );
    expect(
      (screen.getByRole("button", { name: "Re-analyse" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("shows a fixed alert with Retry on a server error and never the server text", async () => {
    stubApi((n) =>
      n === 1
        ? json({ detail: "SECRET-DETAIL" }, 500)
        : json(estimate([estimated()])),
    );
    await mount();
    const g = await group();
    const alert = await within(g).findByRole("alert");
    expect(alert.textContent).toContain("Could not load the cost estimate");
    expect(document.body.textContent).not.toContain("SECRET-DETAIL");
    // The failure stays in its own group.
    expect(
      (screen.getByRole("button", { name: "Re-analyse" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    await user().click(within(g).getByRole("button", { name: "Retry" }));

    expect(
      await within(g).findByText(
        "claude-haiku-4-5: about $0.0227 (estimate, USD)",
      ),
    ).toBeTruthy();
    expect(estimateCalls).toBe(2);
  });

  it("offers no Retry for a 4xx", async () => {
    stubApi(() => json({ detail: "nope" }, 404));
    await mount();
    const g = await group();
    expect(await within(g).findByRole("alert")).toBeTruthy();
    expect(within(g).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("treats a row that breaks the typed encoding as a failed load", async () => {
    stubApi(() =>
      json(
        estimate([
          {
            model: "m",
            state: "free",
            cost_usd: 0.5,
            reason: null,
            samples: 0,
          },
        ]),
      ),
    );
    await mount();
    const g = await group();
    expect(await within(g).findByRole("alert")).toBeTruthy();
    expect(g.textContent).not.toContain("$0.5");
  });

  it("points the Re-analyse button at the estimate text", async () => {
    stubApi(() => json(estimate([estimated()])));
    await mount();
    const g = await group();
    await within(g).findByText(
      "claude-haiku-4-5: about $0.0227 (estimate, USD)",
    );
    const button = screen.getByRole("button", { name: "Re-analyse" });
    const target = document.getElementById(
      button.getAttribute("aria-describedby") ?? "",
    );
    expect(target).not.toBeNull();
    expect(target?.textContent).toContain("about $0.0227 (estimate, USD)");
    expect(button.getAttribute("aria-describedby")).toBeTruthy();
  });

  it("has one h1 and the group is an h2 section", async () => {
    stubApi(() => json(estimate([estimated()])));
    await mount();
    const g = await group();
    expect(
      within(g).getByRole("heading", {
        level: 2,
        name: "Estimated analysis cost",
      }),
    ).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("is refreshed with the video after a re-analysis is triggered", async () => {
    stubApi(() => json(estimate([estimated()])));
    await mount();
    const g = await group();
    await within(g).findByText(
      "claude-haiku-4-5: about $0.0227 (estimate, USD)",
    );
    expect(estimateCalls).toBe(1);

    await user().click(screen.getByRole("button", { name: "Re-analyse" }));

    await waitFor(() => expect(estimateCalls).toBe(2));
  });

  it("is neither shown nor requested while the video has no transcript", async () => {
    stubApi(
      () => json(estimate([estimated()])),
      video({ transcript: null, has_transcript: false }),
    );
    await mount();
    expect(
      screen.queryByRole("region", { name: "Estimated analysis cost" }),
    ).toBeNull();
    expect(estimateCalls).toBe(0);
  });

  it("asks again when the video's latest analysis changes (a finished re-analysis, seen by the poll)", async () => {
    let finished = false;
    const seenKeys: string[] = [];
    estimateCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/analysis-estimate")) {
          estimateCalls += 1;
          seenKeys.push(finished ? "after" : "before");
          return Promise.resolve(
            json(
              estimate([
                estimated({ cost_usd: finished ? 0.03 : 0.0227 }),
              ]),
            ),
          );
        }
        if (url.pathname.endsWith("/analyses"))
          return Promise.resolve(
            json({ items: [], total: 0, offset: 0, limit: 1 }),
          );
        if (init?.method === "POST") {
          finished = true;
          return Promise.resolve(json({ job_id: 1 }, 202));
        }
        return Promise.resolve(
          json(
            video({
              latest_analysis_at: finished ? "2026-10-02T00:00:00Z" : null,
            }),
          ),
        );
      }),
    );
    await mount();
    const g = await group();
    await within(g).findByText(
      "claude-haiku-4-5: about $0.0227 (estimate, USD)",
    );

    await user().click(screen.getByRole("button", { name: "Re-analyse" }));

    expect(
      await within(g).findByText(
        "claude-haiku-4-5: about $0.0300 (estimate, USD)",
      ),
    ).toBeTruthy();
    expect(seenKeys).toContain("after");
  });
});
