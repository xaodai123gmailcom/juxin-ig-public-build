# Exact snapshot aggregates

The retained business ledgers remain authoritative. No Instagram record, profile,
review, action success, exclusion, source checkpoint, or alias is removed by this
optimization. No timer, TTL, stale-count fallback, or background history scan is
used. The public snapshot and progress response structures remain unchanged.

## Query design

Exact lifetime-history counts can grow even when visible payloads are capped.
The projections materialize only the identity, owner, target, mode and integer
membership needed to answer those sums. Performance must be measured for the
current source and environment; this document makes no historical benchmark claim.

## Snapshot counts and actionable pages

`workbench_aggregates.py` maintains small exact owner/global counters and a sparse
approved-candidate membership index. Candidate pages still load original rows.
Eligibility tracks canonical account scope, owner, candidate status/visibility,
dismissal, aliases, and matching owner/operation action-success ledger entries.
A completed or dismissed historical backlog is skipped before the page LIMIT.

## Source progress

`workbench_progress_aggregates.py` maintains small source/mode totals from compact
per-identity evidence. Every relevant result, claim, candidate, exclusion, or task
owner mutation refreshes only its affected identities using unique-key joins.
The refresh runs synchronously in the source transaction. A no-row insert-only
view shares its SQL body across source triggers; it is not a queue or worker.

The exact former provenance rules remain:

- Modern reviews must match the original result's target and source, result task
  owner, and identity claim owner/target/source
- Legacy source-less reviews count only when an unambiguous claim and original
  result prove the exact source mode and target
- Review status never erases an admitted-to-review count
- Direct exclusions, hover exclusions, and global duplicates remain distinct
- Legacy checkpoint-only progress remains unknown unless stored result/exclusion
  evidence proves zero admissions
- Invalid/non-array result sources cannot create source evidence

The poll now seeks displayed target IDs in `workbench_progress_totals`, while the
existing discovery/processed/checkpoint recovery logic remains unchanged.

## Startup, restore, and safety

Versions 40 and 41 backfill once, after the existing pure-Instagram and legacy
repairs. Missing projection tables/triggers are rebuilt. Ordinary restarts do not
recount permanent business history. SQLite triggers remain active for compatible
older writers, and rollback restores both original rows and exact projections.
Bulk cloud restore explicitly rebuilds both projections after restoring and
purging rows, before the existing foreign-key validation and transaction commit.

## Verification

- `test_workbench_progress_aggregates_r5.py`: all 24 insert permutations, 160
  deterministic provenance/owner/status/source-JSON mutations, deletion cascades,
  rollback, missing/modified-trigger repair, no-op/payload-only update avoidance,
  opaque legacy JSON-mode equivalence, explicit restore rebuild, and no history
  rebuild on a normal restart
- The same test compares complete progress output with a test-only verbatim copy
  of the previous implementation, retained in `support/progress_aggregate_reference.py`
- Existing progress, snapshot-scale, storage, backpressure, live-target, and wire
  regressions retain their semantic checks; plan assertions now require exact
  indexed projection reads and bounded actionable pages
- Run `scripts/benchmark_pure_ig_acceptance.py` for current synthetic route and
  callback measurements; isolated progress timings are not application benchmarks

## Additional completed-source and task-history axis

A separate real-route probe grows retained completed sources/tasks rather than
only identities/results. It retains four active source/window bindings and
normally dismisses half the historical cards. The former target window-sort and
COUNT scanned lifetime sources even though the detail payload was capped.

Two additive indexes now match the existing task and target priority orders.
Bounded task details first seek each selected task's requested IDs plus one ID
sentinel, then load payloads only for the selected page. The sentinel replaces an
internal COUNT used solely for `targets_truncated`; no public total is removed.
Window selection, fair sharing, target status priorities, queue/id ordering,
dismissal flags, page offsets, and full-history APIs retain their previous
behavior. Bounded ID selection and payload loading share one SQLite read snapshot.

`test_snapshot_target_priority_r5.py` compares full outputs with the former query
implementation across 60 limit/offset/detail-budget combinations, mixed active,
paused, terminal-cleanup, dismissed and foreign-owner rows. It also checks status,
owner, dismissal and order mutations; concurrent completion between ID selection
and payload read; matching index plans; and flat VM work from 1k to 20k sources.
Existing payload instrumentation verifies only returned target payloads are read.

## Measurement boundary

When nearly all historical cards are dismissed, proving that no more visible
task exists may require examining their exclusion markers. Explicit full-history
and export reads remain proportional to the requested history. Validate exact
counts, immediate freshness, retained records, restart recovery, latency, memory
and database growth together. Synthetic ASGI/persistence measurements do not
replace Windows browser or installed application evidence.
