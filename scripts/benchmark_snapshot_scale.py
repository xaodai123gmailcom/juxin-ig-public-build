"""Opt-in synthetic full-route snapshot benchmark (no real browser/account data).

Run from the source root with CI=true:
  PYTHONPATH=backend python scripts/benchmark_snapshot_scale.py --output report.json

The default creates/removes an isolated ~2 GB database. Measurements are first
connection/cold SQLite cache and repeated/warm reads, NOT guaranteed OS-cold I/O.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import CoreService
from snapshot_scale_fixture import seed_snapshot_scale


class Inventory:
    def list_all_windows(self):
        return {'windows': [], 'connection': {'connected': True, 'state': 'ready'}}


async def measure(db, fixture, *, rounds=3, compact=False, platform=None):
    import httpx
    cache_note = 'Fresh SQLite connections; no OS cache eviction facility available'
    if hasattr(os, 'posix_fadvise') and hasattr(os, 'POSIX_FADV_DONTNEED'):
        # Flush and hint eviction ONLY for this synthetic file; never global drop_caches.
        try:
            with db.path.open('rb') as handle:
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            cache_note = 'First read after per-file POSIX_FADV_DONTNEED hint; warm repeats; OS eviction is best-effort'
        except OSError:
            cache_note = 'Per-file eviction hint unavailable; fresh SQLite connections only'
    settings = Settings(startup_token='synthetic-snapshot-scale-startup-token',
                        database_path=db.path, data_dir=db.path.parent)
    app = create_app(settings, database=db, bitbrowser=Inventory())
    service = app.state.service
    login = service.login('snapshot-benchmark', 'synthetic-benchmark-password')
    headers = {'X-Startup-Token': settings.startup_token,
               'Authorization': 'Bearer ' + login['token']}
    timings, writer_seconds, writer_errors, health_seconds = [], [], [], []
    expected = dict(fixture)
    stop = threading.Event()
    def writer():
        count = 0
        while not stop.is_set():
            try:
                begin = time.perf_counter()
                with db.write() as c:
                    # Same durable checkpoint/task write pattern as an active
                    # collector; no network, real profile or external action.
                    c.execute('UPDATE tasks SET version=version+1 WHERE id=?', (fixture['task_id'],))
                    c.execute('UPDATE task_checkpoints SET updated_at=? WHERE target_id=?',
                              (f'synthetic-write-{count}', fixture['target_id']))
                    c.execute('UPDATE workbench_state_revision SET revision=revision+1 WHERE singleton_id=1')
                writer_seconds.append(time.perf_counter()-begin)
                count += 1
            except BaseException as error:
                writer_errors.append(repr(error)); stop.set()
            stop.wait(.05)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://synthetic', headers=headers) as client:
        async def snapshot(label):
            begin = time.perf_counter()
            cpu_begin = time.process_time()
            response = await client.get('/api/workbench/snapshot?limit=2000&history_limit=2000'
                + ('&platform=' + platform if platform else '') + ('&compact=1' if compact else ''))
            elapsed = time.perf_counter()-begin
            response.raise_for_status()
            payload = response.json()
            for key in ('total_collected','total_public','total_private','total_split'):
                assert payload['counts'][key] == expected[key], (key, payload['counts'][key])
            assert payload['global_dedupe_count'] == expected['global_dedupe_count']
            target = next(t for task in payload['tasks'] for t in task['targets']
                          if t['id'] == fixture['target_id'])
            progress = target['mode_progress']['followers']
            assert progress['qualified_for_review'] == expected['qualified_for_review']
            assert progress['discarded'] == expected['discarded']
            timings.append(dict(label=label, seconds=round(elapsed, 4),
                                cpu_seconds=round(time.process_time()-cpu_begin, 4), bytes=len(response.content),
                                revision=payload['revision']))
            return payload
        await snapshot('first_connection_read')
        for index in range(rounds):
            await snapshot(f'warm_{index+1}')
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        heartbeat_stop = asyncio.Event()
        async def health():
            while not heartbeat_stop.is_set():
                begin = time.perf_counter()
                response = await client.get('/health')
                response.raise_for_status()
                health_seconds.append(time.perf_counter()-begin)
                await asyncio.sleep(.02)
        pulse = asyncio.create_task(health())
        try:
            await asyncio.gather(*(snapshot(f'concurrent_writer_{i+1}') for i in range(2)))
        finally:
            heartbeat_stop.set(); stop.set()
            await pulse
            await asyncio.to_thread(thread.join, 15)
        assert not thread.is_alive() and not writer_errors, writer_errors
        # Prove newly committed business data is not hidden by a stale aggregate
        # cache. Use the real identity/result/review service paths after polling.
        def add_reviewed_result():
            owner, target, task = fixture['owner_id'], fixture['target_id'], fixture['task_id']
            username = 'synthetic.latest_result'
            claim = service.claim_workbench_identity(owner, username=username,
                source='followers', source_target=target)
            profile = {'username': username, 'visibility': 'public', 'bio': 'newest committed row'}
            service.record_result(owner, task, target, username=username,
                instagram_user_id=None, source_mode='followers', visibility='public',
                profile=profile, screening={}, qualified=True, dedupe_claim_id=claim['claim_id'])
            candidate = service.create_workbench_candidate(owner, claim_id=claim['claim_id'],
                username=username, visibility='public', profile=profile, screening={},
                review_cache={}, source_mode='followers', source_target=target)
            service.decide_workbench_candidate(owner, candidate_id=candidate['id'], decision='approved')
            return candidate['id']
        newest_candidate = await asyncio.to_thread(add_reviewed_result)
        for key in ('total_collected', 'total_public', 'global_dedupe_count', 'qualified_for_review'):
            expected[key] += 1
        # A newer durable command/revision must be reflected by the next read.
        await asyncio.to_thread(service.set_task_runtime_status, fixture['owner_id'], fixture['task_id'], 'paused')
        required = await asyncio.to_thread(service.advance_workbench_revision)
        newest = await snapshot('after_pause_command')
        assert newest['revision'] >= required
        assert any(row['id'] == newest_candidate for row in newest['approved']['public'])
        assert next(t for t in newest['tasks'] if t['id'] == fixture['task_id'])['status'] == 'paused'
    await app.state.snapshot_inventory.close()
    def summary(values):
        return dict(count=len(values), median_seconds=round(statistics.median(values), 5),
                    maximum_seconds=round(max(values), 5)) if values else dict(count=0)
    try:
        import resource
        peak_rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
    except ImportError:
        peak_rss_bytes = None
    return dict(fixture=fixture, database_bytes=db.path.stat().st_size,
                compact=compact, platform=platform, peak_rss_bytes=peak_rss_bytes,
                request_count=len(timings),
                cache_note=cache_note,
                snapshots=timings, concurrent_writer=summary(writer_seconds),
                concurrent_health=summary(health_seconds), exact_counts_verified=True,
                latest_pause_and_revision_verified=True, newest_result_and_review_verified=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--results', type=int, default=441_552)
    parser.add_argument('--identities', type=int, default=602_831)
    parser.add_argument('--profile-bytes', type=int, default=1_000)
    parser.add_argument('--compact', action='store_true', help='Use modern deduplicated desktop wire format')
    parser.add_argument('--platform', choices=['instagram'], help='Exercise the actual desktop IG platform scope')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='snapshot-scale-') as temporary:
        db = Database(Path(temporary) / 'synthetic.sqlite3'); db.initialize()
        service = CoreService(db)
        owner = service.register_user('snapshot-benchmark', 'synthetic-benchmark-password')['id']
        fixture = seed_snapshot_scale(db.path, owner, result_count=args.results,
            identity_count=args.identities, profile_bytes=args.profile_bytes)
        report = asyncio.run(measure(db, fixture, compact=args.compact, platform=args.platform))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
