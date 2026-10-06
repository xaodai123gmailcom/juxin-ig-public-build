import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { writePrivateFileAtomically } from "../../dist-electron/atomic-secure-store.js";

function withTemporaryDirectory(run) {
  const directory = mkdtempSync(join(tmpdir(), "igac-secure-store-"));
  try {
    return run(directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("secure store commits by private same-directory atomic replacement", () => {
  withTemporaryDirectory((directory) => {
    const target = join(directory, "secure-store.json");
    writeFileSync(target, '{"old":true}', "utf8");
    writePrivateFileAtomically(target, '{"session-token":"encrypted"}');
    assert.equal(readFileSync(target, "utf8"), '{"session-token":"encrypted"}');
    if (process.platform !== "win32") assert.equal(statSync(target).mode & 0o777, 0o600);
    assert.deepEqual(readdirSync(directory), ["secure-store.json"]);
  });
});

test("failed atomic commit removes the uncommitted temporary file", () => {
  withTemporaryDirectory((directory) => {
    const blockedTarget = join(directory, "secure-store.json");
    mkdirSync(blockedTarget);
    assert.throws(() => writePrivateFileAtomically(blockedTarget, "{}"));
    assert.deepEqual(readdirSync(directory), ["secure-store.json"]);
  });
});
