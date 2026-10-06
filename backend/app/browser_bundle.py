"""Validate the one recorded browser bundle; never select arbitrary paths."""
from __future__ import annotations

import json
from pathlib import Path
import re

BUNDLE_DESCRIPTOR = 'juxin-browser-bundle.json'
STABLE_METADATA_URL = 'https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json'


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}(?:\.[0-9]{1,10}){3}', value):
        raise ValueError('Invalid browser version')
    return tuple(int(part) for part in value.split('.'))


def official_archive_url(version):
    version_tuple(version)
    return f'https://storage.googleapis.com/chrome-for-testing-public/{version}/win64/chrome-win64.zip'


def bundle_descriptor(base, revision, driver_version):
    version_tuple(driver_version)
    path = Path(base) / BUNDLE_DESCRIPTOR
    if not path.exists() and not path.is_symlink():
        return {'source': 'playwright', 'version': driver_version}
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or not path.is_file() or info.st_size > 8192:
        raise ValueError('Invalid browser bundle descriptor file')
    record = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(record, dict) or type(record.get('format')) is not int or record['format'] != 1:
        raise ValueError('Invalid browser bundle descriptor format')
    if (record.get('source') != 'chrome-for-testing-stable'
            or record.get('driver_revision') != str(revision)
            or record.get('driver_version') != driver_version):
        raise ValueError('Browser bundle does not match the installed driver')
    version = record.get('version')
    if version_tuple(version) <= version_tuple(driver_version):
        raise ValueError('Compatibility bundle must be newer than the driver bundle')
    if record.get('download_url') != official_archive_url(version):
        raise ValueError('Browser bundle does not use the official archive')
    if not isinstance(record.get('archive_sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', record['archive_sha256']):
        raise ValueError('Browser bundle archive digest is missing or invalid')
    return record


def stable_candidate(payload, minimum_version):
    if not isinstance(payload, dict):
        raise ValueError('Invalid Stable browser metadata')
    try:
        stable = payload['channels']['Stable']
        version = stable['version']
        if stable['channel'] != 'Stable' or version_tuple(version) <= version_tuple(minimum_version):
            raise ValueError('No newer Stable browser is available')
        rows = stable['downloads']['chrome']
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('Invalid Stable browser assets')
        assets = [row for row in rows if row.get('platform') == 'win64']
        url = official_archive_url(version)
        if len(assets) != 1 or assets[0].get('url') != url:
            raise ValueError('Stable metadata must contain one official Windows x64 archive')
        return version, url
    except (KeyError, TypeError) as error:
        raise ValueError('Incomplete Stable browser metadata') from error
