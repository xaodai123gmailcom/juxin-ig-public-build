"""Pre-aggregate progress SQL oracle, retained only for exactness regression tests."""
import sqlite3
import json
from typing import Any, Iterable
_loads = json.loads

def legacy_mode_progress_for_targets(
    connection: sqlite3.Connection,
    target_ids: Iterable[str],
) -> dict[str, dict[str, dict[str, int | None]]]:
    """Project durable, per-mode collection progress for task APIs.

    The trigger-maintained candidate counter is the authoritative live count
    for discovery and processing.  Checkpoint counters provide the most recent
    source-header total and preserve progress after terminal technical spool
    compaction.  This intentionally never consults task_results, whose global
    dedupe and multi-source merge semantics make it unsuitable for mode
    progress.
    """

    normalized_ids = list(dict.fromkeys(str(item) for item in target_ids if item))
    if not normalized_ids:
        return {}
    result: dict[str, dict[str, dict[str, int | None]]] = {}
    # Keep each statement below SQLite's conservative host-parameter limit.
    for offset in range(0, len(normalized_ids), 400):
        batch = normalized_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        candidate_rows = connection.execute(
            f"""
            SELECT target_id, mode,
                   total AS discovered,
                   recorded + deduped AS processed,
                   recorded AS saved,
                   deduped AS skipped_global_duplicates
            FROM task_mode_candidate_counters
            WHERE target_id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        live_modes = {(row["target_id"], row["mode"]) for row in candidate_rows}
        for row in candidate_rows:
            result.setdefault(row["target_id"], {})[row["mode"]] = {
                "source_total": None,
                "discovered": int(row["discovered"] or 0),
                "processed": int(row["processed"] or 0),
                "saved": int(row["saved"] or 0),
                "skipped_global_duplicates": int(row["skipped_global_duplicates"] or 0),
                "qualified_for_review": 0,
            }

        checkpoint_rows = connection.execute(
            f"""
            SELECT target_id, mode, counters_json
            FROM task_checkpoints
            WHERE target_id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for row in checkpoint_rows:
            try:
                counters = _loads(row["counters_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                counters = {}
            if not isinstance(counters, dict):
                counters = {}
            progress = result.setdefault(row["target_id"], {}).setdefault(
                row["mode"],
                {
                    "source_total": None,
                    "discovered": None,
                    "processed": None,
                    "saved": None,
                    "skipped_global_duplicates": None,
                    "qualified_for_review": 0,
                },
            )

            layers: list[dict[str, Any]] = []
            layer = counters
            for _ in range(5):
                if not isinstance(layer, dict):
                    break
                layers.append(layer)
                previous = layer.get("previous_counters")
                if not isinstance(previous, dict):
                    break
                layer = previous

            def layer_int(layer_value: dict[str, Any], key: str) -> int | None:
                value = layer_value.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return None
                return value

            discovery_restored = False
            processing_restored = False
            for layer_value in layers:
                source_total = layer_int(layer_value, "source_total")
                if source_total is not None and progress["source_total"] is None:
                    progress["source_total"] = source_total
                # A recovery checkpoint can predate a spool repair.  It may
                # supply the header count, but must never hide live pending
                # work by inflating discovery/processing from an old run.
                if (row["target_id"], row["mode"]) in live_modes:
                    continue
                discovered = layer_int(layer_value, "discovered")
                if discovered is None:
                    # Legacy spool checkpoints called this value visible_accounts.
                    discovered = layer_int(layer_value, "visible_accounts")
                if discovered is not None and not discovery_restored:
                    progress["discovered"] = discovered
                    discovery_restored = True
                if processing_restored:
                    continue
                saved = layer_int(layer_value, "saved")
                processed = layer_int(layer_value, "processed")
                deduped = layer_int(layer_value, "skipped_global_duplicates")
                if saved is None and processed is None and deduped is None:
                    continue
                # Recover one snapshot, never independently take maxima from
                # different recovery generations.  A corrected latest count
                # (including zero) must not be overwritten by older counters.
                # Exactly two valid values can determine the third. Legacy
                # checkpoints with only a processed count cannot prove dedupe.
                if processed is None and saved is not None and deduped is not None:
                    processed = saved + deduped
                elif saved is None and processed is not None and deduped is not None:
                    if processed >= deduped:
                        saved = processed - deduped
                    else:
                        deduped = None
                elif deduped is None and processed is not None and saved is not None:
                    if processed >= saved:
                        deduped = processed - saved
                if (
                    processed is not None and saved is not None and deduped is not None
                    and processed != saved + deduped
                ):
                    # Imported/corrupt counters are not evidence for a
                    # confident dedupe count. Keep the unknown visible.
                    deduped = None
                if processed is not None:
                    progress["processed"] = processed
                if saved is not None:
                    progress["saved"] = saved
                progress["skipped_global_duplicates"] = deduped
                processing_restored = True

        # Saved results include directly excluded accounts.  Start from only
        # the review entries linked to these source targets, and verify the
        # immutable original result and mode; status remains irrelevant so
        # an eventual approve/reject does not erase the admission count.
        review_rows = connection.execute(
            f"""
            SELECT review.source_target AS target_id,
                   review.source_mode AS mode, COUNT(*) AS qualified
            FROM workbench_candidates AS review
                 INDEXED BY idx_workbench_candidates_progress
            JOIN task_results recorded INDEXED BY idx_results_progress_provenance
              ON recorded.target_id=review.source_target
             AND recorded.account_id=review.account_id
            JOIN tasks task ON task.id=recorded.task_id
              AND task.owner_user_id=review.owner_user_id
            JOIN workbench_identity_claims claim
              ON claim.account_id=review.account_id
             AND claim.claimed_by_user_id=review.owner_user_id
             AND claim.source IN (review.source_mode, 'collection')
             AND (claim.source_target=review.source_target
                  OR claim.source_target IS NULL)
            WHERE review.source_target IN ({placeholders})
              AND review.source_mode IN ('followers', 'following', 'post_likers')
              AND EXISTS (
                SELECT 1
                FROM json_each(CASE WHEN json_valid(recorded.sources_json)
                  THEN CASE WHEN json_type(recorded.sources_json)='array'
                       THEN recorded.sources_json ELSE '[]' END
                  ELSE '[]' END) source
                WHERE source.value=review.source_mode
              )
            GROUP BY review.source_target, review.source_mode
            """,
            batch,
        ).fetchall()
        review_evidence = {
            (row["target_id"], row["mode"]): int(row["qualified"] or 0)
            for row in review_rows
        }
        # Earlier clients could create a candidate without source metadata.
        # Recover only the uniquely bound claim's exact target/mode when its
        # original result confirms that mode.  A claim without a target or
        # concrete source is ambiguous and is never assigned by guesswork.
        # Keep the sparse legacy review index outside every other join.
        # A later ordinary join can move ahead of a CROSS JOIN pair and
        # scan all target results even when no old review rows remain.
        legacy_review_rows = connection.execute(
            f"""
            SELECT claim.source_target AS target_id,
                   claim.source AS mode, COUNT(*) AS qualified
            FROM workbench_candidates AS review
                 INDEXED BY idx_workbench_candidates_legacy_progress
            CROSS JOIN workbench_identity_claims AS claim ON claim.account_id=review.account_id
              AND claim.claimed_by_user_id=review.owner_user_id
            CROSS JOIN task_results recorded INDEXED BY idx_results_progress_provenance
              ON recorded.account_id=claim.account_id
              AND recorded.target_id=claim.source_target
            CROSS JOIN tasks task ON task.id=recorded.task_id
              AND task.owner_user_id=claim.claimed_by_user_id
            WHERE claim.source_target IN ({placeholders})
              AND claim.source IN ('followers', 'following', 'post_likers')
              AND (review.source_target IS NULL OR review.source_mode IS NULL)
              AND (review.source_target IS NULL
                   OR review.source_target=claim.source_target)
              AND (review.source_mode IS NULL OR review.source_mode=claim.source)
              AND EXISTS (
                SELECT 1
                FROM json_each(CASE WHEN json_valid(recorded.sources_json)
                  THEN CASE WHEN json_type(recorded.sources_json)='array'
                       THEN recorded.sources_json ELSE '[]' END
                  ELSE '[]' END) source
                WHERE source.value=claim.source
              )
            GROUP BY claim.source_target, claim.source
            """,
            batch,
        ).fetchall()
        for row in legacy_review_rows:
            key = (row["target_id"], row["mode"])
            review_evidence[key] = review_evidence.get(key, 0) + int(row["qualified"] or 0)
        for (target_id, mode) in review_evidence:
            result.setdefault(target_id, {}).setdefault(
                mode,
                {
                    "source_total": None,
                    "discovered": None,
                    "processed": None,
                    "saved": None,
                    "skipped_global_duplicates": None,
                    "qualified_for_review": 0,
                },
            )
        old_unattributed_modes: dict[tuple[str, str], int | None] = {}
        for target_id in batch:
            for mode, progress in result.get(target_id, {}).items():
                qualified = review_evidence.get((target_id, mode), 0)
                saved = progress.get("saved")
                # Legacy checkpoint totals alone cannot attribute old saved
                # accounts to a review queue.  A live source counter proves
                # that an empty review lookup means zero actual admissions.
                old_unattributed = (
                    qualified == 0
                    and (target_id, mode) not in live_modes
                    and (
                        (saved is not None and saved > 0)
                        or (saved is None and (progress.get("processed") or 0) > 0)
                    )
                )
                if old_unattributed:
                    old_unattributed_modes[(target_id, mode)] = saved
                progress["qualified_for_review"] = (
                    None if old_unattributed else qualified
                )
        if old_unattributed_modes:
            # Only this legacy, counter-only case needs to examine stored
            # results.  If every old saved result is demonstrably a direct
            # collection exclusion, zero admissions is known even after
            # the technical spool was compacted.  Never guess from the
            # checkpoint when results or their provenance are missing.
            cold_targets = list(dict.fromkeys(
                target_id for target_id, _ in old_unattributed_modes
            ))
            cold_marks = ",".join("?" for _ in cold_targets)
            exclusion_rows = connection.execute(
                f"""
                SELECT recorded.target_id, source.value AS mode,
                       COUNT(*) AS recorded_total,
                       COUNT(excluded.id) AS excluded_total
                FROM task_results recorded INDEXED BY idx_results_progress_provenance
                JOIN tasks task ON task.id=recorded.task_id
                JOIN json_each(CASE WHEN json_valid(recorded.sources_json)
                  THEN CASE WHEN json_type(recorded.sources_json)='array'
                       THEN recorded.sources_json ELSE '[]' END
                  ELSE '[]' END) source
                LEFT JOIN workbench_collection_exclusions excluded
                  ON excluded.account_id=recorded.account_id
                 AND excluded.owner_user_id=task.owner_user_id
                WHERE recorded.target_id IN ({cold_marks})
                GROUP BY recorded.target_id, source.value
                """,
                cold_targets,
            ).fetchall()
            excluded_evidence = {
                (row["target_id"], row["mode"]): row
                for row in exclusion_rows
            }
            for (target_id, mode), saved in old_unattributed_modes.items():
                row = excluded_evidence.get((target_id, mode))
                if (
                    row is not None
                    and int(row["recorded_total"]) == int(row["excluded_total"])
                    and (saved is None or int(row["recorded_total"]) >= saved)
                ):
                    result[target_id][mode]["qualified_for_review"] = 0
        # Exclusions have their own durable identity/source attribution.
        # Hover rejection is part of saved records, not a previously-seen
        # duplicate. Keep those counters separate instead of subtracting
        # manual-review admissions from an unrelated displayed total.
        exclusion_rows = connection.execute(
            f"""SELECT claim.source_target AS target_id, claim.source AS mode,
                COUNT(*) AS discarded,
                SUM(CASE WHEN json_valid(excluded.profile_snapshot_json)
                    THEN json_extract(excluded.profile_snapshot_json,'$.page_read_status')='hover_preview'
                    ELSE 0 END) AS hover_discarded
                FROM workbench_identity_claims claim INDEXED BY idx_workbench_claims_progress
                JOIN workbench_collection_exclusions excluded INDEXED BY idx_workbench_exclusions_progress
                  ON excluded.account_id=claim.account_id
                 AND excluded.owner_user_id=claim.claimed_by_user_id
                WHERE claim.source_target IN ({placeholders})
                GROUP BY claim.source_target,claim.source""",batch,
        ).fetchall()
        exclusions={(row['target_id'],row['mode']):row for row in exclusion_rows}
        for target_id in batch:
            for mode, progress in result.get(target_id,{}).items():
                row=exclusions.get((target_id,mode))
                progress['discarded']=int(row['discarded'] or 0) if row else 0
                progress['hover_discarded']=int(row['hover_discarded'] or 0) if row else 0
    return result

