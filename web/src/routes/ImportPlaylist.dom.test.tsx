import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setupUser } from "../test/user";
import { routes } from "./index";

// #343: the playlist import page, with fetch stubbed.
const user = setupUser();

const PL = "https://www.youtube.com/playlist?list=PLabcdefghijkl";
const A = "Ab_-123xyZ0";
const B = "dQw4w9WgXcQ";
const C = "aaaaaaaaaaa";
const D = "bbbbbbbbbbb";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

type Entry = {
  video_id: string;
  title: string | null;
  known?: boolean;
  status?: string | null;
};
const entry = (e: Entry, position: number) => ({
  position,
  video_id: e.video_id,
  title: e.title,
  url: `https://www.youtube.com/watch?v=${e.video_id}`,
  known: e.known ?? false,
  processing_status: e.status ?? null,
});
const listing = (entries: Entry[], extra: object = {}) => ({
  playlist_id: "PLabcdefghijkl",
  title: "My list",
  total_count: entries.length,
  truncated: false,
  entries: entries.map(entry),
  ...extra,
});

let resolves: string[];
let batches: string[][];
let resolveResponder: () => Response | Promise<Response>;
let batchResponder: (urls: string[]) => Response | Promise<Response>;

beforeEach(() => {
  resolves = [];
  batches = [];
  resolveResponder = () => json(listing([{ video_id: A, title: "One" }]));
  batchResponder = (urls) =>
    json({
      counts: {},
      results: urls.map((u, index) => ({
        index,
        outcome: "queued",
        video_id: u,
        job_id: index,
      })),
    });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const path = new URL(String(input), "http://localhost").pathname;
      const body =
        init.body === undefined
          ? null
          : (JSON.parse(String(init.body)) as Record<string, never>);
      if (path === "/api/playlists/resolve") {
        resolves.push((body as unknown as { url: string }).url);
        return Promise.resolve(resolveResponder());
      }
      if (path === "/api/videos/batch") {
        const urls = (body as unknown as { urls: string[] }).urls;
        batches.push(urls);
        return Promise.resolve(batchResponder(urls));
      }
      return Promise.resolve(
        json({ items: [], total: 0, offset: 0, limit: 50 }),
      );
    }),
  );
});

function mount(entry = "/playlists/import") {
  const router = createMemoryRouter(routes, { initialEntries: [entry] });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function load(
  entries: Entry[] = [
    { video_id: A, title: "One" },
    { video_id: B, title: "Two" },
  ],
) {
  resolveResponder = () => json(listing(entries));
  mount();
  await user.type(
    screen.getByRole("textbox", { name: "YouTube playlist URL" }),
    PL,
  );
  await user.click(screen.getByRole("button", { name: "Show videos" }));
  await screen.findByRole("heading", { name: "My list" });
}

const box = (name: string) =>
  screen.getByRole("checkbox", { name }) as HTMLInputElement;
const btn = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;

describe("Import a playlist: page and listing", () => {
  it("shows the heading, the field and the button, and the Library links here", async () => {
    mount();
    expect(
      screen.getByRole("heading", { level: 1, name: "Import a playlist" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("textbox", { name: "YouTube playlist URL" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Show videos" })).toBeTruthy();
    expect(resolves).toEqual([]);
  });

  it("links from the Library", async () => {
    mount("/");
    const link = await screen.findByRole("link", { name: "Import a playlist" });
    expect(link.getAttribute("href")).toBe("/playlists/import");
  });

  it("sends one resolve, keeps the URL in the search params and lists the videos", async () => {
    resolveResponder = () => new Promise(() => undefined);
    const router = mount();
    await user.type(
      screen.getByRole("textbox", { name: "YouTube playlist URL" }),
      `  ${PL}  `,
    );
    await user.click(screen.getByRole("button", { name: "Show videos" }));
    expect(screen.getByRole("status").textContent).toBe(
      "Loading playlist… This can take up to a minute.",
    );
    expect(router.state.location.search).toBe(`?url=${encodeURIComponent(PL)}`);
    expect(resolves).toEqual([PL]);
  });

  it("shows the same listing on reload from the search params", async () => {
    mount(`/playlists/import?url=${encodeURIComponent(PL)}`);
    await screen.findByRole("heading", { name: "My list" });
    expect(resolves).toEqual([PL]);
    expect(
      (
        screen.getByRole("textbox", {
          name: "YouTube playlist URL",
        }) as HTMLInputElement
      ).value,
    ).toBe(PL);
  });

  it("drops an invalid url param without a request", async () => {
    const router = mount("/playlists/import?url=%20%20");
    await flush();
    expect(router.state.location.search).toBe("");
    expect(resolves).toEqual([]);
  });

  it("shows the title, count, table and one checkbox per new entry", async () => {
    await load([
      { video_id: A, title: "One" },
      { video_id: B, title: "" },
      { video_id: C, title: "Known", known: true, status: "done" },
      { video_id: D, title: "Gone", known: true, status: "deleted" },
    ]);
    expect(screen.getByText("4 videos")).toBeTruthy();
    const headers = screen
      .getAllByRole("columnheader")
      .map((h) => [h.textContent, h.getAttribute("scope")]);
    expect(headers).toEqual([
      ["Select", "col"],
      ["Title", "col"],
      ["Status", "col"],
    ]);
    expect(box("Select One").disabled).toBe(false);
    expect(box(`Select ${B}`).disabled).toBe(false);
    expect(box("Select Known").disabled).toBe(true);
    expect(screen.getByText("Already known · Analysed")).toBeTruthy();
    expect(box("Select Gone").disabled).toBe(true);
    expect(screen.getByText("Already known · Deleted")).toBeTruthy();
    expect(
      screen.getByText("Deleted · restore it from Recently deleted"),
    ).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Gone" })).toBeNull();
    expect(screen.getByRole("link", { name: "One" }).getAttribute("href")).toBe(
      `https://www.youtube.com/watch?v=${A}`,
    );
  });

  it("falls back to Untitled playlist and says when the playlist is empty", async () => {
    resolveResponder = () => json(listing([], { title: null }));
    mount(`/playlists/import?url=${encodeURIComponent(PL)}`);
    await screen.findByRole("heading", { name: "Untitled playlist" });
    expect(screen.getByText("This playlist has no videos")).toBeTruthy();
    expect(screen.getByText("0 videos")).toBeTruthy();
  });

  it("names the cap when the response is truncated", async () => {
    resolveResponder = () =>
      json(
        listing([{ video_id: A, title: "One" }], {
          truncated: true,
          total_count: 350,
        }),
      );
    mount(`/playlists/import?url=${encodeURIComponent(PL)}`);
    expect(
      await screen.findByText("Showing the first 200 of 350 videos"),
    ).toBeTruthy();
  });

  it("renders hostile text as text", async () => {
    await load([{ video_id: A, title: "<img src=x onerror=alert(1)>" }]);
    expect(
      screen.getByRole("link", { name: "<img src=x onerror=alert(1)>" }),
    ).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("Import a playlist: errors", () => {
  const cases: [number, string][] = [
    [
      422,
      "Enter a public YouTube playlist URL. Mixes, Liked videos and Watch later cannot be imported.",
    ],
    [404, "Playlist not found or not public"],
    [503, "YouTube is unavailable or limiting requests. Try again later."],
    [502, "Could not read the playlist. Try again later."],
    [429, "Too many requests. Try again shortly."],
    [500, "Could not load the playlist. Check your connection and try again."],
  ];
  it.each(cases)(
    "shows the fixed text for %i and keeps the input",
    async (status, text) => {
      resolveResponder = () => json({ detail: "yt-dlp said secret" }, status);
      mount();
      await user.type(
        screen.getByRole("textbox", { name: "YouTube playlist URL" }),
        PL,
      );
      await user.click(screen.getByRole("button", { name: "Show videos" }));
      expect((await screen.findByRole("alert")).textContent).toBe(text);
      expect(
        (
          screen.getByRole("textbox", {
            name: "YouTube playlist URL",
          }) as HTMLInputElement
        ).value,
      ).toBe(PL);
    },
  );

  it("shows the network text", async () => {
    resolveResponder = () => Promise.reject(new TypeError("Failed to fetch"));
    mount();
    await user.type(
      screen.getByRole("textbox", { name: "YouTube playlist URL" }),
      PL,
    );
    await user.click(screen.getByRole("button", { name: "Show videos" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Could not load the playlist. Check your connection and try again.",
    );
  });

  it("sends nothing for an empty field and allows a resend after an error", async () => {
    mount();
    await user.click(btn("Show videos"));
    expect(screen.getByRole("alert").textContent).toContain(
      "Enter a public YouTube playlist URL",
    );
    expect(resolves).toEqual([]);
    let fail = true;
    resolveResponder = () =>
      fail ? json({}, 503) : json(listing([{ video_id: A, title: "One" }]));
    await user.type(
      screen.getByRole("textbox", { name: "YouTube playlist URL" }),
      PL,
    );
    await user.click(btn("Show videos"));
    await screen.findByText(
      "YouTube is unavailable or limiting requests. Try again later.",
    );
    fail = false;
    await user.click(btn("Show videos"));
    await screen.findByRole("heading", { name: "My list" });
    expect(resolves).toEqual([PL, PL]);
  });
});

describe("Import a playlist: selection and submit", () => {
  it("selects all new, clears, and counts", async () => {
    await load();
    const group = screen.getByRole("group", { name: "Selection" });
    expect(within(group).getByText("No videos selected")).toBeTruthy();
    expect(btn("Add selected videos").disabled).toBe(true);
    await user.click(btn("Select all new"));
    expect(within(group).getByText("2 selected")).toBeTruthy();
    expect(btn("Add 2 selected videos").disabled).toBe(false);
    await user.click(btn("Clear selection"));
    expect(within(group).getByText("No videos selected")).toBeTruthy();
  });

  it("confirms inline with focus, Escape and Cancel", async () => {
    await load();
    await user.click(box("Select One"));
    await user.click(btn("Add 1 selected video"));
    expect(
      screen.getByText(
        "Add 1 video from this playlist? They are queued with new uploads and processed in order; this can take a while.",
      ),
    ).toBeTruthy();
    expect(document.activeElement).toBe(btn("Confirm"));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(document.activeElement).toBe(btn("Add 1 selected video"));
    await user.click(btn("Add 1 selected video"));
    await user.click(btn("Cancel"));
    expect(document.activeElement).toBe(btn("Add 1 selected video"));
    expect(batches).toEqual([]);
  });

  it("sends one batch in playlist order, lists results and refetches the playlist", async () => {
    await load([
      { video_id: A, title: "One" },
      { video_id: B, title: "Two" },
      { video_id: C, title: "Three" },
    ]);
    // Select out of playlist order: the request still follows the playlist.
    await user.click(box("Select Three"));
    await user.click(box("Select One"));
    await user.click(btn("Add 2 selected videos"));
    resolveResponder = () =>
      json(
        listing([
          { video_id: A, title: "One", known: true, status: "processing" },
          { video_id: B, title: "Two" },
          { video_id: C, title: "Three", known: true, status: "processing" },
        ]),
      );
    await user.click(btn("Confirm"));
    expect(await screen.findAllByText("Already known · Queued")).toHaveLength(
      2,
    );
    expect(batches).toEqual([[A, C]]);
    expect(resolves).toHaveLength(2);
    const items = within(
      screen.getByRole("region", { name: "Results" }),
    ).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      `Queued: ${A} Open`,
      `Queued: ${C} Open`,
    ]);
    expect(screen.getByText("Queued 2")).toBeTruthy();
    expect(box("Select Two").checked).toBe(false);
    expect(screen.getByText("No videos selected")).toBeTruthy();
  });

  it("disables the whole selection group while the batch is pending", async () => {
    await load();
    await user.click(btn("Select all new"));
    await user.click(btn("Add 2 selected videos"));
    batchResponder = () => new Promise(() => undefined);
    await user.click(btn("Confirm"));
    for (const name of [
      "Select all new",
      "Clear selection",
      "Add 2 selected videos",
      "Confirm",
      "Cancel",
    ])
      expect(btn(name).disabled).toBe(true);
    expect(box("Select One").disabled).toBe(true);
  });

  it("shows the #240 texts, retries only the failed ones and merges the answer", async () => {
    await load();
    await user.click(btn("Select all new"));
    await user.click(btn("Add 2 selected videos"));
    batchResponder = () =>
      json({
        counts: {},
        results: [
          { index: 0, outcome: "already_submitted", video_id: A },
          { index: 1, outcome: "cancellation_in_progress", video_id: B },
        ],
      });
    await user.click(btn("Confirm"));
    const list = () =>
      within(screen.getByRole("region", { name: "Results" }))
        .getAllByRole("listitem")
        .map((li) => li.textContent);
    await screen.findByText("Already added 1");
    expect(list()).toEqual([
      `Already added: ${A} Open`,
      `${B}: still being deleted, try again in a minute`,
    ]);
    batchResponder = (urls) =>
      json({
        counts: {},
        results: [{ index: 0, outcome: "failed", video_id: urls[0] }],
      });
    await user.click(btn("Retry these"));
    expect(batches[1]).toEqual([B]);
    await screen.findByText(`${B}: could not be added`);
    expect(list()[0]).toBe(`Already added: ${A} Open`);
  });

  it("keeps the selection and shows fixed text when the batch fails, then allows a resend", async () => {
    await load();
    await user.click(btn("Select all new"));
    await user.click(btn("Add 2 selected videos"));
    batchResponder = () => json({ detail: "boom" }, 503);
    await user.click(btn("Confirm"));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "The service is unavailable. Try again shortly.",
    );
    expect(screen.getByText("2 selected")).toBeTruthy();
    batchResponder = (urls) =>
      json({
        counts: {},
        results: urls.map((u, index) => ({
          index,
          outcome: "queued",
          video_id: u,
        })),
      });
    await user.click(btn("Add 2 selected videos"));
    await user.click(btn("Confirm"));
    await screen.findByText("Queued 2");
    expect(batches).toHaveLength(2);
  });

  it("loading another playlist clears the selection", async () => {
    await load();
    await user.click(btn("Select all new"));
    resolveResponder = () =>
      json(
        listing([
          { video_id: A, title: "One" },
          { video_id: B, title: "Two" },
        ]),
      );
    const field = screen.getByRole("textbox", { name: "YouTube playlist URL" });
    await user.clear(field);
    await user.type(
      field,
      "https://www.youtube.com/playlist?list=PLzzzzzzzzzzzz",
    );
    await user.click(btn("Show videos"));
    await screen.findByText("No videos selected");
    expect(box("Select One").checked).toBe(false);
  });

  it("stays latched in the window between the settle and the re-render", async () => {
    await load();
    await user.click(btn("Select all new"));
    await user.click(btn("Add 2 selected videos"));
    const confirm = btn("Confirm");
    // One act: the response settles, then a click arrives on the Confirm
    // button of the render before the settle.
    await act(async () => {
      fireEvent.click(confirm);
      for (let i = 0; i < 20; i += 1) await Promise.resolve();
      fireEvent.click(confirm);
    });
    await flush();
    expect(batches).toHaveLength(1);
  });
});
