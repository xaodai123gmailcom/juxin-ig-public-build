"""The shipped browser dependency, separate from per-account engine records."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .browser_bundle import version_tuple
from .errors import UpstreamUnavailableError

POLICY_NAME = 'juxin-runtime-requirement.json'


def validate_requirement(record):
    if (not isinstance(record, dict) or type(record.get('format')) is not int
            or record['format'] != 1
            or record.get('mode') not in {'bundled-available', 'installed-chrome-required'}):
        raise ValueError('Invalid browser dependency record')
    if record['mode'] == 'installed-chrome-required':
        version_tuple(record.get('minimum_version'))
    return record


def read_requirement():
    filename = os.environ.get('IGAC_RUNTIME_REQUIREMENT')
    if not filename:
        return None  # Source development; packaged desktop always supplies it.
    try:
        path = Path(filename)
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError('Missing or invalid browser dependency file')
        return validate_requirement(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError) as error:
        raise UpstreamUnavailableError('安装包的浏览器依赖记录缺失或损坏，请重新安装完整软件',
            details={'reason': 'native_runtime_requirement_invalid'}) from error


def enforce_chrome_version(requirement, version):
    if requirement and requirement['mode'] == 'installed-chrome-required':
        if version_tuple(version) < version_tuple(requirement['minimum_version']):
            raise UpstreamUnavailableError(
                '此兼容版需要 Google Chrome ' + requirement['minimum_version'] + ' 或更新版本，请更新 Chrome；账号数据已保留',
                details={'reason': 'native_runtime_chrome_too_old',
                         'minimum_version': requirement['minimum_version'], 'actual_version': version})
