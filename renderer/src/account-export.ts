import type { CoreAccountExport } from "./core-client";

/** Uses Chromium/Electron's normal download flow, like diagnostic exports. */
export function downloadAccountExport(result: CoreAccountExport): void {
  if (!result || typeof result.csv !== "string" || !result.csv.startsWith("\ufeff")
      || !Number.isSafeInteger(result.row_count) || result.row_count < 1
      || !/^Juxin-(public|private)-accounts-\d{8}-\d{6}\.csv$/.test(result.filename)
      || result.mime_type !== "text/csv;charset=utf-8") {
    throw new Error("账号导出文件无效，请重新导出");
  }
  const url = URL.createObjectURL(new Blob([result.csv], { type: result.mime_type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = result.filename;
  link.style.display = "none";
  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    // Do not revoke synchronously: Electron must first start consuming the URL.
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}
