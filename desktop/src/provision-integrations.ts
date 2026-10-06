/** Runs only in the main process; credentials never enter renderer state. */
export function provisionOpenAI(
  read: (key: string) => string | null,
): string {
  // Only an explicitly configured value in the OS-backed secure store can
  // enable this integration. There is no bundled or plaintext fallback.
  return read('openai-api-key') || '';
}
