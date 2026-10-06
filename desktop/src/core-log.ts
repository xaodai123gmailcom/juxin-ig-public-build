import { StringDecoder } from "node:string_decoder";
import type { Readable } from "node:stream";

/** Drain both Core pipes, with bounded buffering and local credential redaction. */
export function captureCoreLog(stream: Readable | null, channel: string, write: (line: string) => void, secrets: readonly string[]) {
  if (!stream) return;
  const decoder = new StringDecoder("utf8");
  let pending = "";
  let discarding = false;
  const redact = (line: string) => {
    for (const value of secrets) if (value) line = line.split(value).join("[REDACTED]");
    return line.replace(/Bearer\s+[^\s,;]+/gi, "Bearer [REDACTED]")
      .replace(/((?:["']?)\b(?:password|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|set-cookie)(?:["']?)\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]*)/gi, "$1[REDACTED]");
  };
  const flush = (text: string) => { try { write(`[Core ${channel}] ${redact(text).slice(0, 8192)}`); } catch { /* Logging must never block draining Core. */ } };
  const accept = (text: string) => {
    let start = 0;
    while (start < text.length) {
      const newline = text.indexOf("\n", start);
      const end = newline < 0 ? text.length : newline;
      if (!discarding) {
        if (pending.length + end - start > 16384) { pending = ""; discarding = true; }
        else pending += text.slice(start, end);
      }
      if (newline < 0) break;
      flush(discarding ? "[overlong Core log record omitted]" : pending);
      pending = ""; discarding = false; start = newline + 1;
    }
  };
  stream.on("data", (chunk: Buffer | string) => accept(typeof chunk === "string" ? chunk : decoder.write(chunk)));
  stream.on("end", () => {
    accept(decoder.end());
    if (discarding || pending) flush(discarding ? "[overlong Core log record omitted]" : pending);
    pending = ""; discarding = false;
  });
  stream.on("error", () => flush("log stream ended unexpectedly"));
}
