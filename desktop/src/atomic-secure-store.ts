import { chmodSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { randomBytes } from "node:crypto";

/**
 * Replace one private file atomically using a temporary file in the same
 * directory. Same-directory rename is the commit point; a failed write or
 * rename leaves the previous file intact and removes the uncommitted temp.
 */
export function writePrivateFileAtomically(targetPath: string, contents: string) {
  const temporaryPath = join(
    dirname(targetPath),
    `.${basename(targetPath)}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`,
  );
  let committed = false;
  try {
    writeFileSync(temporaryPath, contents, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
      // Close alone does not flush pending file contents to the device. Flush
      // the replacement before publishing its name (supported by Node 22).
      flush: true,
    });
    // Apply the private mode explicitly before the rename so an existing file
    // with broader permissions can never lend those permissions to the update.
    chmodSync(temporaryPath, 0o600);
    renameSync(temporaryPath, targetPath);
    committed = true;
  } finally {
    if (!committed) {
      try {
        unlinkSync(temporaryPath);
      } catch (reason) {
        const code = reason && typeof reason === "object" && "code" in reason
          ? String((reason as { code?: unknown }).code)
          : "";
        if (code !== "ENOENT") throw reason;
      }
    }
  }
}
