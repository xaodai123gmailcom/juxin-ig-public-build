"""Resumable build-only downloads, separate from packaged browser resources.

An active transfer has no wall-clock deadline. Socket timeouts and bounded
retries stop a stalled connection; partial bytes survive retries and restarts.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile

MAX_BYTES = 600 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
SOCKET_TIMEOUT = 30
MAX_ATTEMPTS = 5
CACHE_ENTRIES = 3


class DownloadError(RuntimeError):
    pass


class RetryDownload(Exception):
    pass


def checked(path, directory=False):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                or (not path.is_dir() if directory else not path.is_file())):
            raise DownloadError(f'Download cache entry must be a regular {"directory" if directory else "file"}: {path}')
    return path


@contextmanager
def cache_lock(root):
    checked(root, directory=True)
    root.mkdir(parents=True, exist_ok=True)
    lock = checked(root / 'download.lock')
    # Advisory OS locks are released even if the build process is killed.
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise DownloadError('Another browser download is using this build cache; let it finish first') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_record(path):
    checked(path)
    if not path.exists():return {}
    if path.stat().st_size > 8192:return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def write_record(path, value):
    temporary = checked(path.with_suffix('.tmp'))
    checked(path)
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path):
    checked(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise DownloadError('Browser ZIP size exceeds expected bounds')
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 20000 or sum(row.file_size for row in entries) > MAX_EXPANDED_BYTES:
                raise DownloadError('Browser ZIP expanded size exceeds expected bounds')
            names = [row.filename for row in entries]
            if len(names) != len(set(names)):raise DownloadError('Duplicate browser ZIP entries')
            for name in names:
                parts = name.replace('\\', '/').split('/')
                if name.startswith(('/', '\\')) or ':' in name or '..' in parts:
                    raise DownloadError('Unsafe browser ZIP path')
            if archive.testzip():raise DownloadError('Browser ZIP integrity check failed')
            sizes = {row.filename: row.file_size for row in entries}
            if sizes.get('chrome-win64/chrome.exe', 0) <= 0:
                raise DownloadError('Downloaded archive has no nonempty Chromium executable')
            required = ('chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak')
            missing = [name for name in required if sizes.get('chrome-win64/' + name, 0) <= 0]
            if missing:raise DownloadError('Downloaded browser runtime is incomplete: ' + ', '.join(missing))
    except (zipfile.BadZipFile, EOFError) as error:
        raise DownloadError('Browser download is not a complete valid ZIP') from error


def validator(headers):
    etag = headers.get('ETag', '')
    if isinstance(etag, str) and re.fullmatch(r'"[^"\r\n]{1,256}"', etag):
        return {'header': 'ETag', 'value': etag}
    modified = headers.get('Last-Modified', '')
    if isinstance(modified, str) and modified and len(modified) < 128 and not any(c in modified for c in '\r\n'):
        return {'header': 'Last-Modified', 'value': modified}
    return None


def valid_state(state, url):
    total = state.get('total')
    token = state.get('validator')
    return (state.get('url') == url and state.get('format') == 1
            and (total is None or type(total) is int and 0 < total <= MAX_BYTES)
            and (token is None or isinstance(token, dict) and token.get('header') in {'ETag', 'Last-Modified'}
                 and isinstance(token.get('value'), str) and 0 < len(token['value']) < 260
                 and not any(c in token['value'] for c in '\r\n')))


def response_bounds(response, offset, state):
    status = getattr(response, 'status', None) or 200
    headers = getattr(response, 'headers', {})
    if headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise DownloadError('Encoded browser archives cannot be resumed safely')
    length = headers.get('Content-Length')
    if length is not None:
        if not re.fullmatch(r'[0-9]{1,12}', str(length)):raise DownloadError('Invalid browser Content-Length')
        length = int(length)
        if not 0 < length <= MAX_BYTES:raise DownloadError('Browser download exceeds expected bounds')
    token = validator(headers)
    if status == 200:
        # Ignored Range / changed If-Range: replace from byte zero, never append.
        return 0, length, length, token
    if status != 206:raise DownloadError(f'Unexpected browser download HTTP status: {status}')
    match = re.fullmatch(r'bytes ([0-9]{1,12})-([0-9]{1,12})/([0-9]{1,12})', headers.get('Content-Range', ''))
    if not match:raise DownloadError('Invalid browser Content-Range')
    start, end, total = map(int, match.groups())
    if start != offset or not 0 <= start <= end < total <= MAX_BYTES:
        raise DownloadError('Browser range does not match the saved bytes')
    if length is not None and length != end - start + 1:raise DownloadError('Inconsistent browser range length')
    if offset and (token != state.get('validator') or state.get('total') not in (None, total)):
        raise DownloadError('Browser archive changed during resume; refusing to mix versions')
    return start, end + 1, total, token


def prune_cache(root, active):
    entries = [path for path in root.iterdir() if path != active and re.fullmatch(r'[a-f0-9]{64}', path.name)
               and path.is_dir() and not path.is_symlink()
               and not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in entries[CACHE_ENTRIES - 1:]:
        try:shutil.rmtree(path)
        except OSError:pass  # A scanner lock on obsolete cache is not a build failure.


def download_archive(url, cache_root, *, opener=None, clock=None, sleep=None, attempts=MAX_ATTEMPTS):
    opener = urllib.request.urlopen if opener is None else opener
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    if not 1 <= attempts <= 20:raise ValueError('Invalid download retry limit')
    root = Path(cache_root)
    with cache_lock(root):
        entry = checked(root / hashlib.sha256(url.encode()).hexdigest(), directory=True)
        entry.mkdir(exist_ok=True);os.utime(entry, None)
        prune_cache(root, entry)
        partial = checked(entry / 'browser.zip.part')
        state_path = checked(entry / 'download.json')
        archive = checked(entry / 'browser.zip')
        receipt_path = checked(entry / 'complete.json')
        receipt = read_record(receipt_path)
        if (archive.exists() and receipt.get('url') == url and type(receipt.get('bytes')) is int
                and receipt['bytes'] == archive.stat().st_size and 0 < receipt['bytes'] <= MAX_BYTES
                and checksum(archive) == receipt.get('sha256')):
            try:validate_archive(archive)
            except DownloadError:pass
            else:
                print(f'Browser download: verified cache reused ({receipt["bytes"] // 1024} KiB)', flush=True)
                return {'path': archive, **receipt}
        archive.unlink(missing_ok=True);receipt_path.unlink(missing_ok=True)
        state = read_record(state_path)
        if (not valid_state(state, url) or partial.exists() and
                (partial.stat().st_size > MAX_BYTES or type(state.get('total')) is int
                 and partial.stat().st_size > state['total'])):
            partial.unlink(missing_ok=True);state_path.unlink(missing_ok=True);state = {}

        def finish():
            validate_archive(partial)
            result = {'format': 1, 'url': url, 'bytes': partial.stat().st_size, 'sha256': checksum(partial)}
            # Keep the partial on metadata-write failure; a restart can seal it.
            write_record(receipt_path, result)
            partial.replace(archive)
            state_path.unlink(missing_ok=True)
            print(f'Browser download complete and ZIP verified: {result["bytes"] // 1024} KiB', flush=True)
            return {'path': archive, **result}

        # Process killed after the final byte but before publication.
        if partial.exists() and state.get('total') == partial.stat().st_size:
            try:return finish()
            except DownloadError:
                partial.unlink();state_path.unlink(missing_ok=True);state = {}

        for attempt in range(attempts):
            offset = partial.stat().st_size if partial.exists() and state.get('validator') else 0
            request = url
            if offset:
                request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-',
                    'If-Range': state['validator']['value'], 'Accept-Encoding': 'identity'})
                print(f'Browser download: resuming from {offset // 1024} KiB (request {attempt + 1}/{attempts})', flush=True)
            try:
                try:
                    response = opener(request, timeout=SOCKET_TIMEOUT)
                except urllib.error.HTTPError as error:
                    code = error.code;error.close()
                    if code in {408, 429, 500, 502, 503, 504}:raise RetryDownload(f'HTTP {code}') from error
                    if code == 416 and offset:
                        # Inconsistent server range state: restart once per request budget.
                        partial.unlink(missing_ok=True);state_path.unlink(missing_ok=True);state = {}
                        raise RetryDownload('Server rejected saved range; restarting from byte zero') from error
                    raise DownloadError(f'Browser download HTTP {code}; saved progress retained at {entry}') from error
                except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                    raise RetryDownload(str(error)) from error
                with response:
                    try:
                        start, end, total, token = response_bounds(response, offset, state)
                    except DownloadError as error:
                        if not offset:raise
                        partial.unlink(missing_ok=True);state_path.unlink(missing_ok=True);state = {}
                        raise RetryDownload('Server returned an incompatible range; restarting safely from byte zero') from error
                    if offset and start == 0:print('Browser server returned a full response; safely restarting from byte zero', flush=True)
                    with partial.open('ab' if start else 'wb') as stream:
                        state = {'format': 1, 'url': url, 'validator': token, 'total': total}
                        write_record(state_path, state)
                        size = start;last_report = clock()
                        reader = getattr(response, 'read1', None) or response.read
                        while True:
                            pending = None
                            try:chunk = reader(CHUNK_BYTES)
                            except http.client.IncompleteRead as error:chunk = error.partial;pending = error
                            except (TimeoutError, ConnectionError, urllib.error.URLError) as error:raise RetryDownload(str(error)) from error
                            if chunk:
                                if size + len(chunk) > MAX_BYTES or end is not None and size + len(chunk) > end:
                                    raise DownloadError('Browser response exceeds its declared length')
                                stream.write(chunk);stream.flush();size += len(chunk)
                                now = clock()
                                if now - last_report >= 5:
                                    suffix = f' / {total // 1024} KiB' if total else ' KiB'
                                    print(f'Browser download: {size // 1024}{suffix}; receiving data', flush=True);last_report = now
                            if pending is not None:raise RetryDownload('Browser response interrupted') from pending
                            if not chunk:break
                        stream.flush();os.fsync(stream.fileno())
                    if end is not None and size != end:raise RetryDownload(f'Incomplete browser response ({size}/{end} bytes)')
                    if total is not None and size < total:raise RetryDownload('Browser server supplied another partial segment')
                try:return finish()
                except DownloadError:
                    # A completed bad ZIP must not poison subsequent attempts.
                    partial.unlink(missing_ok=True);state_path.unlink(missing_ok=True)
                    raise
            except RetryDownload as error:
                saved = partial.stat().st_size if partial.exists() else 0
                if attempt + 1 == attempts:
                    raise DownloadError(f'Browser download paused after {attempts} network attempts; {saved} bytes retained at {entry}. Rerun the same build to continue. Last error: {error}') from error
                delay = min(2 ** attempt, 8)
                print(f'Browser download interrupted; {saved // 1024} KiB retained. Retry in {delay}s: {error}', flush=True)
                sleep(delay)
        raise AssertionError('unreachable')
