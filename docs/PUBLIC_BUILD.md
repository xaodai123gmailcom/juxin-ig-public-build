# Windows build and evidence scope — v3.0.4

## Supported build environment

- Windows x64 and 64-bit PowerShell
- Standard CPython 3.11–3.14 x64, with Python available on PATH; ARM64,
  32-bit and free-threaded Python are rejected by the installer script
- Node.js Windows x64 with TypeScript type stripping enabled: 22.18+ or 24+
- Google Chrome installed on the runner and each target computer
- Git and sufficient disk space for dependencies, models, native fixtures,
  frozen Core, installer and diagnostic output

Use the pinned lockfiles and packaging requirements. Dependency installation,
model acquisition and native/runtime verification are performed by the Windows
build scripts; a copied dependency directory is not acceptance evidence.

## Public verification route

The entry is `.github/workflows/public-windows-verify.yml` in
`xaodai123gmailcom/juxin-ig-public-build`. Review the workflow from the exact commit
being built. The public source verifier requires the real checked-out HEAD,
repository identity, tracked bytes and Actions-provided context to agree.

The public workflow uploads bounded verification summaries. It does not publish
the installer, raw logs, screenshots, user data or credentials as public artifacts.
Installer delivery requires its own review of the actual final artifact.

The installed-Chrome build entry is `BUILD_WITH_INSTALLED_CHROME.bat`, which
invokes `scripts/build_windows.ps1 -BrowserMode installed-chrome`. The workflow
uses the full Windows build path and retains mandatory browser, source, backend,
renderer, native UI, frozen runtime and packaging gates. Portable output is a
separate artifact and cannot substitute for an NSIS installer.

Required public source receipts intentionally fail outside their actual Actions
context. Local source diagnostics do not create a public release receipt; do not
alter markers, unset CI identity or supply invented commit/repository values to
make a required gate pass. Changing tracked source invalidates the current seal
and requires a reviewed new manifest/commit.

## Installer identity and result

Expected setup: `Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe`.
Expected sidecar: `Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe.sha256`.

Require the exact final Setup bytes, SHA-256 sidecar and
`installer-output/LATEST_SUCCESS.txt` to agree. Native and installed API reports
must identify the same source and installer. Missing evidence, a skipped native
stage, failure or a report for another installer prevents acceptance.

The source tree does not assert any run ID, final installer hash, installed pass
or live Instagram success. Read the actual same-commit run and artifacts for those
results. Synthetic fixtures must remain isolated from user data and live accounts.

## Service configuration

Cloud is disabled until the user supplies and enables their own project and
signs in. Translation requires a separately obtained, locally imported compatible
userscript. Pexels requires a separately configured valid API credential.
Unconfigured services must stay visibly unavailable. Configuration or proprietary
scripts must never be added to the tracked build source.
