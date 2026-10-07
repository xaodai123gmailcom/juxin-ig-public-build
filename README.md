# Juxin IG desktop v3.0.4

Windows desktop source for 聚鑫国际, an Instagram account workspace with collection,
review, reporting, account nurture and account management. The desktop
shell is Electron; the local Core is Python. Product version: **3.0.4**. Source
revision: **stability-r94**. Feature revision: **2026.10.05-r6.4-ig**.

This repository contains source and verification tools. It does not contain a
prebuilt installer or evidence that a particular Windows build has passed.

## Build and verification

Use the public Windows workflow in `.github/workflows/public-windows-verify.yml`.
It builds the actual flat Git checkout and binds its source manifest to the real
GitHub Actions commit and repository identity. See [Windows build](docs/PUBLIC_BUILD.md)
and [source identity](docs/CI_SOURCE_IDENTITY.md). The intended Windows package
requires **Google Chrome installed on both the build runner and target computer**.

Local source checks and synthetic tests are useful diagnostics. They do not
replace Windows native UI, frozen Core, installed application or installer digest
verification. Do not invent CI environment variables to make local checks claim
GitHub Actions evidence. [Verification scope](RELEASE_VERIFICATION.md) explains
the acceptance boundary.

## Optional integrations

- Cloud backup is off by default. To use it, create and configure your own
  Supabase project with `cloud/supabase-init.sql`, enter its HTTPS URL and
  publishable/anon key in the cloud workspace, then explicitly enable it and sign
  in. Enabling and signing in uploads supported collection, nurture and account
  workspace records to that project. Legacy posting materials and retirement
  archives stay local and are excluded from this backup. Never enter a `service_role` or secret key.
- Immersive Translate is optional. Its proprietary userscript is not included.
  Obtain the supported original version from an authorized vendor distribution,
  import the local file through translation settings, then explicitly enable
  translation and select your provider. The exact accepted version/hash and
  privacy boundary are in the [integration notice](desktop/vendor/immersive-translate/NOTICE.txt).
  Vendor accounts, terms and fees remain the user's choice.

## Operating boundaries

Posting and Pexels are removed. Legacy captions, materials and history are
verified in a recoverable local archive before safely idle associations leave
active storage; active or uncertain owners remain fenced. Window leases and
authoritative close confirmation protect concurrent tasks. See
[removal acceptance](docs/POSTING_REMOVAL_ACCEPTANCE.md). Account credentials, browser sessions, databases and generated evidence
belong outside the public repository.

- [中文说明](README_CN.md)
- [Acceptance checklist](docs/ACCEPTANCE_CHECKLIST.md)
- [Recovery policy](RECOVERY_POLICY.md)
- [Storage policy](STORAGE_POLICY.md)
- [Database migration behavior](docs/PURE_IG_DATABASE_MIGRATION_R5.md)
- [Current package identity](NEWGEN_RELEASE_STATUS.md)

Publication of this source does not itself grant third-party software rights or
state a project license. Review the applicable rights before redistribution.
