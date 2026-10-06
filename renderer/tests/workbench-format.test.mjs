import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { formatCount, formatTime } from "../src/workbench-format.ts";

test("shared number formatting preserves counts, missing values and non-finite handling", () => {
  const previous = (value) => typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("zh-CN").format(value) : "—";
  for (const value of [0, -0, 1, -1, 20347, 99999999, 1.23456, Number.MAX_SAFE_INTEGER, NaN, Infinity, null, undefined, "123"]) {
    assert.equal(formatCount(value), previous(value), String(value));
  }
});

test("shared time formatting preserves valid timestamps and invalid/missing value fallbacks", () => {
  const previous = (value) => {
    if (typeof value !== "string" && typeof value !== "number") return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  };
  for (const value of [0, -1, 1789635746000, "2026-09-16T10:22:26Z", "2024-02-29T23:59:59Z", "2026-09-16T00:00:00Z", "invalid", "", NaN, Infinity, null, undefined, {}, false]) {
    assert.equal(formatTime(value), previous(value), String(value));
  }
});

test("display times keep the local timezone and daylight-saving behavior", () => {
  const moduleUrl = new URL("../src/workbench-format.ts", import.meta.url).href;
  const script = `
    import assert from "node:assert/strict";
    const { formatTime } = await import(${JSON.stringify(moduleUrl)});
    for (const value of ["2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-03-08T09:59:59Z", "2026-03-08T10:00:00Z"]) {
      assert.equal(formatTime(value), new Date(value).toLocaleString("zh-CN", { hour12: false }));
    }
  `;
  for (const timezone of ["UTC", "Asia/Bangkok", "America/Los_Angeles"]) {
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], { env: { ...process.env, TZ: timezone }, encoding: "utf8" });
    assert.equal(result.status, 0, `${timezone}: ${result.stderr}`);
  }
});

test("a 2000-row refresh creates locale formatters once rather than once per cell", async () => {
  const constructors = { NumberFormat: Intl.NumberFormat, DateTimeFormat: Intl.DateTimeFormat };
  const created = { NumberFormat: 0, DateTimeFormat: 0 };
  try {
    for (const name of Object.keys(constructors)) {
      Intl[name] = new Proxy(constructors[name], {
        construct(target, args) { created[name]++; return Reflect.construct(target, args); },
      });
    }
    const fresh = await import(`../src/workbench-format.ts?formatter-lifetime`);
    for (let row = 0; row < 2000; row++) {
      fresh.formatCount(row); fresh.formatCount(518); fresh.formatCount(170);
      fresh.formatTime("2026-09-16T10:22:26Z");
    }
    assert.deepEqual(created, { NumberFormat: 1, DateTimeFormat: 1 });
  } finally {
    Object.assign(Intl, constructors);
  }
});
