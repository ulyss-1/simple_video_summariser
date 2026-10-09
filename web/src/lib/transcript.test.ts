import { describe, expect, it } from "vitest";
import {
  formatCount,
  languageLabel,
  lastPage,
  MAX_PAGE,
  PAGE_SIZE,
  parsePage,
  sourceLabel,
  speakerLabels,
} from "./transcript";

const q = (s: string) => new URLSearchParams(s);

describe("parsePage", () => {
  it("uses page 1, not invalid, when absent", () => {
    expect(parsePage(q(""))).toEqual({ page: 1, invalid: false });
  });

  it.each([
    ["page=1", 1],
    ["page=2", 2],
    ["page=13", 13],
    [`page=${MAX_PAGE}`, MAX_PAGE],
  ])("accepts %s", (s, page) => {
    expect(parsePage(q(s))).toEqual({ page, invalid: false });
  });

  it("caps the page at the API offset cap: (N - 1) * 200 <= 1_000_000", () => {
    expect(MAX_PAGE).toBe(5001);
    expect((MAX_PAGE - 1) * PAGE_SIZE).toBe(1_000_000);
    expect(parsePage(q(`page=${MAX_PAGE + 1}`))).toEqual({
      page: 1,
      invalid: true,
    });
  });

  it.each([
    "0",
    "-1",
    "1.5",
    "abc",
    "1e3",
    "",
    "99999999999999999999",
    "01",
    "+2",
    " 2",
    "2 ",
    "0x10",
    "NaN",
    "Infinity",
    "<script>alert(1)</script>",
    "2%00",
  ])("falls back to page 1 and flags %j", (v) => {
    const params = new URLSearchParams();
    params.set("page", v);
    expect(parsePage(params)).toEqual({ page: 1, invalid: true });
  });

  it("rejects a repeated parameter", () => {
    expect(parsePage(q("page=2&page=3"))).toEqual({ page: 1, invalid: true });
    expect(parsePage(q("page=2&page=2"))).toEqual({ page: 1, invalid: true });
  });
});

describe("lastPage", () => {
  it.each([
    [0, 1],
    [1, 1],
    [200, 1],
    [201, 2],
    [2500, 13],
    [400, 2],
  ])("total %i -> page %i", (total, expected) => {
    expect(lastPage(total)).toBe(expected);
  });

  it("never exceeds the offset cap", () => {
    expect(lastPage(10_000_000)).toBe(MAX_PAGE);
  });
});

describe("sourceLabel", () => {
  it.each([
    ["youtube_manual", "YouTube captions (manual)"],
    ["youtube_auto", "YouTube auto-generated captions"],
    ["whisper", "Machine transcription"],
    ["something_new", "something_new"],
    ["", ""],
    ["<b>x</b>", "<b>x</b>"],
  ])("%j -> %j", (source, label) => {
    expect(sourceLabel(source)).toBe(label);
  });
});

describe("languageLabel", () => {
  it("shows the language as stored", () => {
    expect(languageLabel("en")).toBe("en");
    expect(languageLabel("pt-BR")).toBe("pt-BR");
  });
  it("says unknown for null", () => {
    expect(languageLabel(null)).toBe("Language unknown");
  });
});

describe("speakerLabels", () => {
  const sp = (...s: Array<string | null>) =>
    speakerLabels(s.map((speaker) => ({ speaker })));

  it("labels the first segment and every change", () => {
    expect(sp("A", "A", "B", "B", "A")).toEqual(["A", null, "B", null, "A"]);
  });

  it("counts null as a value: A, A, null, A -> A on the 1st and 4th", () => {
    expect(sp("A", "A", null, "A")).toEqual(["A", null, null, "A"]);
  });

  it("shows nothing when every speaker is null", () => {
    expect(sp(null, null, null)).toEqual([null, null, null]);
  });

  it("treats an empty or blank speaker as none", () => {
    expect(sp("", "A", "  ", "A")).toEqual([null, "A", null, "A"]);
  });

  it("keeps a speaker name as stored, whitespace included", () => {
    // Names are shown as stored, so "A" and "  A  " are different speakers.
    expect(sp("  A  ", "  A  ", "A")).toEqual(["  A  ", null, "A"]);
  });

  it("handles an empty page", () => {
    expect(sp()).toEqual([]);
  });

  it("keeps names verbatim", () => {
    expect(sp('"><img src=x onerror=alert(1)>')).toEqual([
      '"><img src=x onerror=alert(1)>',
    ]);
  });
});

describe("formatCount", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1000, "1 000"],
    [2500, "2 500"],
    [1234567, "1 234 567"],
  ])("%i -> %j", (n, s) => {
    expect(formatCount(n)).toBe(s);
  });
});
