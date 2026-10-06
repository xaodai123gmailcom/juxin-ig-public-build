"""Seal an explicit installed-Chrome build only after full native verification."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def installed_candidate():
    from app.browser_runtime import installed_chrome, windows_file_version
    if os.name != 'nt':
        raise RuntimeError('Installed Chrome builds require Windows')
    executable = installed_chrome()
    if executable is None:
        raise RuntimeError('Google Chrome is required for this build mode. Install Chrome or use START_HERE_NEWGEN.bat for the bundled edition.')
    return {'source': 'installed-chrome', 'executable': str(executable),
            'version': windows_file_version(executable)}


def requirement_from_report(report, *, installed):
    if report.get('status') != 'passed' or report.get('headless') is not False:
        raise RuntimeError('A passed visible native-browser report is required')
    for key in ('network_verified', 'isolation_verified', 'persistence_verified', 'ownership_verified'):
        if report.get(key) is not True:
            raise RuntimeError('Native browser verification is incomplete: ' + key)
    if not installed:
        return {'format': 1, 'mode': 'bundled-available'}
    choice = report.get('selected_runtime', {})
    versions = report.get('actual_browser_versions')
    if (report.get('require_installed_chrome') is not True
            or choice.get('source') != 'installed-chrome'
            or not isinstance(versions, list) or len(versions) != 2
            or any(version != choice.get('version') for version in versions)):
        raise RuntimeError('Both verified windows must use the required installed Chrome')
    version = choice.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,10}(?:\.[0-9]{1,10}){3}', version):
        raise RuntimeError('Installed Chrome version is invalid')
    return {'format': 1, 'mode': 'installed-chrome-required', 'minimum_version': version}


def save_json(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate-output', type=Path, required=True)
    options = parser.parse_args()
    candidate = installed_candidate()
    save_json(options.candidate_output, candidate)
    print('CHROME_CANDIDATE=' + json.dumps(candidate, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
