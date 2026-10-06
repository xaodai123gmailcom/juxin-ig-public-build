// Lists format thousands of values on each refresh. Share the expensive locale
// formatters instead of constructing one for every table cell.
const countFormatter = new Intl.NumberFormat("zh-CN");
const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "numeric", day: "numeric",
  hour: "numeric", minute: "numeric", second: "numeric", hour12: false,
});

export function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? countFormatter.format(value) : "—";
}

export function formatTime(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : timeFormatter.format(date);
}
