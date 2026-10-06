# IG Audience Collector local Core — v3.0.4

This directory contains the loopback Python service used by the Windows desktop
application. It persists users, task checkpoints, results, review decisions,
deduplication and action attempts. The desktop manages authenticated access and
browser ownership. Uncertain external action outcomes remain unknown and are
not automatically submitted again.

## Supported execution

Use the project Windows build and desktop launcher described in
[the build guide](../docs/PUBLIC_BUILD.md). The intended packaged application
requires installed Google Chrome. Native, frozen and installed verification must
run against the actual target environment; importing a Python module or passing
a synthetic test does not prove the packaged service works.

For isolated backend development, install the development dependencies from this
directory into a dedicated virtual environment. A directly started service must
receive a fresh high-entropy `IGAC_STARTUP_TOKEN`; every request, including
health checks, needs `X-Startup-Token`. Authenticated routes also require the
application's session token. Never commit tokens, local configuration or user data.
The server rejects non-loopback bind addresses.

## Tests

From the project root, run `python scripts/run_backend_tests.py` with the supported
environment and installed dependencies. Tests must use their isolated fixtures.
The full Windows builder adds mandatory real-browser, native, packaged-runtime
and installed gates; do not interpret the backend suite as those separate stages.

Cloud is off until explicitly configured with the user's own project. Pexels and
other credential-based integrations require valid user configuration. No project
endpoint, service credential or user database is supplied by this repository.
