import { useLayoutEffect, useRef, type ReactNode } from "react";
import { bindCollectionFixedFooter } from "./collection-fixed-footer-layout";
import "./collection-fixed-footer.css";

export function CollectionFixedFooter({ children }: { children: ReactNode }) {
  const anchor = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!anchor.current || !bar.current) return;
    return bindCollectionFixedFooter(anchor.current, bar.current);
  }, []);
  return <div ref={anchor} className="collection-control-anchor">
    <div ref={bar} className="collection-control-bar" role="region" aria-label="采集任务调度">{children}</div>
  </div>;
}
