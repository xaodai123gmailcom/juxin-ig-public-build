/** Keep the collection controls at the viewport edge without hiding the last rows.
 * The in-flow anchor supplies the actual content width, so rail and window size
 * changes do not require a second set of hard-coded sidebar dimensions.
 */
export function bindCollectionFixedFooter(anchor: HTMLElement, bar: HTMLElement): () => void {
  const view = anchor.ownerDocument.defaultView;
  if (!view) return () => {};
  let disposed = false;
  let frame: number | null = null;
  const write = (element: HTMLElement, key: "left" | "width" | "height", value: string) => {
    if (element.style[key] !== value) element.style[key] = value;
  };
  const measure = () => {
    frame = null;
    if (disposed || !anchor.isConnected || !bar.isConnected) return;
    const rect = anchor.getBoundingClientRect();
    if (rect.width <= 0) return;
    write(bar, "left", `${rect.left}px`);
    write(bar, "width", `${rect.width}px`);
    bar.dataset.fixed = "true";
    const bottom = Number.parseFloat(view.getComputedStyle(bar).bottom) || 0;
    write(anchor, "height", `${Math.ceil(bar.getBoundingClientRect().height + bottom)}px`);
  };
  const schedule = () => {
    if (!disposed && frame === null) frame = view.requestAnimationFrame(measure);
  };
  // Read before the first paint; ResizeObserver then follows wrapping, option
  // hints, accessibility zoom, and the available width of the current page.
  measure();
  const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
  observer?.observe(anchor);
  observer?.observe(bar);
  if (anchor.parentElement) observer?.observe(anchor.parentElement);
  view.addEventListener("resize", schedule);
  view.addEventListener("scroll", schedule, true);
  view.visualViewport?.addEventListener("resize", schedule);
  return () => {
    disposed = true;
    observer?.disconnect();
    if (frame !== null) view.cancelAnimationFrame(frame);
    view.removeEventListener("resize", schedule);
    view.removeEventListener("scroll", schedule, true);
    view.visualViewport?.removeEventListener("resize", schedule);
    delete bar.dataset.fixed;
    bar.style.removeProperty("left");
    bar.style.removeProperty("width");
    anchor.style.removeProperty("height");
  };
}
