# Public Windows verification

This workflow verifies a real, exact GitHub source checkout in the designated public repository. It runs six early native fixtures and all 36 required early backend groups, preserves their runner-local evidence, checks out the same commit into a fresh tree, and invokes the original full Windows build with `-BrowserMode installed-chrome`. No release gate is replaced by a mocked test.

The separate installed stage silently installs the newly built NSIS product into a fresh runner-local destination. It verifies installed native resources and inference, actual frozen Core behavior, scale and upgrade invariants, and the actual installed desktop's authenticated recovery API. Independent receipt validators check source identity, runtime identity, case coverage, screenshot hashes, installed EXE/app.asar hashes, prior upgrade evidence, and retained early receipts. All raw evidence, screenshots, model files, dependencies, profiles, source copies, logs, and binaries remain on the runner.

Only three newly constructed, size-bounded JSON summaries can be uploaded: `run-summary.json`, `source-build-proof.json`, and `installed-acceptance-proof.json`. Each has the real run identity and the current schema-2 public source identity. Passed build/installed stages include fresh binary hashes; interrupted, failed, and not-run stages retain those distinct statuses. Failure diagnostics contain fixed gate names, process outcomes and exit codes, existing ASCII test symbols, and sealed relative source locations only. Diagnostic parsing cannot make a failed gate pass. The three summaries are evidence, not downloadable installers.

Before dependency imports, raw ASCII `0` is written and read back at the actual Windows Local AppData telemetry file, whose location is verified through the Windows shell API. Every child gate rechecks it. The wrapper preserves the profile environment, canonicalizes only TEMP/TMP, removes credential variables from build children, disables pip configuration-file loading, explicitly selects official PyPI and npm registries, and gives npm empty per-job user/global configuration. Existing Microsoft-signed VC runtime and Google-signed installed Chrome are checked before the build; missing prerequisites fail closed. The workflow does not install or repair system prerequisites, request elevation, or change execution policy or security settings.

## Contract-suite coverage mapping

The historical seven bootstrap suites are accounted for as follows:

- `test-r63-native-proof.py`: retained with its unchanged strict native oracle; rejects incomplete scenarios, missing observations, non-Windows identity, stale source/compiled host hashes, external activity, and invalid painted screenshots
- `test-r63-upgrade-proof.py`: retained with its unchanged strict upgrade oracle and baseline contract; its example is an explicitly synthetic, non-executable fixture. It rejects missing cases, wrong frozen executable, invalid chronology, history loss, and missing admission evidence
- `test-r64-crop-proof.py`: retained with its unchanged strict crop oracle; only repository-relative orchestration paths and the final validation boundary were adapted. It still rejects missing or reordered cases, wrong click trust/counts, stale source/probe hashes, and altered screenshots
- `test-r64-recovery-ui-proof.py`: retained with its unchanged strict native recovery oracle; keeps every top-level field, scenario, production source, target/revision, timing, input-trust, and screenshot rejection check
- `test-r62-release-contract.py`: orchestration coverage moved into `test_public_ci.py`. The replacement checks exact six-fixture order, all 36 early groups, real-browser requirements, failure aggregation, narrower final-seed timing, fresh same-commit checkout order, single NSIS/sidecar identity, retained early hashes/equality, all 40 explicit late groups, and existing per-case/teardown watchdogs. The retired standalone diagnostic workflow is not presented as required-release acceptance
- `test-r64-release-contract.py`: orchestration coverage moved into `test_public_ci.py`. The replacement keeps the actual installed probe after prior upgrade/scale proof, its 120-second inner and 180-second owned-process bound, native-after-host-compile order, registered renderer/native tests, and every independent validator. Historical PID-based outer cleanup was replaced by the existing private Windows Job Object supervisor
- `test-private-output.py`: private publication and fallback-branch transport are retired entirely. Replacement public-export negative tests reject credential forwarding, non-pinned/write-capable workflow actions, wildcard/binary/log uploads, stale stage/source/binary identity, false success after gate failure, non-allowlisted diagnostics, and oversized diagnostic exports. There is no public release, branch publication, or executable artifact path

`test_public_ci.py` and the four retained suites are static or mocked contract checks. Running them locally proves only their assertions, not Windows execution or product acceptance. The workflow runs actual product gates only on the hosted Windows runner.

## Official action pins

The immutable commits were resolved from official release pages and their linked commit pages:

- [actions/checkout v4.2.2](https://github.com/actions/checkout/commit/11bd71901bbe5b1630ceea73d27597364c9af683)
- [actions/setup-python v5.6.0](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065)
- [actions/setup-node v4.4.0](https://github.com/actions/setup-node/commit/49933ea5288caeca8642d1e84afbd3f7d6820020)
- [actions/upload-artifact v4.6.2](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02)
