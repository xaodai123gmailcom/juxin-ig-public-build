// Small DOM test adapter. The production extraction JavaScript is supplied on
// stdin and executed unchanged; fixtures model row ancestry, visibility and order.
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor({ tag = "div", attrs = {}, text = "", children = [] }, parent = null) {
    this.tagName = tag.toLowerCase();
    this.attrs = attrs;
    this.text = text;
    this.parentElement = parent;
    this.children = children.map(child => new Element(child, this));
    this.hidden = Boolean(attrs.hidden);
    this.clientHeight = Number(attrs.clientHeight || 0);
    this.scrollHeight = Number(attrs.scrollHeight || this.clientHeight);
  }
  getAttribute(name) { return this.attrs[name] == null ? null : String(this.attrs[name]); }
  get innerText() { return [this.text, ...this.children.map(child => child.innerText)].filter(Boolean).join("\n"); }
  get textContent() { return this.innerText; }
  matches(selectors) {
    return selectors.split(",").some(selector => {
      if (selector.trim() === "*") return true;
      const match = selector.trim().match(/^([a-z\d]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i);
      if (!match) throw new Error(`Unsupported fixture selector: ${selector}`);
      return (!match[1] || this.tagName === match[1].toLowerCase())
        && (!match[2] || (this.getAttribute(match[2]) != null && (match[3] == null || this.getAttribute(match[2]) === match[3])));
    });
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [ ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector) ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
    return null;
  }
  contains(other) {
    for (let node = other; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  getClientRects() {
    for (let node = this; node; node = node.parentElement) if (node.hidden || node.attrs.display === "none") return [];
    return [{ width: 120, height: 32 }];
  }
  getBoundingClientRect() {
    const top = Number(this.attrs.top || 0), height = Number(this.attrs.height || 32);
    return this.getClientRects().length
      ? { top, left: 0, right: 120, bottom: top + height, width: 120, height }
      : { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const extract = vm.runInNewContext(`(${input.script})`, {
  URL,
  getComputedStyle: node => ({ display: node.attrs.display || "block", visibility: node.attrs.visibility || "visible",
    overflowY: node.attrs.overflowY || "visible", overflowX: node.attrs.overflowX || "visible" }),
});
process.stdout.write(JSON.stringify(input.fixtures.map(tree => extract(new Element(tree)))));
