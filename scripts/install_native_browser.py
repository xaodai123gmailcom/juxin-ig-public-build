"""Fallback for a CDN outage: install the exact pinned Chrome for Testing build.

Uses the version/revision shipped by the installed Playwright driver. No browser
version is guessed and no substitute runtime is silently accepted.
"""
from __future__ import annotations
import hashlib,http.client,json,os,platform,re,shutil,ssl,sys,tempfile,time,urllib.error,urllib.request,zipfile,zlib
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from browser_download import download_archive
import playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.browser_bundle import (BUNDLE_DESCRIPTOR, STABLE_METADATA_URL, bundle_descriptor,
                                official_archive_url, stable_candidate)

def check_directory(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or not path.is_dir():
            raise RuntimeError(f'Browser runtime must be a real directory, not a link or reparse point: {path}')


@contextmanager
def extraction_workspace(parent):
    """Cleanup of owned scratch must not replace verification/rollback results."""
    folder = Path(tempfile.mkdtemp(prefix='juxin-browser-extract-', dir=parent))
    try:
        yield folder
    finally:
        try:
            shutil.rmtree(folder)
        except OSError as error:
            # Outside build/browsers, so a scanner lock cannot enter the package.
            print(f'Browser extraction scratch retained at {folder}: {error}', flush=True)


def fetch_stable_candidate(minimum_version, *, opener=None, sleep=None):
    """Retry transient metadata transport failures; never relax version checks."""
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    limit = 1024 * 1024
    for attempt in range(3):
        try:
            with opener(STABLE_METADATA_URL, timeout=30) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise RuntimeError('Stable browser metadata exceeds size limit')
                declared = getattr(response, 'headers', {}).get('Content-Length')
                if declared is not None:
                    if not str(declared).isdigit() or int(declared) > limit:
                        raise RuntimeError('Invalid stable browser metadata Content-Length')
                    if len(raw) != int(declared):
                        raise http.client.IncompleteRead(raw, int(declared) - len(raw))
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            if isinstance(error, urllib.error.HTTPError):
                retry = error.code in {408, 429, 500, 502, 503, 504}
                error.close()
            elif isinstance(error, urllib.error.URLError):
                retry = not isinstance(error.reason, (PermissionError, ssl.SSLCertVerificationError))
            else:
                retry = True
            if not retry or attempt == 2:
                raise
            print(f'Stable browser metadata temporarily unavailable; retry {attempt + 2}/3: {error}', flush=True)
            sleep(attempt + 1)
    # A complete but invalid response is not a transport retry. Keep the strict
    # official URL, Stable channel and newer-version checks in browser_bundle.
    return stable_candidate(json.loads(raw), minimum_version)


def verify_extracted_files(archive, directory):
    """Read staged files back from disk before replacing any previous runtime."""
    for member in archive.infolist():
        path = directory / member.filename
        if member.is_dir():continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or not path.is_file():
            raise RuntimeError('Extracted browser entry is not a regular file: ' + member.filename)
        size = 0;crc = 0
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > member.file_size:break
                crc = zlib.crc32(chunk, crc)
        if size != member.file_size or crc != member.CRC:
            raise RuntimeError('Extracted browser file failed disk readback integrity check: ' + member.filename)


def publish_runtime(staged, destination, *, verify=None):
    """Publish only sealed staging; retain the previous copy until rename works."""
    check_directory(destination)
    backup = None
    if destination.exists():
        backup = destination.parent.parent / ('juxin-browser-previous-' + destination.name + '-' + uuid4().hex)
        destination.rename(backup)
    published = False
    try:
        # Staging is a sibling under build/, so this is a same-volume rename.
        # Unlike shutil.move, failure cannot silently fall back to partial copy.
        staged.rename(destination)
        published = True
        if verify is not None:
            # Validate at the final path, before deleting the previous copy.
            # A successful download is not evidence of a runnable browser.
            verify(destination)
    except BaseException as error:
        if published:
            rejected = destination.parent.parent / ('juxin-browser-rejected-' + destination.name + '-' + uuid4().hex)
            try:
                destination.rename(rejected)
            except OSError as rollback:
                raise RuntimeError(f'Browser verification failed ({error}); cannot remove rejected runtime from {destination}; previous runtime preserved at {backup}; rollback failed: {rollback}') from error
        if backup is not None:
            try:
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError('another entry appeared at the destination')
                backup.rename(destination)
            except OSError as rollback:
                raise RuntimeError(f'Browser publication failed ({error}); previous runtime preserved at {backup}; rollback failed: {rollback}') from error
            except RuntimeError as rollback:
                raise RuntimeError(f'Browser publication failed ({error}); previous runtime preserved at {backup}; {rollback}') from error
        raise
    if backup is not None:
        try:
            shutil.rmtree(backup)
        except OSError as error:
            # The published runtime is complete. A scanner's lock on obsolete
            # files must not invalidate it or remove the new version.
            print(f'Browser runtime published; old build copy preserved at {backup}: {error}', flush=True)


def main(*, verify=None, browser_root=None, compatibility=False):
    registry=Path(playwright.__file__).parent/'driver'/'package'/'browsers.json'
    chromium=next(r for r in json.loads(registry.read_text())['browsers'] if r['name']=='chromium')
    version=chromium['browserVersion'];revision=chromium['revision']
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+',version) or not str(revision).isdigit():raise RuntimeError('Invalid pinned browser metadata')
    if os.name!='nt' or platform.machine().lower() not in {'amd64','x86_64'}:raise RuntimeError('Fallback builder supports Windows x64 only')
    root=Path(browser_root if browser_root is not None else os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    check_directory(root)
    root.mkdir(parents=True,exist_ok=True)
    destination=root/('chromium-'+str(revision))
    check_directory(destination)
    driver_version = version
    recorded = bundle_descriptor(destination, revision, driver_version)
    version = recorded['version']
    compatible = compatibility or recorded['source'] == 'chrome-for-testing-stable'
    if compatible and verify is None:
        raise RuntimeError('Compatibility browser requires full final-path verification')
    if compatibility:
        version, url = fetch_stable_candidate(version)
    else:
        url = official_archive_url(version)
    print(f'Downloading Chrome for Testing {version} from the official mirror...',flush=True)
    # Downloads persist outside release resources; only verified extracted files
    # enter build/browsers. Network interruptions can resume across build retries.
    downloaded = download_archive(url, root.parent / 'browser-download-cache')
    archive = downloaded['path']
    with extraction_workspace(root.parent) as folder:
        extract=Path(folder)/'extracted';extract.mkdir()
        with zipfile.ZipFile(archive) as z:
            if sum(m.file_size for m in z.infolist())>2*1024*1024*1024:raise RuntimeError('Browser ZIP is unexpectedly large')
            for m in z.infolist():
                target=(extract/m.filename).resolve()
                if not target.is_relative_to(extract.resolve()):raise RuntimeError('Unsafe browser ZIP path')
            if z.testzip():raise RuntimeError('Browser ZIP integrity check failed')
            z.extractall(extract)
            verify_extracted_files(z, extract)
        executable=extract/'chrome-win64'/'chrome.exe'
        if not executable.is_file() or executable.stat().st_size == 0:raise RuntimeError('Downloaded archive has no nonempty Chromium executable')
        required=('chrome.dll','icudtl.dat','resources.pak','locales/en-US.pak')
        missing=[name for name in required if not (executable.parent/name).is_file() or (executable.parent/name).stat().st_size == 0]
        if missing:raise RuntimeError('Downloaded browser runtime is incomplete: '+', '.join(missing))
        # Seal before moving the old runtime. A disk-full marker failure leaves
        # the previous copy untouched; failed publication rolls it back.
        if compatible:
            record = {'format': 1, 'source': 'chrome-for-testing-stable',
                      'driver_revision': str(revision), 'driver_version': driver_version,
                      'version': version, 'download_url': url, 'archive_sha256': downloaded['sha256']}
            (extract / BUNDLE_DESCRIPTOR).write_text(json.dumps(record, indent=2), encoding='utf-8')
        (extract/'INSTALLATION_COMPLETE').touch()
        publish_runtime(extract, destination, verify=verify)
    state='final-path browser verification passed' if verify is not None else 'real-browser verification follows'
    print(f'Installed verified-version Chrome for Testing {version} from official mirror; {state}',flush=True)
if __name__=='__main__':main()
