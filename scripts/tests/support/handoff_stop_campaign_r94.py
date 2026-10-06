"""Exercise 36 Stop/end-of-source orderings with the real scheduler and SQLite."""
from pathlib import Path
import argparse
import json
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from test_single_handoff_r94 import BatchCollectionR59Tests


class StopBoundaryCase(BatchCollectionR59Tests):
    async def runTest(self):
        await self._unexpected_child_cleanup_case(
            stop=self.stop_case, before_source_end=self.before_end, checkpoint_delay=self.delay)


def main():
    parser = argparse.ArgumentParser();parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args();args.output.mkdir(parents=True, exist_ok=True)
    records = [];started = time.monotonic()
    with (args.output / 'execution.log').open('w', encoding='utf-8') as log:
        for boundary in ('natural_end', 'unfinished_source', 'child_error'):
            for index in range(12):
                case = StopBoundaryCase('runTest')
                case.stop_case = boundary != 'child_error'
                case.before_end = boundary == 'unfinished_source'
                case.delay = index * .025
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.TestSuite([case]))
                records.append({'id': f'{boundary}-{index:02}', 'boundary': boundary,
                    'last_source_checkpoint_delay': case.delay,
                    'passed': result.wasSuccessful() and result.testsRun == 1 and not result.skipped})
            print(boundary + ': 12 orderings completed', flush=True)
    summary = {'cases': len(records), 'passed': sum(row['passed'] for row in records),
        'failed': sum(not row['passed'] for row in records),
        'real_sqlite': True, 'real_scheduler': True, 'live_instagram': False,
        'windows_builds': 0, 'seconds': round(time.monotonic()-started, 3)}
    (args.output/'cases.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary), flush=True)
    return bool(summary['failed'])


if __name__ == '__main__':raise SystemExit(main())
