# R6 acceptance scope

This checklist describes required evidence. It is not a claim that a Windows build has passed. The final `installed-verification.json` must identify the SHA-256 of the delivered installer.

| Requested behavior | Production path | Required evidence |
|---|---|---|
| Pure IG, R5 scalability and migration preserved | service, schema migrations, bounded worker caches | existing pure-IG upgrade, snapshot-scale, index/compact-wire and normal-restart gates |
| One extra gap pass after verified list exhaustion | execution_manager | single-gap regression suite and installed collection_completion.single_gap_recheck |
| No false completion on interruption; children and persistence finish before card retirement | collection completion / discovery sessions | existing cancellation, child cleanup, card dismissal and restart gates |
| Parent collector Reels uses 8–20 seconds / 50% | parent_reels.py | current-source ownership/action regressions and offline real-Chrome Reels proof |
| Seven truthful collection counters | collection task renderer | counter semantics tests and native collection screenshot |
| One merged reports page above history | reports-workspace, report-data-overview | route alias, lazy loading, stale-response/cancellation, full CSV tests and native screenshots |
| Five aligned cards, new posting count from confirmed receipts | work_reports, reports-workspace | exact timezone/microsecond/owner tests; never count legacy/manual/unknown results as new receipts |
| Header removals and wolf refresh | formal-workbench | safe app-only refresh, single-flight/error recovery and native shell screenshots |
| Standalone nurture hidden 8–20 seconds / 70%; default5min and concurrency | standalone_nurture / studio | identity, real active-clock accounting, persistent decisions, busy-window and restart tests |
| Own profile metrics and nurture history | standalone_nurture / UI | unknown values preserved, immutable capture timing, installed standalone_nurture proof and native settings/history/narrow/error screenshots |
| Real single-image posting queue | posting_workflow / posting_executor | session/owner isolation, exact review CAS, submit-once, unknown outcome, strict lease and cleanup proofs; native posting stats/window/credential/paging screenshot |
| Global material deduplication and bounded downloading | posting_pexels / posting_schema | provider-ID and both content-hash reservations, network error/redirect limits, offline image validation |
| Secure Pexels configuration | desktop encrypted store / internal Core activation | safeStorage fail-closed, no generic bridge read/write, redaction, env removal, no database secret, no task restart |
| Installed exact EXE behavior | bootstrap verify-installed / frozen selftests | installed report, collection, nurture and posting proofs all tied to the same installer digest |

## External verification boundary

All social-action tests use synthetic accounts/pages/receipts. No real Instagram posts, likes or account changes are part of acceptance. Pexels requires the user's valid local API credential, which is not embedded, provisioned or used in these tests. The installer must clearly report unconfigured/invalid/unconfirmed service states. There is no Commons provider, no website crawler, and no paid server requirement.

## Evidence interpretation

Local source tests, Electron bundle checks, actual Windows native UI fixtures, frozen Core tests, and installed EXE checks are separate stages. Browser tests skipped in a local environment remain mandatory on Windows. Source fixture evidence must not be described as an installed pass. A failed or incomplete stage prevents declaring the final installer accepted.
