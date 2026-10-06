"""Atomically publish a complete ZIP64 portable archive after reading it back."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile


def inventory(root: Path) -> dict:
    result, names = {}, set()
    def visit(path):
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError(f'Portable source contains a link or reparse point: {path}')
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f'Portable source is not a regular file: {path}')
        name = path.relative_to(root.parent).as_posix() + ('/' if directory else '')
        key = name.rstrip('/').casefold()
        if key in names or '\\' in name:
            raise RuntimeError(f'Portable source has ambiguous Windows paths: {name}')
        names.add(key)
        result[name] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino) if not directory else None
        if directory:
            for child in sorted(path.iterdir()):
                visit(child)
    visit(root)
    if not any(value is not None for value in result.values()):
        raise RuntimeError('Portable source is empty')
    return result


def verify_archive(path: Path, expected: dict) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise RuntimeError('Portable archive entries differ from the staged application')
        for name, digest in expected.items():
            with archive.open(name) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                raise RuntimeError(f'Portable archive content mismatch: {name}')


def package_archive(source: Path, destination: Path) -> dict:
    source, destination = source.absolute(), destination.absolute()
    if not source.is_dir():
        raise RuntimeError('Portable source directory is missing')
    if destination.resolve().is_relative_to(source.resolve()):
        raise RuntimeError('Portable archive must be outside the staged application')
    before = inventory(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + '.', suffix='.tmp', dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    digests = {}
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True, strict_timestamps=False) as archive:
            for name, stamp in before.items():
                path = source.parent / name.rstrip('/')
                if stamp is None:
                    archive.writestr(zipfile.ZipInfo(name), b'')
                    digests[name] = hashlib.sha256(b'').hexdigest()
                    continue
                digest = hashlib.sha256()
                info = zipfile.ZipInfo.from_file(path, name, strict_timestamps=False)
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open('rb') as stream, archive.open(info, 'w', force_zip64=True) as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                        output.write(chunk)
                digests[name] = digest.hexdigest()
        if inventory(source) != before:
            raise RuntimeError('Portable source changed while creating the archive')
        verify_archive(temporary, digests)
        with temporary.open('r+b') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return {'verified': True, 'files': sum(v is not None for v in before.values()),
                'sha256': digest, 'bytes': destination.stat().st_size}
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            # This untrusted .tmp is never selected as the release artifact.
            # Keep the original write/verification/publication error intact.
            print(f'Portable archive scratch retained at {temporary}: {error}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = package_archive(args.source, args.destination)
    except Exception as error:
        print(f'PORTABLE_ARCHIVE_CHECK=FAIL: {error}')
        return 1
    print('PORTABLE_ARCHIVE_CHECK=PASS ' + json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
