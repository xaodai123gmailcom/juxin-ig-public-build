import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";

type RouterLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  href: string;
  children?: ReactNode;
};

export default function RouterLink({ href, onClick, children, ...props }: RouterLinkProps) {
  const hashHref = href === "/" ? "#/" : `#${href}`;
  const navigate = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (window.location.hash === hashHref) window.dispatchEvent(new HashChangeEvent("hashchange"));
    else window.location.hash = hashHref;
  };
  return <a href={hashHref} onClick={navigate} {...props}>{children}</a>;
}
