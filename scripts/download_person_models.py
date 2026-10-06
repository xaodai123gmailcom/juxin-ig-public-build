#!/usr/bin/env python3
"""Download and verify the pinned local person-recognition model assets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
import re
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DIR = PROJECT_ROOT / "backend" / "app" / "assets" / "person_classifier"
DEFAULT_MANIFEST = DEFAULT_ASSET_DIR / "MODEL_SOURCES.json"
OFFICIAL_DOWNLOAD_HOST = "storage.openvinotoolkit.org"
SHA384_PATTERN = re.compile(r"[0-9a-f]{96}")
CHUNK_SIZE = 1024 * 1024


class ModelAssetError(RuntimeError):
    """Raised when a model manifest, download, or verification is invalid."""


def _exception_summary(exc: BaseException) -> str:
    try:
        detail = str(exc)
    except Exception as stringify_error:
        detail = f"unprintable native error ({type(stringify_error).__name__})"
    return f"{type(exc).__name__}: {detail}"


@dataclass(frozen=True, slots=True)
class ModelAsset:
    filename: str
    url: str
    size: int
    sha384: str


def _manifest_asset(raw: Any, index: int) -> ModelAsset:
    if not isinstance(raw, dict):
        raise ModelAssetError(f"manifest files[{index}] must be an object")

    filename = raw.get("filename")
    url = raw.get("url")
    size = raw.get("size")
    sha384 = raw.get("sha384")
    label = f"manifest files[{index}]"

    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ModelAssetError(f"{label}.filename must be a plain file name")
    if not isinstance(url, str):
        raise ModelAssetError(f"{label}.url must be a string")
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != OFFICIAL_DOWNLOAD_HOST:
        raise ModelAssetError(
            f"{label}.url must use https://{OFFICIAL_DOWNLOAD_HOST}/"
        )
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ModelAssetError(f"{label}.size must be a positive integer")
    if not isinstance(sha384, str) or not SHA384_PATTERN.fullmatch(sha384.casefold()):
        raise ModelAssetError(f"{label}.sha384 must be a 96-character hex digest")

    return ModelAsset(
        filename=filename,
        url=url,
        size=size,
        sha384=sha384.casefold(),
    )


def load_manifest(path: Path) -> list[ModelAsset]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelAssetError(f"model manifest was not found: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelAssetError(f"model manifest could not be read: {path}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ModelAssetError("model manifest schema_version must be 1")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ModelAssetError("model manifest files must be a non-empty array")

    assets = [_manifest_asset(item, index) for index, item in enumerate(raw_files)]
    filenames = [asset.filename for asset in assets]
    if len(filenames) != len(set(filenames)):
        raise ModelAssetError("model manifest contains duplicate filenames")
    return assets


def verify_file(path: Path, asset: ModelAsset) -> str | None:
    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"cannot read metadata ({exc})"

    if not path.is_file():
        return "not a regular file"
    if file_size != asset.size:
        return f"wrong size ({file_size} bytes; expected {asset.size})"

    digest = hashlib.sha384()
    try:
        with path.open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        return f"cannot read data ({exc})"
    actual_digest = digest.hexdigest()
    if actual_digest != asset.sha384:
        return f"SHA-384 mismatch ({actual_digest}; expected {asset.sha384})"
    return None


def _download_once(asset: ModelAsset, destination: Path, timeout: float) -> None:
    request = urllib.request.Request(
        asset.url,
        headers={"User-Agent": "JuxinIGCollector-ModelDownloader/1.0"},
    )
    temporary_path: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            advertised_size = response.headers.get("Content-Length")
            if advertised_size is not None:
                try:
                    parsed_size = int(advertised_size)
                except ValueError as exc:
                    raise ModelAssetError(
                        f"{asset.filename}: server returned an invalid Content-Length"
                    ) from exc
                if parsed_size != asset.size:
                    raise ModelAssetError(
                        f"{asset.filename}: server announced {parsed_size} bytes; "
                        f"expected {asset.size}"
                    )

            digest = hashlib.sha384()
            byte_count = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{asset.filename}.",
                suffix=".part",
                dir=destination,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := response.read(CHUNK_SIZE):
                    byte_count += len(chunk)
                    if byte_count > asset.size:
                        raise ModelAssetError(
                            f"{asset.filename}: download exceeded expected size"
                        )
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

        if byte_count != asset.size:
            raise ModelAssetError(
                f"{asset.filename}: downloaded {byte_count} bytes; expected {asset.size}"
            )
        actual_digest = digest.hexdigest()
        if actual_digest != asset.sha384:
            raise ModelAssetError(
                f"{asset.filename}: downloaded SHA-384 {actual_digest}; "
                f"expected {asset.sha384}"
            )
        if temporary_path is None:
            raise ModelAssetError(f"{asset.filename}: temporary download was not created")
        os.replace(temporary_path, destination / asset.filename)
        temporary_path = None
    except ModelAssetError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ModelAssetError(f"{asset.filename}: download failed: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_asset(
    asset: ModelAsset,
    destination: Path,
    *,
    timeout: float,
    attempts: int,
) -> None:
    last_error: ModelAssetError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _download_once(asset, destination, timeout)
            verification_error = verify_file(destination / asset.filename, asset)
            if verification_error is not None:
                raise ModelAssetError(
                    f"{asset.filename}: post-download verification failed: "
                    f"{verification_error}"
                )
            return
        except ModelAssetError as exc:
            last_error = exc
            if attempt < attempts:
                print(
                    f"WARNING: {exc}; retrying ({attempt + 1}/{attempts})...",
                    file=sys.stderr,
                )
                time.sleep(min(2.0, float(attempt)))
    assert last_error is not None
    raise last_error


def run(
    *,
    manifest_path: Path,
    destination: Path,
    check_only: bool,
    timeout: float,
    attempts: int,
) -> None:
    assets = load_manifest(manifest_path)

    if check_only:
        failures: list[str] = []
        for asset in assets:
            error = verify_file(destination / asset.filename, asset)
            if error is None:
                print(f"OK: {asset.filename}")
            else:
                failures.append(f"{asset.filename}: {error}")
        if failures:
            details = "\n  - ".join(failures)
            raise ModelAssetError(
                "required local person-recognition model assets are unavailable "
                f"or invalid:\n  - {details}\n"
                "Run: python scripts/download_person_models.py"
            )
        print(f"All {len(assets)} local person-recognition model assets are valid.")
        return

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelAssetError(f"model destination could not be created: {destination}: {exc}") from exc

    for asset in assets:
        target = destination / asset.filename
        error = verify_file(target, asset)
        if error is None:
            print(f"OK: {asset.filename} (already verified)")
            continue
        print(f"Downloading {asset.filename} ({error})...")
        download_asset(
            asset,
            destination,
            timeout=timeout,
            attempts=attempts,
        )
        print(f"OK: {asset.filename} (downloaded and verified)")

    # Verify the complete set once more so a concurrent modification cannot
    # silently produce a partially valid build.
    final_failures = [
        f"{asset.filename}: {error}"
        for asset in assets
        if (error := verify_file(destination / asset.filename, asset)) is not None
    ]
    if final_failures:
        details = "\n  - ".join(final_failures)
        raise ModelAssetError(f"final model verification failed:\n  - {details}")
    print(f"All {len(assets)} local person-recognition model assets are ready.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and SHA-384 verify pinned local person-recognition models."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing files only; never access the network or write files",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"model source manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_ASSET_DIR,
        help=f"model asset directory (default: {DEFAULT_ASSET_DIR})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="download attempts per file (default: 3)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.attempts <= 0:
        parser.error("--attempts must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(
            manifest_path=args.manifest.resolve(),
            destination=args.destination.resolve(),
            check_only=bool(args.check),
            timeout=float(args.timeout),
            attempts=int(args.attempts),
        )
    except ModelAssetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _disable_frozen_openvino_telemetry() -> None:
    """Write the pinned telemetry package's disabled state before OpenVINO imports.

    OpenVINO imports its conversion helpers from the public package namespace.
    Those helpers initialize telemetry with a no-dialog default, so a fresh
    Windows user profile must receive the disabled state before the first
    ``import openvino`` in the frozen application.  Writing the one-byte state
    directly also avoids the official opt-out command's one-time opt-out event.
    """

    if not getattr(sys, "frozen", False):
        return
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if sys.platform != "win32" or not local_app_data:
        print(
            "ERROR: cannot establish the local-only OpenVINO telemetry setting: "
            "LOCALAPPDATA is unavailable",
            file=sys.stderr,
        )
        raise SystemExit(1)

    directory = Path(local_app_data) / "Intel Corporation"
    consent_file = directory / "openvino_telemetry"
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            prefix=".openvino_telemetry.",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write("0")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, consent_file)
        temporary_path = None
        if consent_file.read_text(encoding="ascii") != "0":
            raise OSError("disabled state could not be verified")
    except (OSError, UnicodeError) as exc:
        print(
            "ERROR: cannot establish the local-only OpenVINO telemetry setting: "
            f"{exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _run_frozen_model_smoke_if_requested() -> None:
    """PyInstaller runtime hook used only by the Windows release builder.

    This validates the files from their *frozen* application path and then asks
    the bundled OpenVINO Runtime to read and compile both networks. Normal app
    launches do no work here because the build-only environment flag is absent.
    """

    smoke_nonce = os.environ.get("IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS", "").strip()
    if not smoke_nonce:
        return
    if not re.fullmatch(r"[0-9a-f]{32}", smoke_nonce):
        print(
            "ERROR: frozen local person-recognition smoke received an invalid build nonce",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        # The first PyInstaller runtime hook must already have staged and
        # preloaded the sealed native DLLs.  Re-enter the same helper here and
        # prove that Windows reports the loaded openvino.dll from that exact
        # ASCII-only cache before importing any application classifier code.
        from app.openvino_native_bootstrap import (
            assert_loaded_openvino_from_cache,
            load_openvino,
            prepare_openvino_native_runtime,
        )

        cache_libs = prepare_openvino_native_runtime().resolve()
        hook_cache = Path(
            str(getattr(sys, "_igac_openvino_native_cache_libs", ""))
        ).resolve()
        if hook_cache != cache_libs:
            raise ModelAssetError(
                "frozen OpenVINO smoke did not inherit the earliest runtime-hook "
                "ASCII cache"
            )
        raw_frozen_root = getattr(sys, "_MEIPASS", None)
        if not raw_frozen_root:
            raise ModelAssetError("frozen OpenVINO smoke has no sys._MEIPASS")
        source_openvino_dll = (
            Path(raw_frozen_root).resolve()
            / "openvino"
            / "libs"
            / "openvino.dll"
        )
        hook_source = Path(
            str(getattr(sys, "_igac_openvino_native_source_dll", ""))
        ).resolve()
        if hook_source != source_openvino_dll:
            raise ModelAssetError(
                "frozen OpenVINO smoke did not inherit the packaged native "
                "source recorded by the earliest runtime hook"
            )
        require_non_ascii_source = os.environ.get(
            "IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE", ""
        ).strip()
        if require_non_ascii_source not in {"", "0", "1"}:
            raise ModelAssetError(
                "frozen OpenVINO smoke received an invalid non-ASCII source flag"
            )
        source_is_non_ascii = any(
            ord(character) > 0x7F for character in str(source_openvino_dll)
        )
        if require_non_ascii_source == "1" and not source_is_non_ascii:
            raise ModelAssetError(
                "Unicode-path smoke did not execute the packaged openvino.dll "
                f"from a non-ASCII source path: {source_openvino_dll}"
            )
        ov = load_openvino()
        loaded_openvino_dll = assert_loaded_openvino_from_cache(cache_libs).resolve()
        try:
            str(loaded_openvino_dll).encode("ascii")
        except UnicodeEncodeError as exc:
            raise ModelAssetError(
                "loaded openvino.dll path is not ASCII-only: "
                f"{loaded_openvino_dll}"
            ) from exc

        from app import person_recognition
        from app.person_recognition import (
            LocalOpenVinoPersonClassifier,
            exercise_local_openvino_gender_branch,
        )
        import numpy  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        asset_directory = (
            Path(person_recognition.__file__).resolve().parent
            / "assets"
            / "person_classifier"
        )
        manifest_path = asset_directory / "MODEL_SOURCES.json"
        assets = load_manifest(manifest_path)
        failures = [
            f"{asset.filename}: {error}"
            for asset in assets
            if (error := verify_file(asset_directory / asset.filename, asset))
            is not None
        ]
        if failures:
            details = "\n  - ".join(failures)
            raise ModelAssetError(f"frozen model verification failed:\n  - {details}")

        core = ov.Core()
        compiled_gender: Any | None = None
        for filename in (
            "face-detection-retail-0004.xml",
            "age-gender-recognition-retail-0013.xml",
            "person-detection-retail-0013.xml",
            "person-attributes-recognition-crossroad-0230.xml",
        ):
            model = person_recognition.read_openvino_ir_model_from_memory(
                core,
                asset_directory / filename,
            )
            compiled = core.compile_model(model, "CPU")
            if filename == "age-gender-recognition-retail-0013.xml":
                compiled_gender = compiled
            input_shape = tuple(int(value) for value in compiled.input(0).shape)
            inference = compiled([numpy.zeros(input_shape, dtype=numpy.float32)])
            if not inference:
                raise ModelAssetError(
                    f"frozen OpenVINO inference returned no outputs: {filename}"
                )

        if compiled_gender is None:
            raise ModelAssetError("frozen OpenVINO gender model was not compiled")
        exercise_local_openvino_gender_branch(
            compiled_gender,
            face_model_path=asset_directory / "face-detection-retail-0004.xml",
            gender_model_path=asset_directory
            / "age-gender-recognition-retail-0013.xml",
        )

        # Exercise the real application path as well as direct tensors.  A tiny
        # valid PNG is expected to contain no reliable face, but it still covers
        # frozen Pillow loading, image decoding, preprocessing, model integrity,
        # face-output parsing, and the classifier's conservative result path.
        sample = io.BytesIO()
        Image.new("RGB", (96, 96), (127, 127, 127)).save(sample, format="PNG")
        classification = LocalOpenVinoPersonClassifier().classify(sample.getvalue())
        if (
            not classification.checked
            or classification.category not in {"male", "female", "couple", "unknown"}
            or classification.reason in {
                "model_missing",
                "model_integrity_failed",
                "local_inference_failed",
            }
        ):
            raise ModelAssetError(
                "frozen application classifier smoke failed: "
                f"category={classification.category}; checked={classification.checked}; "
                f"reason={classification.reason}"
            )
    except Exception as exc:
        print(
            "ERROR: frozen local person-recognition smoke failed: "
            + _exception_summary(exc),
            file=sys.stderr,
        )
        _print_frozen_openvino_native_diagnostics()
        raise SystemExit(1) from exc

    runtime_version = str(getattr(ov, "__version__", "unknown"))
    version_match = re.match(r"^(\d+\.\d+\.\d+)", runtime_version)
    base_version = version_match.group(1) if version_match is not None else "unknown"
    if base_version != "2025.4.1":
        print(
            "ERROR: frozen local person-recognition smoke loaded unexpected "
            f"OpenVINO version {runtime_version!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(
        f"IGAC_FROZEN_OPENVINO_ASCII_OK:{smoke_nonce}:"
        f"openvino_dll={loaded_openvino_dll}"
    )
    print(
        f"IGAC_FROZEN_OPENVINO_SOURCE_B64_OK:{smoke_nonce}:"
        "source_openvino_dll_utf8_b64="
        + base64.b64encode(str(source_openvino_dll).encode("utf-8")).decode("ascii")
    )
    print(f"IGAC_FROZEN_OPENVINO_OK:{smoke_nonce}:openvino={base_version}")
    print(
        "Frozen local person-recognition smoke passed: eight model payloads verified; "
        "four OpenVINO CPU models compiled and inferred; production gender outputs "
        "parsed; real classifier path executed."
    )
    raise SystemExit(0)


def _print_frozen_openvino_native_diagnostics() -> None:
    """Explain which packaged native dependency fails after a real import error."""

    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        import ctypes

        native_root = Path(getattr(sys, "_MEIPASS", "")).resolve()
        openvino_package = native_root / "openvino"
        openvino_libs = openvino_package / "libs"
        pyopenvino_modules = sorted(openvino_package.glob("_pyopenvino*.pyd"))
        candidates = [
            native_root / "MSVCP140.dll",
            native_root / "VCRUNTIME140.dll",
            native_root / "VCRUNTIME140_1.dll",
            openvino_libs / "tbb12.dll",
            openvino_libs / "openvino.dll",
            openvino_libs / "openvino_ir_frontend.dll",
            openvino_libs / "openvino_intel_cpu_plugin.dll",
            *pyopenvino_modules,
        ]
        print(
            "Frozen native diagnostics: "
            f"python={sys.version.split()[0]}; pointer_bits={struct.calcsize('P') * 8}; "
            f"executable={sys.executable}; _MEIPASS={native_root}; "
            f"dll_search={native_root};{openvino_libs}",
            file=sys.stderr,
        )
        if not pyopenvino_modules:
            print("  FAIL openvino/_pyopenvino*.pyd: missing", file=sys.stderr)
        for candidate in candidates:
            try:
                size = candidate.stat().st_size
            except OSError as stat_error:
                print(f"  FAIL {candidate}: {stat_error}", file=sys.stderr)
                continue
            try:
                ctypes.WinDLL(str(candidate))  # type: ignore[attr-defined]
            except OSError as load_error:
                print(
                    f"  FAIL {candidate} ({size} bytes): "
                    f"winerror={getattr(load_error, 'winerror', None)}; {load_error}",
                    file=sys.stderr,
                )
            else:
                print(f"  OK   {candidate} ({size} bytes)", file=sys.stderr)
    except Exception as diagnostic_error:
        print(
            "  FAIL diagnostic probe: "
            f"{type(diagnostic_error).__name__}: {diagnostic_error}",
            file=sys.stderr,
        )


_disable_frozen_openvino_telemetry()
_run_frozen_model_smoke_if_requested()


# A PyInstaller runtime hook may execute with ``__name__ == "__main__"``.
# Never start this downloader's CLI from inside collector_core.exe; the hook
# above is the only frozen behavior and the real backend entry point runs next.
if __name__ == "__main__" and not getattr(sys, "frozen", False):
    raise SystemExit(main())
