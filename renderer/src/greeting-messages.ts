export const MAX_GREETING_MESSAGE_CHARACTERS = 200;

export type GreetingMessageParseResult =
  | { ok: true; messages: string[] }
  | {
      ok: false;
      code: "empty" | "too_long";
      error: string;
      lineNumber?: number;
      characterCount?: number;
    };

function characterCount(value: string): number {
  // Python validates greeting length by Unicode code point. Array.from mirrors
  // that behavior more closely than JavaScript's UTF-16 `string.length`, so an
  // emoji is not incorrectly counted as two characters in the renderer.
  return Array.from(value).length;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function nonEmptyText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** Resolve the text proven by one successful greeting attempt.
 *
 * New attempts persist the chosen message in immutable attempt details. The
 * direct attempt-level field is retained as a compatibility boundary, while a
 * campaign message is valid only for legacy one-message campaigns. Follow
 * actions never expose a greeting message.
 */
export function resolveSuccessfulGreetingMessage(
  operation: string,
  attempt: unknown,
  campaignMessage: unknown,
): string | null {
  if (operation !== "greet") return null;
  const attemptRecord = recordValue(attempt);
  const details = recordValue(attemptRecord.details);
  return nonEmptyText(details.greeting_message)
    ?? nonEmptyText(attemptRecord.message)
    ?? nonEmptyText(campaignMessage);
}

export function parseGreetingMessages(input: string): GreetingMessageParseResult {
  const messages = input
    .split(/\r?\n/)
    .map((value, index) => ({ value: value.trim(), lineNumber: index + 1 }))
    .filter((item) => item.value.length > 0);

  if (!messages.length) {
    return {
      ok: false,
      code: "empty",
      error: "请至少输入一条非空打招呼话术。",
    };
  }

  for (const item of messages) {
    const length = characterCount(item.value);
    if (length > MAX_GREETING_MESSAGE_CHARACTERS) {
      return {
        ok: false,
        code: "too_long",
        lineNumber: item.lineNumber,
        characterCount: length,
        error: `第 ${item.lineNumber} 行话术共 ${length} 个字符，超过 ${MAX_GREETING_MESSAGE_CHARACTERS} 字符上限，请缩短后再执行。`,
      };
    }
  }

  return { ok: true, messages: messages.map((item) => item.value) };
}
