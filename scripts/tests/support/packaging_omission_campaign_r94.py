"""100 distinct missing-file cases against the real source release gate."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import tempfile
import time
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with zipfile.ZipFile(args.archive) as archive, tempfile.TemporaryDirectory(prefix='聚鑫 F8 包核对 (100) ') as folder:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'Duplicate archive entries'
        assert archive.testzip() is None, 'Archive CRC failure'
        manifest = json.loads(archive.read('SOURCE_SHA256.json'))
        assert set(names) == set(manifest) | {'SOURCE_SHA256.json'}, 'Missing or unlisted archive files'
        for name, expected in manifest.items():
            assert not Path(name).is_absolute() and '..' not in Path(name).parts
            assert not {'node_modules', '__pycache__', 'installer-output'} & set(Path(name).parts)
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected, name
        root = Path(folder) / '完整包';archive.extractall(root)
        def gate():
            return subprocess.run(['node', 'scripts/verify_build_source.mjs'], cwd=root,
                                  capture_output=True, timeout=20)
        baseline = gate();assert baseline.returncode == 0, baseline.stderr
        selected = ['START_HERE_NEWGEN.bat', 'BUILD_REVISION.txt', 'package.json', 'package-lock.json',
                    'requirements.txt', 'backend/requirements.txt', 'build_installer_windows.bat',
                    'REPAIR_NATIVE_BROWSER.bat', 'backend/app/browser_bundle.py',
                    'backend/app/browser_runtime.py', 'backend/app/native_browser.py',
                    'backend/app/main.py', 'backend/app/service.py', 'backend/app/playwright_worker.py',
                    'scripts/build_windows.ps1', 'scripts/install_native_browser.py',
                    'scripts/repair_native_browser.py', 'scripts/verify_native_browser.py',
                    'scripts/prune_browser_runtime.py', 'scripts/verify_frozen_core_service.py',
                    'scripts/browser_download.py', 'scripts/tests/test_browser_download_r94.py']
        for prefix, maximum in [('backend/app/', 30), ('desktop/src/', 20), ('renderer/src/', 20), ('scripts/', 8)]:
            candidates = [name for name in sorted(manifest) if name.startswith(prefix)
                          and name not in selected and '/tests/' not in name
                          and name != 'scripts/verify_build_source.mjs']
            selected.extend(candidates[:maximum])
        assert len(selected) == 100 and len(set(selected)) == 100
        records = []
        for index, name in enumerate(selected):
            path = root / name;held = Path(folder) / 'removed-entry';path.rename(held)
            try:
                result = gate()
                output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
                passed = result.returncode != 0 and (name in output or Path(name).name in output)
                records.append({'id': index + 1, 'removed_file': name, 'detected': passed,
                                'gate_exit_code': result.returncode, 'message': output[:1500]})
            finally:
                held.rename(path)
            if (index + 1) % 20 == 0:print(f'{index + 1}/100 omissions checked', flush=True)
        restored = gate();assert restored.returncode == 0, restored.stderr
        (args.output / 'cases.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records), encoding='utf-8')
        summary = {'cases': 100, 'passed': sum(row['detected'] for row in records),
                   'failed': sum(not row['detected'] for row in records),
                   'archive_files': len(names), 'baseline_passed': True, 'restored_passed': True,
                   'windows_installer_builds': 0, 'seconds': round(time.monotonic() - started, 3)}
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary), flush=True)
        return bool(summary['failed'])


if __name__ == '__main__':raise SystemExit(main())
