"""10,000 parameterized production-code build fault checks, not Windows builds.

HTTP and browser adapters are controlled; cache/publication/rollback use real
temporary files. Native ownership failures use real Python child processes.
The complete source, compiler and packaging-omission gates run separately.
"""
from pathlib import Path
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/tests'))
from browser_download_campaign_r94 import FAMILIES, scenario as download_scenario
from test_native_repair_r94 import CompatibilityRecoveryTests, BuildResilienceTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    compatibility = CompatibilityRecoveryTests()
    resilience = BuildResilienceTests()
    groups = [('download', name, 400, download_scenario) for name in FAMILIES]
    groups += [('native', name, 400, compatibility.scenario) for name in compatibility.families]
    groups += [('resilience', name, 500, resilience.scenario) for name in resilience.families]
    assert sum(count for _, _, count, _ in groups) == 10000
    started = time.monotonic();completed = failures = 0;families = []
    with (args.output / 'cases.jsonl').open('w', encoding='utf-8') as records, \
         (args.output / 'execution.log').open('w', encoding='utf-8') as log:
        for group, family, count, run in groups:
            group_failures = 0
            for index in range(count):
                record = {'id': f'{group}/{family}/{index:04}', 'family': f'{group}/{family}', 'parameter': index}
                try:
                    with redirect_stdout(log), redirect_stderr(log):run(family, index)
                    record['status'] = 'passed'
                except Exception:
                    failures += 1;group_failures += 1
                    record.update(status='failed', error=traceback.format_exc())
                records.write(json.dumps(record, ensure_ascii=False) + '\n')
                completed += 1
                if completed % 100 == 0:
                    records.flush();log.flush()
                    print(f'{completed}/10000; failures={failures}; {group}/{family}', flush=True)
            families.append({'family': f'{group}/{family}', 'cases': count, 'passed': count - group_failures, 'failed': group_failures})
    summary = {'scenarios': completed, 'passed': completed - failures, 'failed': failures,
               'families': families, 'real_python_child_processes': 400,
               'live_cdn_downloads': 0, 'real_chrome_tested': False, 'windows_builds': 0,
               'seconds': round(time.monotonic() - started, 3),
               'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                   ('scripts/browser_download.py', 'scripts/install_native_browser.py', 'scripts/repair_native_browser.py',
                    'scripts/verify_native_browser.py', 'backend/app/browser_bundle.py', 'backend/app/native_browser.py')}}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key not in {'families', 'source_sha256'}}), flush=True)
    return bool(failures)


if __name__ == '__main__':raise SystemExit(main())
