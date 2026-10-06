#!/usr/bin/env python3
"""Read-only proof of a complete sanitized public source checkout in GitHub Actions.

The marker binds a full public byte manifest to one repository. Only a real
root Git HEAD matching the supplied Actions identity can produce CI evidence.
This does not establish Windows execution, installation, or runtime success.
"""
from __future__ import annotations
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def _load_io():
    spec = importlib.util.spec_from_file_location('source_binding_io', Path(__file__).with_name('source_binding_io.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safe = _load_io()
require = safe._require
MARKER = 'CI_SOURCE_PROVENANCE.json'
MANIFEST = 'SOURCE_SHA256.json'
MODE = 'flat-git-ci'
REPRESENTATION = 'public-sanitized-source'
REPOSITORY = {'full_name': 'xaodai123gmailcom/juxin-ig-public-build',
    'id': '1406784621', 'owner_id': '337452708'}
# These incompatible controls must never be included in this representation.
FORBIDDEN_CONTROLS = frozenset({'LOCAL_SOURCE_PROVENANCE.json', 'LOCAL_BASE_SOURCE_SHA256.json',
    'ARCHIVE_SOURCE_SHA256.json', 'ARCHIVE_SOURCE_PROVENANCE.json'})
GENERATED_ROOTS = frozenset({'.git', '.venv', 'node_modules', 'build', 'dist-electron',
    'installer-output', 'dist', 'desktop/dist', 'renderer/dist', 'backend/build', 'backend/dist'})
GENERATED_FILES = frozenset({'collector_core.spec'})


def _inspect_git_metadata(directory):
    # Inspect every metadata entry before Git can follow it. Disallow alternate
    # object stores and shared worktrees: authority belongs to this root .git.
    safe._inspect(directory, directory=True)
    for item in sorted(directory.iterdir()):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not (
            getattr(info, 'st_file_attributes', 0) & safe.REPARSE_POINT),
            'Symlink/reparse point is forbidden in Git metadata')
        if stat.S_ISDIR(info.st_mode):
            _inspect_git_metadata(item)
        else:
            safe._inspect(item)


def _git(root, *args):
    # Never inherit Git redirection, object replacement, config injection or an
    # alternate index. A checkout must mean exactly this directory's own Git.
    bad = sorted(key for key in os.environ if key.startswith('GIT_') and key not in {'GIT_TERMINAL_PROMPT'})
    require(not bad, 'Unsupported Git environment override: ' + ', '.join(bad))
    # Read-only plumbing must not invoke configured monitors/filters, refresh
    # the index, acquire optional locks, or fetch promised objects. Construct
    # this trusted environment only after rejecting caller Git overrides.
    environment = dict(os.environ)
    environment.update(GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='',
        GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(['git', '--no-pager', '--no-replace-objects',
        '--no-optional-locks', '-c', 'core.fsmonitor=false',
        '-c', 'protocol.allow=never', '-c', 'protocol.ext.allow=never',
        '-C', str(root), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, check=False, timeout=30)
    require(result.returncode == 0, 'Git source inspection failed: ' + ' '.join(args[:2]))
    return result.stdout


def _git_snapshot(root):
    require(os.path.lexists(root / '.git'), 'Flat source root requires its own Git checkout')
    _inspect_git_metadata(root / '.git')
    for name in ('commondir', 'objects/info/alternates', 'objects/info/http-alternates', 'info/grafts'):
        require(not os.path.lexists(root / '.git' / name), 'External Git metadata is forbidden: ' + name)
    top = Path(os.fsdecode(_git(root, 'rev-parse', '--show-toplevel')).strip())
    require(top == root, 'Ancestor Git repository is not the flat source checkout')
    git_dir = Path(os.fsdecode(_git(root, 'rev-parse', '--absolute-git-dir')).strip())
    require(git_dir == root / '.git', 'Git directory is not the root-owned .git')
    require(_git(root, 'rev-parse', '--is-bare-repository').strip() == b'false', 'Bare Git repositories cannot supply source identity')
    require(_git(root, 'rev-parse', '--is-shallow-repository').strip() in {b'true', b'false'}, 'Invalid Git repository')
    head = _git(root, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip()
    require(re.fullmatch('[a-f0-9]{40}', head), 'Exact SHA-1 Git HEAD is required')
    entries = {}
    for line in _git(root, 'ls-tree', '-r', '-z', '--full-tree', head).split(b'\0'):
        if not line:
            continue
        metadata, raw_name = line.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        name = raw_name.decode('utf-8')
        safe._safe_name(name)
        require(mode in {'100644', '100755'} and kind == 'blob', 'Git symlinks/submodules are forbidden: ' + name)
        require(name not in entries, 'Duplicate Git path: ' + name)
        entries[name] = {'mode': mode, 'blob': oid}
    index = {}
    for line in _git(root, 'ls-files', '--stage', '--full-name', '-z').split(b'\0'):
        if not line:
            continue
        metadata, raw_name = line.split(b'\t', 1)
        mode, oid, stage = metadata.decode('ascii').split()
        name = raw_name.decode('utf-8')
        safe._safe_name(name)
        require(stage == '0', 'Unmerged Git index entry: ' + name)
        require(mode in {'100644', '100755'} and name not in index, 'Unsupported Git index entry: ' + name)
        index[name] = {'mode': mode, 'blob': oid}
    require(index == entries, 'Git index differs from HEAD paths/modes/blobs')
    return head, entries


def _inventory(root, tracked):
    """Reject every untracked executable, even if ignored by Git.

    Generated model data remain allowed; non-executable
    untracked build logs are not source identity. Tracked bytes are exhaustive.
    """
    def visit(directory):
        safe._inspect(directory, directory=True)
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            safe._safe_name(name)
            # Reject link/junction metadata before any following type query;
            # even statting a remote reparse target is outside source inspection.
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not (
                getattr(info, 'st_file_attributes', 0) & safe.REPARSE_POINT),
                'Symlink/reparse point is forbidden: ' + name)
            if stat.S_ISDIR(info.st_mode):
                safe._inspect(path, directory=True)
                if name in GENERATED_ROOTS or path.name in safe.IGNORED_DIRECTORY_NAMES:
                    continue
                visit(path)
                continue
            safe._inspect(path)
            if name not in tracked and name not in GENERATED_FILES:
                data = safe._read(root, name)
                executable = (path.suffix.casefold() in safe.EXECUTABLE_SUFFIXES or
                    bool(path.lstat().st_mode & 0o111) or data.startswith(b'#!'))
                require(not executable, 'Unsealed executable source: ' + name)
    visit(root)



def verify_ci_source_binding(root):
    """Read-only proof of exact real CI checkout, public manifest and every blob."""
    root = Path(os.path.abspath(os.fspath(root)))
    safe._parents(root)
    safe._inspect(root, directory=True)
    require(not any(os.path.lexists(root / name) for name in FORBIDDEN_CONTROLS),
        'Private and public source controls must not be mixed')
    require(os.environ.get('GITHUB_ACTIONS', '').strip().casefold() == 'true', 'CI mode requires GITHUB_ACTIONS=true')
    sha = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[a-f0-9]{40}', sha), 'CI mode requires exact GITHUB_SHA')
    for key, field in (('GITHUB_REPOSITORY', 'full_name'), ('GITHUB_REPOSITORY_ID', 'id'), ('GITHUB_REPOSITORY_OWNER_ID', 'owner_id')):
        require(os.environ.get(key) == REPOSITORY[field], 'CI repository identity mismatch: ' + key)
    head, entries = _git_snapshot(root)
    require(head == sha, 'Git HEAD differs from actual GITHUB_SHA')
    marker_bytes = safe._read(root, MARKER)
    marker = safe._json(marker_bytes, MARKER)
    require(set(marker) == {'schema', 'mode', 'representation', 'repository', 'product_version',
        'source_revision', 'source_manifest_sha256'}, 'CI marker fields are missing or unexpected')
    require(type(marker['schema']) is int and marker['schema'] == 2 and marker['mode'] == MODE and
        marker['representation'] == REPRESENTATION, 'CI marker schema/mode/representation mismatch')
    require(marker['repository'] == REPOSITORY, 'CI marker repository identity mismatch')
    manifest_bytes = safe._read(root, MANIFEST)
    manifest = safe._manifest(manifest_bytes, MANIFEST)
    require(marker['source_manifest_sha256'] == safe._sha256(manifest_bytes), 'CI manifest digest mismatch')
    require(set(entries) == set(manifest) | {MARKER, MANIFEST}, 'Git blob paths differ from full CI manifest')
    require(not {MARKER, MANIFEST} & set(manifest), 'Self-referential CI manifest')
    require(not FORBIDDEN_CONTROLS & set(entries), 'Private controls are forbidden in the public manifest')
    package = safe._json(safe._read(root, 'package.json'), 'package.json')
    revision = re.search(r'^Source revision:\s*(\S+)\s*$', safe._read(root, 'BUILD_REVISION.txt').decode('utf-8'), re.M)
    require(isinstance(marker['product_version'], str) and isinstance(marker['source_revision'], str) and
        package.get('version') == marker['product_version'] and revision and revision[1] == marker['source_revision'],
        'CI version/revision mismatch')
    for name, git_entry in entries.items():
        data = safe._read(root, name)
        if os.name != 'nt':
            executable = bool(safe._inspect(root.joinpath(*safe._safe_name(name))).st_mode & 0o111)
            require(executable == (git_entry['mode'] == '100755'), 'Working source executable mode differs from Git: ' + name)
        if name in manifest:
            require(safe._sha256(data) == manifest[name], 'CI source checksum mismatch: ' + name)
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        require(blob == git_entry['blob'], 'Working source differs from Git blob: ' + name)
    _inventory(root, entries)
    require((head, entries) == _git_snapshot(root), 'Git identity changed during verification')
    require(safe._read(root, MARKER) == marker_bytes and safe._read(root, MANIFEST) == manifest_bytes,
        'CI manifest changed during verification')
    blob_bytes = (json.dumps(entries, sort_keys=True, separators=(',', ':')) + '\n').encode()
    return {'schema': 2, 'mode': MODE, 'representation': REPRESENTATION, 'repository': dict(REPOSITORY),
        'execution': 'github-actions', 'git_checkout': True, 'github_actions': True, 'source_commit': head,
        'source_manifest_sha256': safe._sha256(manifest_bytes), 'source_marker_sha256': safe._sha256(marker_bytes),
        'git_tree': _git(root, 'rev-parse', head + '^{tree}').decode().strip(),
        'git_blob_paths_sha256': safe._sha256(blob_bytes), 'tracked_files_verified': len(entries),
        'effective_files_verified': len(manifest)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_ci_source_binding(args.root)
    except (safe.SourceBindingError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('CI_SOURCE_BINDING=FAIL ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
