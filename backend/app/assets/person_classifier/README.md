# Local person-classification model assets

The application uses the following Intel Open Model Zoo 2023.0 FP16 models
through the local OpenVINO Runtime:

- `face-detection-retail-0004` detects all usable faces in an avatar.
- `age-gender-recognition-retail-0013` classifies each detected adult face as
  female or male.
- `person-detection-retail-0013` detects sufficiently visible upright people
  when no reliable adult face can be used.
- `person-attributes-recognition-crossroad-0230` supplies the local body-
  appearance fallback for those people.

Inference runs on the user's computer. It does not call GPT or a remote
inference service. The age/gender model was not trained for children; low
confidence, unusable, or otherwise unsupported images must remain `unknown`.

The complete Windows/source package carries the eight verified model XML and BIN
files so local recognition works immediately after installation.
The release pins `openvino==2025.4.1`, the runtime version used for the real
CPU inference verification of these assets.
`MODEL_SOURCES.json` pins the official recovery URL, byte size, and SHA-384
digest for every required file. Verify them, or restore a missing/corrupt file,
with:

```console
python scripts/download_person_models.py
python scripts/download_person_models.py --check
```

The normal command only verifies files that are already valid and does not
download them again. A missing or corrupt file is recovered from Intel's
official host, written to a temporary file, fully verified, and then moved into
place atomically. `--check` never accesses the network or writes files and
returns a non-zero exit status if any asset is missing, truncated, or has the
wrong digest.

Windows release builds repeat the checksum check immediately before PyInstaller
packaging. The frozen `collector_core.exe` is then started in a build-only smoke
mode that verifies its packaged copies and compiles all four networks on the CPU
through the bundled OpenVINO Runtime. The smoke then executes a zero-input
inference through each compiled network, so a missing plugin or native runtime
dependency fails the build instead of failing silently on an end user's PC.

The models and their metadata are provided by Intel's Open Model Zoo under the
Apache License 2.0. See `LICENSE` in this directory. The application itself may
be distributed under separate terms.
