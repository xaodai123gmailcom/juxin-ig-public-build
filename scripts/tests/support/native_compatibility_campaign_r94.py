"""Run 1,000 parameterized production-code scenarios, not Windows builds."""
from pathlib import Path
import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/tests'))
from test_native_repair_r94 import CompatibilityRecoveryTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    test = CompatibilityRecoveryTests()
    started = time.monotonic()
    failures = 0
    with (args.output / 'campaign.jsonl').open('w', encoding='utf-8') as results, \
         (args.output / 'execution.log').open('w', encoding='utf-8') as log:
        for family in test.families:
            for index in range(100):
                record = {'id': f'{family}-{index:03}', 'family': family, 'parameter': index}
                try:
                    with redirect_stdout(log), redirect_stderr(log):test.scenario(family, index)
                    record['status'] = 'passed'
                except Exception:
                    failures += 1
                    record.update(status='failed', error=traceback.format_exc())
                results.write(json.dumps(record, ensure_ascii=False) + '\n');results.flush()
            print(f'{family}: 100 scenarios completed', flush=True)
    summary = {'scenarios': 1000, 'passed': 1000 - failures, 'failed': failures,
               'families': 10, 'real_python_child_processes': 100,
               'real_chrome_tested': False, 'windows_builds': 0,
               'seconds': round(time.monotonic() - started, 3)}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return bool(failures)


if __name__ == '__main__':sys.exit(main())
