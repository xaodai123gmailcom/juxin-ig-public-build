"""Select a browser engine while keeping each app profile isolated and versioned."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .browser_bundle import bundle_descriptor
from .errors import UpstreamUnavailableError
from .browser_permissions import BrowserPermissionError, ensure_browser_sandbox_access
from .browser_requirement import read_requirement, enforce_chrome_version


class BrowserLaunchLog:
    """Drain stderr continuously; retain only the last 64 KiB per profile.

    Do not put browser output in API errors: it may contain visited URLs. The
    local report records paths and exit state, never the parent's environment.
    """
    limit = 64 * 1024

    def __init__(self, directory, executable, args, headless):
        self.report = directory / 'juxin-startup.json'
        self.output = directory / 'juxin-startup.stderr.log'
        self.data = {'status': 'starting', 'executable': str(executable),
                     'directory': str(directory), 'headless': headless, 'args': args,
                     'stderr_file': str(self.output)}
        self.tail = b''
        self.thread = None
        self.output.write_bytes(b'')
        self._write()

    def _write(self):
        self.report.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def attach(self, process):
        self.data['pid'] = process.pid
        self._write()
        self.thread = threading.Thread(target=self._drain, args=(process.stderr,), daemon=True)
        self.thread.start()

    def _drain(self, stream):
        try:
            while chunk := stream.read(4096):
                self.tail = (self.tail + chunk)[-self.limit:]
                try:
                    self.output.write_bytes(self.tail)
                except OSError:
                    pass  # Always drain; a logging error must never block Chrome.
        except (OSError, ValueError):
            pass  # The process may close its stream while the application exits.
        finally:
            stream.close()

    def update(self, status, **details):
        self.data.update(status=status, **details)
        try:
            self._write()
        except OSError:
            pass

    def finish(self, returncode):
        if self.thread:
            self.thread.join(timeout=1)
        self.update('exited' if self.data['status'] == 'ready' else self.data['status'],
                    exit_code=returncode)


def pinned_chromium():
    import playwright

    registry = Path(playwright.__file__).parent / 'driver' / 'package' / 'browsers.json'
    try:
        entry = next(row for row in json.loads(registry.read_text(encoding='utf-8'))['browsers']
                     if row['name'] == 'chromium')
        revision, version = str(entry['revision']), entry['browserVersion']
        if not revision.isdigit() or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
            raise ValueError('Invalid Chromium metadata')
        return revision, version
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as error:
        raise UpstreamUnavailableError('配套浏览器版本信息缺失，请重新构建完整安装包',
                                       details={'reason': 'native_runtime_metadata'}) from error


def bundled_chromium(root, driver=None):
    revision, version = pinned_chromium() if driver is None else driver
    try:
        record = bundle_descriptor(Path(root) / ('chromium-' + revision), revision, version)
        return revision, record['version']
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise UpstreamUnavailableError('内置浏览器版本记录无效，请重新构建完整安装包',
            details={'reason': 'native_runtime_bundle_invalid'}) from error


def bundled_executable(root, *, windows=None):
    windows = os.name == 'nt' if windows is None else windows
    revision, version = bundled_chromium(root)
    base = Path(root).resolve() / ('chromium-' + revision)
    candidates = ('chrome-win64/chrome.exe', 'chrome-win/chrome.exe') if windows else (
        'chrome-linux64/chrome', 'chrome-linux/chrome')
    for relative in candidates:
        executable = base / relative
        if executable.is_file():
            if windows:
                # A leftover chrome.exe alone is not a usable installation.
                required = ('chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak')
                missing = [name for name in required if not (executable.parent / name).is_file()
                           or (executable.parent / name).stat().st_size == 0]
                if missing:
                    raise UpstreamUnavailableError('内置 Chromium 运行文件不完整，请重新下载配套浏览器',
                        details={'reason': 'native_runtime_incomplete', 'version': version, 'missing': missing})
            return executable
    raise UpstreamUnavailableError(f'未找到配套 Chromium {version}，请重新下载本版本的浏览器运行文件',
        details={'reason': 'native_runtime_missing', 'revision': revision, 'version': version})


def installed_chrome():
    """Resolve only standard Chrome installations, never PATH or a user profile."""
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
            if candidate.is_file():
                return candidate.resolve()
    return None


def windows_file_version(executable):
    """Read the executable's version resource without launching it or a shell."""
    import ctypes
    from ctypes import wintypes

    class FixedInfo(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            'signature', 'structure_version', 'file_ms', 'file_ls',
            'product_ms', 'product_ls', 'flags_mask', 'flags', 'os', 'type',
            'subtype', 'date_ms', 'date_ls')]

    library = ctypes.WinDLL('version', use_last_error=True)
    size_fn = library.GetFileVersionInfoSizeW
    size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size_fn.restype = wintypes.DWORD
    read_fn = library.GetFileVersionInfoW
    read_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    read_fn.restype = wintypes.BOOL
    query_fn = library.VerQueryValueW
    query_fn.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.UINT)]
    query_fn.restype = wintypes.BOOL
    ignored = wintypes.DWORD()
    size = size_fn(str(executable), ctypes.byref(ignored))
    if not size or size > 16 * 1024 * 1024:
        raise OSError('Browser version resource is missing or invalid')
    buffer = ctypes.create_string_buffer(size)
    pointer = ctypes.c_void_p();length = wintypes.UINT()
    if (not read_fn(str(executable), 0, size, buffer)
            or not query_fn(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length))
            or not pointer.value or length.value < ctypes.sizeof(FixedInfo)):
        raise OSError('Cannot read browser version resource')
    info = ctypes.cast(pointer, ctypes.POINTER(FixedInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise OSError('Invalid browser version signature')
    version = '.'.join(str(value) for value in (
        info.file_ms >> 16, info.file_ms & 0xFFFF,
        info.file_ls >> 16, info.file_ls & 0xFFFF))
    if info.file_ms == 0:
        raise OSError('Invalid browser version')
    return version


def _bundled_or_development_executable():
    root = os.environ.get('IGAC_BROWSER_DIR')
    if root:
        executable = bundled_executable(root)
    else:
        # Development install only; packaged builds always set IGAC_BROWSER_DIR.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            executable = Path(driver.chromium.executable_path).resolve()
        if not executable.is_file():
            raise UpstreamUnavailableError('内置 Chromium 运行文件缺失，请使用包含浏览器运行时的完整安装包',
                                           details={'reason': 'native_runtime_missing'})
    try:
        ensure_browser_sandbox_access(executable)
    except BrowserPermissionError as error:
        raise UpstreamUnavailableError('内置浏览器目录无法完成沙箱读取权限配置，请运行浏览器修复工具',
            details={'reason': 'native_runtime_permissions', 'error': str(error)}) from error
    return executable


def _version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', value):
        raise ValueError('Invalid browser version')
    return tuple(int(part) for part in value.split('.'))


def select_browser_runtime(directory=None, *, windows=None):
    """Windows prefers installed Chrome, with bundled Chromium on other hosts.

    Each profile remembers its engine. Removing Chrome must not silently open
    its newer profile in an older bundled engine. Only the executable is used;
    the native manager always supplies the app-owned user-data-dir and port.
    """
    windows = os.name == 'nt' if windows is None else windows
    marker = Path(directory) / 'juxin-runtime.json' if directory is not None else None
    requirement = read_requirement()
    previous = None
    if marker is not None and marker.exists():
        try:
            with marker.open(encoding='utf-8') as stream:
                previous = json.loads(stream.read(8192))
            if previous['source'] not in {'installed-chrome', 'bundled', 'override'}:
                raise ValueError('Invalid browser source')
            _version_tuple(previous['version'])
            if not isinstance(previous['executable'], str) or not Path(previous['executable']).is_absolute():
                raise ValueError('Invalid browser executable')
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise UpstreamUnavailableError('窗口的浏览器版本记录无法读取，已保留原账号目录',
                details={'reason': 'native_runtime_record_invalid'}) from error

    override = os.environ.get('IGAC_NATIVE_BROWSER_EXECUTABLE')
    if requirement and requirement['mode'] == 'installed-chrome-required':
        chrome = installed_chrome() if windows else None
        if chrome is None:
            raise UpstreamUnavailableError('此兼容版需要已安装 Google Chrome，请安装 Chrome 后重试；账号数据已保留',
                details={'reason': 'native_runtime_chrome_required'})
        if override and Path(override).resolve() != chrome:
            raise UpstreamUnavailableError('此兼容版只允许使用已安装的 Google Chrome',
                details={'reason': 'native_runtime_chrome_override_rejected'})
        executable, source = chrome, 'installed-chrome'
    elif override:
        executable, source = Path(override).resolve(), 'override'
    elif previous and previous['source'] != 'bundled':
        executable, source = Path(previous['executable']), previous['source']
    elif previous:
        executable, source = _bundled_or_development_executable(), 'bundled'
    else:
        chrome = installed_chrome() if windows else None
        executable, source = (chrome, 'installed-chrome') if chrome else (
            _bundled_or_development_executable(), 'bundled')
    if not executable.is_file():
        reason = 'native_runtime_override_missing' if override else 'native_runtime_bound_missing'
        raise UpstreamUnavailableError('该窗口使用的浏览器已不存在，请恢复该浏览器；账号目录已保留',
                                       details={'reason': reason, 'executable': str(executable)})

    try:
        version = windows_file_version(executable) if windows else (
            pinned_chromium()[1] if source == 'bundled' else None)
        if windows and source == 'bundled' and os.environ.get('IGAC_BROWSER_DIR'):
            expected = bundled_chromium(os.environ['IGAC_BROWSER_DIR'])[1]
            if version != expected:
                raise UpstreamUnavailableError('内置浏览器实际版本与记录不一致，请重新构建或修复浏览器',
                    details={'reason': 'native_runtime_bundle_version_mismatch',
                             'expected_version': expected, 'actual_version': version})
        if version is not None:
            enforce_chrome_version(requirement, version)
            selected_version = _version_tuple(version)
            versions = [previous['version']] if previous else []
            if marker is not None:
                last_version_file = marker.parent / 'Last Version'
                if last_version_file.exists():
                    with last_version_file.open(encoding='utf-8') as stream:
                        versions.append(stream.read(128).strip())
            for saved in versions:
                if selected_version < _version_tuple(saved):
                    raise UpstreamUnavailableError(
                        f'该窗口已使用浏览器 {saved}，当前为 {version}；请更新浏览器后重试，账号目录已保留',
                        details={'reason': 'native_runtime_downgrade', 'saved_version': saved,
                                 'selected_version': version})
    except (OSError, ValueError) as error:
        raise UpstreamUnavailableError('无法确认浏览器或账号目录的版本，已停止打开并保留原数据',
                                       details={'reason': 'native_runtime_version_unavailable'}) from error

    choice = {'source': source, 'executable': str(executable), 'version': version}
    if marker is not None and version is not None:
        # NativeBrowser holds the per-profile lock. Persist before launch so a
        # crash cannot make a later run forget the engine that touched the data.
        temporary = marker.with_suffix('.json.tmp')
        try:
            temporary.write_text(json.dumps(choice, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(marker)
        except OSError as error:
            raise UpstreamUnavailableError('无法保存窗口的浏览器版本记录，尚未启动浏览器',
                                           details={'reason': 'native_runtime_record_write_failed'}) from error
    return choice


def browser_executable(directory=None):
    return Path(select_browser_runtime(directory)['executable'])
