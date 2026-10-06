"""Runner-only Unicode source and copied-frozen checks; never rebuild the app here."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import stat
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_ci_common import (ROOT, check_telemetry_consent, read_json, regular,
    require, run_owned, state_root, validate_source_build, verify_run_state, write_json)

UNICODE_PARENT = Path('\u805a\u946b\u6d4b\u8bd5') / '\u65b0\u5efa\u6587\u4ef6\u5939 (4)'
UNICODE_SOURCE = UNICODE_PARENT / '\u91c7\u96c6\u5668'
ASSETS = Path('backend/app/assets/person_classifier')
MODELS = ('face-detection-retail-0004', 'age-gender-recognition-retail-0013',
          'person-detection-retail-0013', 'person-attributes-recognition-crossroad-0230')
MODEL_FILES = {name + suffix for name in MODELS for suffix in ('.xml', '.bin')}
MAX_ENTRIES, MAX_DEPTH = 40000, 24
MAX_FILE_BYTES, MAX_TREE_BYTES = 512 * 1024 * 1024, 4 * 1024 * 1024 * 1024


def digest(path, algorithm='sha256'):
    path = regular(path)
    before = path.stat()
    require(before.st_size <= MAX_FILE_BYTES, 'Unicode file exceeds byte bound')
    checksum, total = hashlib.new(algorithm), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            require(total <= MAX_FILE_BYTES, 'Unicode file grew beyond byte bound')
            checksum.update(chunk)
        finished = os.fstat(stream.fileno())
    after = regular(path).stat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    require(identity(before) == identity(opened) == identity(finished) == identity(after) and total == before.st_size,
            'Unicode file changed while reading')
    return checksum.hexdigest()


def files(root):
    """Reject links before traversal; cap every owned/source tree inspection."""
    regular(root, directory=True)
    pending, found, count, total = [(root, 0)], {}, 0, 0
    while pending:
        directory, depth = pending.pop()
        require(depth <= MAX_DEPTH, 'Unicode tree exceeds depth bound')
        regular(directory, directory=True)
        for path in directory.iterdir():
            count += 1
            require(count <= MAX_ENTRIES, 'Unicode tree exceeds entry bound')
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400),
                    'Linked Unicode gate entry is forbidden')
            if stat.S_ISDIR(info.st_mode):
                pending.append((path, depth + 1))
            else:
                regular(path)
                total += info.st_size
                require(info.st_size <= MAX_FILE_BYTES and total <= MAX_TREE_BYTES, 'Unicode tree exceeds byte bound')
                found[path.relative_to(root).as_posix()] = path
    return found


def fingerprint(root):
    rows = [(name, path.stat().st_size, digest(path)) for name, path in sorted(files(root).items())]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def copy_file(source, destination, expected):
    require(digest(source) == expected, 'Unicode copy source bytes changed')
    require(regular(source).stat().st_size <= MAX_FILE_BYTES, 'Unicode copy file exceeds bound')
    destination.parent.mkdir(parents=True, exist_ok=True)
    regular(destination.parent, directory=True)
    with regular(source).open('rb') as incoming, destination.open('xb') as outgoing:
        total = 0
        while chunk := incoming.read(1024 * 1024):
            total += len(chunk)
            require(total <= MAX_FILE_BYTES, 'Unicode copy source grew beyond bound')
            outgoing.write(chunk)
    require(digest(source) == digest(destination) == expected, 'Unicode copy bytes changed')


def source_entries():
    manifest = read_json(ROOT / 'SOURCE_SHA256.json')
    selected = {}
    for name, checksum in manifest.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and '..' not in relative.parts and '\\' not in name,
                'Unsafe sealed source path')
        if relative.parts[0] in ('backend', 'scripts'):
            selected[name] = checksum
    require('backend/requirements.txt' in selected and 'scripts/verify_openvino_windows.py' in selected,
            'Unicode source inputs are not sealed')
    return selected


def verify_source_copy(source, *, models):
    expected = source_entries()
    actual = {prefix + '/' + name: path for prefix in ('backend', 'scripts')
              for name, path in files(source / prefix).items()}
    allowed = set(expected)
    if models:
        allowed.update((ASSETS / name).as_posix() for name in MODEL_FILES)
    require(set(actual) == allowed, 'Unicode source copy contains missing or unsealed files')
    for name, checksum in expected.items():
        require(digest(ROOT / name) == digest(actual[name]) == checksum, 'Unicode source differs from sealed checkout')
    if models:
        manifest = read_json(source / ASSETS / 'MODEL_SOURCES.json')
        rows = manifest.get('files', [])
        require(len(rows) == 8 and {row.get('filename') for row in rows} == MODEL_FILES,
                'Unicode model manifest must contain the original four model pairs')
        for row in rows:
            path = regular(source / ASSETS / row['filename'])
            require(path.stat().st_size == row['size'], 'Unicode model size differs from sealed pin')
            require(digest(path, 'sha384') == row['sha384'], 'Unicode model bytes differ from sealed pin')


def owned_work(identifier, kind):
    require(re.fullmatch(r'[0-9a-f]{32}', identifier), 'Invalid Unicode gate owner')
    state = verify_run_state()
    work = state_root() / ('unicode-' + kind + '-' + identifier)
    require(str(work).isascii(), 'Fresh Unicode gate cache parent must be ASCII')
    regular(work, directory=True)
    ownership = read_json(work / 'ownership.json')
    require(ownership == {'kind': kind, 'id': identifier, 'nonce': state['nonce'],
                         'source_manifest_sha256': state['source_provenance']['source_manifest_sha256']},
            'Unicode directory does not belong to this exact source/run')
    return work


def create_work(kind):
    state = verify_run_state()
    identifier = uuid.uuid4().hex
    work = state_root() / ('unicode-' + kind + '-' + identifier)
    require(str(work).isascii(), 'Fresh Unicode gate cache parent must be ASCII')
    work.mkdir(exist_ok=False)
    write_json(work / 'ownership.json', {'kind': kind, 'id': identifier, 'nonce': state['nonce'],
        'source_manifest_sha256': state['source_provenance']['source_manifest_sha256']})
    return identifier, work


def cleanup(identifier, kind):
    work = owned_work(identifier, kind)
    files(work)  # Refuse cleanup if any junction, link, special entry or bound changed.
    shutil.rmtree(work)


def python_command(python, script, *arguments):
    return [str(python), '-I', '-B', '-X', 'utf8', str(script), *map(str, arguments)]


def source_gates():
    identifier, work = create_work('source')
    try:
        source = work / UNICODE_SOURCE
        source.mkdir(parents=True, exist_ok=False)
        for name, checksum in source_entries().items():
            copy_file(ROOT / name, source / name, checksum)
        verify_source_copy(source, models=False)
        cache = work / 'source-cache'
        cache.mkdir(exist_ok=False)
        require(not list(cache.iterdir()), 'Source cache must start empty')
        python = source / '.venv/Scripts/python.exe'
        run_owned('unicode-source-create-venv', [sys.executable, '-I', '-B', '-X', 'utf8',
                  '-m', 'venv', str(source / '.venv')], 180)
        run_owned('unicode-source-pinned-dependencies', python_command(python,
            source / 'scripts/install_python_dependencies.py', '--project-root', source), 1800)
        run_owned('unicode-source-pinned-models', python_command(python,
            source / 'scripts/download_person_models.py'), 600)
        run_owned('unicode-source-model-integrity', python_command(python,
            source / 'scripts/download_person_models.py', '--check'), 180)
        verify_source_copy(source, models=True)
        environment = {'IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE': '1',
                       'IGAC_OPENVINO_CACHE_ROOT': str(cache)}
        for mode in ('source-probe', 'classifier-probe'):
            run_owned('unicode-' + mode, python_command(python, Path(__file__), mode, identifier), 300, environment)
        verify_source_copy(source, models=True)
        verify_run_state()
    finally:
        cleanup(identifier, 'source')


def source_context(identifier):
    work = owned_work(identifier, 'source')
    source, cache = work / UNICODE_SOURCE, work / 'source-cache'
    verify_source_copy(source, models=True)
    require(Path(sys.prefix).resolve() == regular(source / '.venv', directory=True).resolve(),
            'Unicode inference must use its own Unicode venv')
    require(os.environ.get('IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE') == '1' and
            Path(os.environ.get('IGAC_OPENVINO_CACHE_ROOT', '')).resolve() == regular(cache, directory=True).resolve(),
            'Unicode inference lost its strict source/fresh cache settings')
    require(not any(name == 'openvino' or name.startswith('openvino.') for name in sys.modules),
            'Unicode probe must start before any OpenVINO import')
    distribution = importlib.metadata.distribution('openvino')
    original_dll = regular(Path(distribution.locate_file('openvino/libs/openvino.dll'))).resolve()
    require(distribution.version == '2025.4.1' and not str(original_dll).isascii() and
            original_dll.is_relative_to((source / '.venv').resolve()),
            'Original OpenVINO DLL must belong to the copied Unicode venv')
    check_telemetry_consent()
    # run_owned intentionally keeps a fixed checkout cwd. Only this validated,
    # exact owned source may become the child interpreter's working directory.
    os.chdir(source)
    sys.path[:0] = [str(source / 'backend'), str(source / 'scripts')]
    return work, source, cache


def assert_cache(cache):
    from app.openvino_native_bootstrap import (prepare_openvino_native_runtime,
                                             assert_loaded_openvino_from_cache)
    loaded = regular(assert_loaded_openvino_from_cache(prepare_openvino_native_runtime())).resolve()
    require(str(loaded).isascii() and loaded.is_relative_to(cache.resolve()),
            'Unicode source reused a native cache outside its fresh owned root')


def source_probe(identifier):
    work, source, cache = source_context(identifier)
    check_telemetry_consent()
    verifier = runpy.run_path(str(source / 'scripts/verify_openvino_windows.py'))
    require(verifier['_model_directory']().resolve() == (source / ASSETS).resolve(),
            'Source inference must use copied Unicode model assets')
    # This sealed original function compiles and infers all four pinned models,
    # then exercises the production gender-output parser.
    verifier['verify_source'](work / 'unicode-native-manifest.json')
    assert_cache(cache)
    check_telemetry_consent()


def classifier_probe(identifier):
    _, source, cache = source_context(identifier)
    check_telemetry_consent()
    from PIL import Image
    from app.person_recognition import LocalOpenVinoPersonClassifier, exercise_local_openvino_gender_branch
    classifier = LocalOpenVinoPersonClassifier()
    expected = (source / ASSETS).resolve()
    require(classifier.ASSET_DIRECTORY.resolve() == expected and
            {path.resolve() for path in classifier._model_paths()} == {expected / name for name in MODEL_FILES},
            'Classifier models must belong to the copied Unicode source')
    sample = io.BytesIO()
    Image.new('RGB', (96, 96), (127, 127, 127)).save(sample, format='PNG')
    result = classifier.classify(sample.getvalue())
    require(result.checked and result.reason not in {'model_unavailable', 'model_integrity_failed', 'local_inference_failed'},
            'Actual Unicode source classifier inference failed')
    face, gender, person, attribute = classifier._get_compiled_models()
    require(all(model is not None for model in (face, gender, person, attribute)), 'Four classifier models must compile')
    category, confidence = exercise_local_openvino_gender_branch(gender)
    require(category in {'male', 'female'} and math.isfinite(confidence), 'Actual production gender branch failed')
    assert_cache(cache)
    check_telemetry_consent()


def frozen_gates():
    state = verify_run_state()
    validate_source_build(state)
    identifier, work = create_work('frozen')
    try:
        original = ROOT / 'dist/collector_core'
        copied = work / UNICODE_PARENT / 'collector_core'
        python = ROOT / '.venv/Scripts/python.exe'
        manifest = ROOT / 'build/openvino-native-manifest.json'
        verifier = ROOT / 'scripts/verify_openvino_windows.py'
        run_owned('unicode-frozen-original-layout', python_command(python, verifier,
            'frozen', '--manifest', manifest, '--dist', original), 180)
        sealed = fingerprint(original)
        for name, path in files(original).items():
            copy_file(path, copied / name, digest(path))
        require(fingerprint(original) == fingerprint(copied) == sealed, 'Copied frozen Core differs from verified build')
        copied_manifest = work / 'original-native-manifest.json'
        copy_file(manifest, copied_manifest, digest(manifest))
        require(digest(copied / '_internal/openvino-native-manifest.json') == digest(copied_manifest),
                'Copied frozen Core must use its actual original native manifest')
        cache = work / 'frozen-cache'
        cache.mkdir(exist_ok=False)
        require(not list(cache.iterdir()), 'Frozen cache must start empty')
        run_owned('unicode-frozen-copied-layout', python_command(python, verifier,
            'frozen', '--manifest', copied_manifest, '--dist', copied), 180)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        run_owned('unicode-frozen-real-inference', [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
            '-File', str(ROOT / 'scripts/test_frozen_openvino.ps1'), '-Executable', str(copied / 'collector_core.exe'),
            '-LogPath', str(work / 'frozen-openvino.log'), '-TimeoutSeconds', '180',
            '-RequireNonAsciiSource', '-ExpectedCacheRoot', str(cache)], 240)
        run_owned('unicode-frozen-real-service', python_command(python, ROOT / 'scripts/verify_frozen_core_service.py',
            '--executable', copied / 'collector_core.exe', '--log', work / 'frozen-service.log'), 180)
        require(fingerprint(original) == fingerprint(copied) == sealed, 'Frozen Core bytes changed during Unicode checks')
        validate_source_build(verify_run_state())
    finally:
        cleanup(identifier, 'frozen')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('source', 'frozen', 'source-probe', 'classifier-probe'))
    parser.add_argument('identifier', nargs='?')
    args = parser.parse_args()
    verify_run_state()  # Real authorized hosted Windows and raw ASCII 0 only.
    if args.mode in ('source', 'frozen'):
        require(args.identifier is None, 'Top-level Unicode gates create their own fresh owner')
        (source_gates if args.mode == 'source' else frozen_gates)()
    else:
        require(args.identifier is not None, 'Unicode child requires its exact owner')
        (source_probe if args.mode == 'source-probe' else classifier_probe)(args.identifier)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
