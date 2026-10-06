import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { reconcileWindowOrder, retainEqualOrder, retainLiveSelection, sameComponentProps, sameReviewWorkspaceInputs } from "../src/workbench-render-state.ts";

function reviewInputs() {
  return {
    snapshot: {
      generated_at: "2026-09-17T00:00:00Z", tasks: [], sources: [], windows: [],
      pending: { public: [], private: [] }, split_candidates: [],
      counts: { pending_public: 10, pending_private_primary: 8, pending_private_secondary: 2, pending_private: 10, pending_review: 20 },
      dedupe: { total: 20000 }, has_more: {}, truncated: false,
    }, run: () => {}, disabled: () => false,
  };
}

test("task-only heartbeats do not repeat a review workspace render", () => {
  const previous = reviewInputs();
  let renders = 1;
  for (let tick = 1; tick <= 24; tick++) {
    const next = { ...previous, snapshot: { ...previous.snapshot,
      generated_at: `tick-${tick}`, tasks: [{ id: "task", processed: tick }],
      sources: [{ id: "target", processed: tick }], windows: [{ id: "busy", locked: true }],
    } };
    if (!sameReviewWorkspaceInputs(previous, next)) renders++;
  }
  assert.equal(renders, 1, "memo input simulation: 24 unchanged review heartbeats");
});

test("every visible review input or changed command/permission callback invalidates the workspace memo", () => {
  const previous = reviewInputs();
  const changes = [
    ["run", (p) => ({ ...p, run: () => {} })],
    ["disabled", (p) => ({ ...p, disabled: () => true })],
    ["public pending", (p) => ({ ...p, snapshot: { ...p.snapshot, pending: { ...p.snapshot.pending, public: [{}] } } })],
    ["private pending", (p) => ({ ...p, snapshot: { ...p.snapshot, pending: { ...p.snapshot.pending, private: [{}] } } })],
    ["split candidates", (p) => ({ ...p, snapshot: { ...p.snapshot, split_candidates: [{}] } })],
    ["dedupe total", (p) => ({ ...p, snapshot: { ...p.snapshot, dedupe: { total: 20001 } } })],
    ["has more", (p) => ({ ...p, snapshot: { ...p.snapshot, has_more: { pending_public: true } } })],
    ["truncated", (p) => ({ ...p, snapshot: { ...p.snapshot, truncated: true } })],
    ...Object.keys(previous.snapshot.counts).map((key) => [key, (p) => ({ ...p, snapshot: { ...p.snapshot, counts: { ...p.snapshot.counts, [key]: p.snapshot.counts[key] + 1 } } })]),
  ];
  for (const [label, change] of changes) assert.equal(sameReviewWorkspaceInputs(previous, change(previous)), false, label);
});

function rowInputs(id = "candidate") {
  return {
    item: { id, profile: { avatar_url: "https://example.invalid/avatar.jpg" } },
    queue: "public", visibility: "public", selected: false, reviewMutationBusy: false,
    disabled: () => false, splitUsernames: new Set(), toggleCandidateSelection: () => {},
    decideCandidate: async () => {}, addReviewCandidateToSplit: async () => {},
  };
}

test("row memo compares every prop including content, tier, selection, busy state and callbacks", () => {
  const previous = rowInputs();
  assert.equal(sameComponentProps(previous, { ...previous }), true);
  for (const key of Object.keys(previous)) {
    const value = previous[key];
    const replacement = typeof value === "function" ? () => {} : typeof value === "boolean" ? !value
      : typeof value === "string" ? "private" : value instanceof Set ? new Set(["candidate"]) : { ...value, changed: true };
    assert.equal(sameComponentProps(previous, { ...previous, [key]: replacement }), false, key);
  }
  assert.equal(sameComponentProps(previous, { ...previous, onProfile: () => {} }), false, "new props cannot be silently ignored");
  const removed = { ...previous }; delete removed.disabled;
  assert.equal(sameComponentProps(previous, removed), false);
});

test("one checkbox affects one row; batch busy and permission changes still affect all 2000 rows", () => {
  const shared = rowInputs();
  const rows = Array.from({ length: 2000 }, (_, index) => ({ ...shared, item: { id: `candidate-${index}` } }));
  const changed = (next) => rows.filter((previous, index) => !sameComponentProps(previous, next[index])).length;
  assert.equal(changed(rows.map((row, index) => ({ ...row, selected: index === 18 }))), 1);
  assert.equal(changed(rows.map((row) => ({ ...row, selected: true }))), 2000);
  assert.equal(changed(rows.map((row) => ({ ...row, reviewMutationBusy: true }))), 2000);
  const disabled = () => true;
  assert.equal(changed(rows.map((row) => ({ ...row, disabled }))), 2000);
  assert.equal(changed(rows.map((row, index) => index === 18 ? { ...row, item: { ...row.item, profile: { posts: 25 } } } : row)), 1);
});

test("unchanged window lists retain state while removals/additions preserve operator order", () => {
  const order = ["second", "first"];
  assert.equal(reconcileWindowOrder(order, ["first", "second"]), order);
  assert.equal(retainEqualOrder(order, ["second", "first"]), order);
  assert.deepEqual(reconcileWindowOrder(order, ["first", "new"]), ["first", "new"]);
  assert.deepEqual(retainEqualOrder(order, ["first", "second"]), ["first", "second"]);
  assert.deepEqual(order, ["second", "first"], "never mutate the current selection order");
});

test("identical selection refreshes preserve identity but unavailable or locked IDs are removed", () => {
  const selected = new Set(["open", "later-locked"]);
  assert.equal(retainLiveSelection(selected, new Set(["open", "later-locked", "new"])), selected);
  const retained = retainLiveSelection(selected, new Set(["open"]));
  assert.notEqual(retained, selected);
  assert.deepEqual([...retained], ["open"]);
  assert.deepEqual([...selected], ["open", "later-locked"]);
  const empty = new Set();
  assert.equal(retainLiveSelection(empty, new Set(["new"])), empty);
});

test("memoized rows are wired with stable handlers and preserve batch scope, tier state and avatar identity", () => {
  const source = readFileSync(new URL("../src/formal-workbench.tsx", import.meta.url), "utf8");
  assert.match(source, /const StableReviewWorkspace = memo\(ReviewWorkspace, sameReviewWorkspaceInputs\)/);
  assert.match(source, /mode === "review"\) return <StableReviewWorkspace/);
  assert.match(source, /const ReviewCandidateRow = memo\(function ReviewCandidateRow[\s\S]*?\}, sameComponentProps\)/);
  const rowType = source.match(/type ReviewCandidateRowProps = \{([\s\S]*?)\n\};/)[1];
  const declaredProps = [...rowType.matchAll(/^  (\w+):/gm)].map((match) => match[1]);
  assert.deepEqual(declaredProps.sort(), Object.keys(rowInputs()).sort(), "new row props require memo invalidation coverage");
  assert.match(source, /const toggleCandidateSelection = useCallback\([\s\S]*?\}, \[\]\)/);
  assert.match(source, /const decideCandidate = useCallback\([\s\S]*?\}, \[run\]\)/);
  assert.match(source, /const addReviewCandidateToSplit = useCallback\([\s\S]*?\}, \[run\]\)/);
  assert.match(source, /<ReviewCandidateRow key=\{item\.id\}[\s\S]*?selected=\{selectedCandidateIds\.has\(item\.id\)\}/);
  assert.match(source, /setSelectedCandidateIds\(allCandidatesSelected \? new Set\(\) : new Set\(candidates\.map/);
  assert.match(source, /setSelectedCandidateIds\(new Set\(\)\);\s*setReviewView\(next\);\s*setReviewListOpen\(true\)/);
  assert.match(source, /<img src=\{avatar\} alt="" loading="lazy"/);
  assert.match(source, /open=\{reviewListOpen\}/);
});

test("window effects use equality-preserving setters and remove occupied windows from dispatch order", () => {
  const source = readFileSync(new URL("../src/formal-workbench.tsx", import.meta.url), "utf8");
  assert.match(source, /setOrderedIds\(\(previous\) => reconcileWindowOrder\(previous,/);
  assert.match(source, /setSelectedOrder\(\(previous\) => retainEqualOrder\(previous, orderedSelected\)\)/);
  assert.match(source, /selected\.has\(item\.id\) && live\.has\(item\.id\)/);
  assert.match(source, /setSelectedAccounts\(\(current\) => retainLiveSelection\(current, live\)\)/);
  assert.match(source, /const visibleWindows = useMemo\([\s\S]*?\[instagramOnly, snapshot\.windows\]\)/);
  assert.match(source, /const \[targetDraft, setTargetDraft\] = useCollectionDraft\(\)/);
  assert.match(source, /value=\{targetDraft\} onChange=\{\(event\) => updateTargetDraft\(event\.target\.value\)\}/);
  for (const [value, setter] of [["messages", "setMessages"], ["port", "setPort"]]) {
    assert.match(source, new RegExp(`const \\[${value}, ${setter}\\] = useState\\(""\\)`));
    assert.match(source, new RegExp(`value=\\{${value}\\} onChange=\\{\\(event\\) => ${value === "targetDraft" ? "updateTargetDraft" : setter}\\(event\\.target\\.value\\)\\}`));
  }
});
