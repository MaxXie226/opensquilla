# Public UI package release policy

The public UI release contains four independently installable packages:
`@opensquilla/client-sdk`, `@opensquilla/ui-tokens`,
`@opensquilla/ui-primitives`, and `@opensquilla/ui-foundation`.
The SDK has an independent version. The three UI packages use one fixed
Foundation release version.

## Compatibility

- Pull requests regenerate `contracts/ui-foundation/v1/api-report.json` and
  compare it with the target branch.
- Additive API changes require a minor version. Breaking changes require a
  major version after 1.0, or a minor version while the package is pre-1.0.
- Export removal requires a recorded deprecation whose `removeAfter` version
  has been reached.
- `packages/ui-compatibility-matrix.json` records the current and previous
  supported minor. The first 0.1.0 release is explicitly marked as bootstrap;
  later releases must provide N-1 assets and pass both cross-version
  Foundation/SDK combinations.
- UI Foundation declares the supported independent Client SDK range in the
  matrix and its package dependency; both N and N-1 SDK versions must satisfy
  that range.
- Gateway protocol ranges without an overlap fail. Missing optional
  capabilities disable the affected feature; missing required capabilities
  fail with an upgrade action.

## Release

The `Public UI Foundation Release` workflow accepts
`ui-foundation-v<version>` tags or a manual dry run. It builds each tarball
twice and requires identical SHA-256 digests, installs the packages into an
external fixture, and builds the complete public WebUI from the tarballs.

The GitHub Release contains:

- four versioned npm tarballs;
- the API and compatibility reports;
- combined changelogs;
- SHA-256 checksums;
- an SPDX 2.3 SBOM;
- an in-toto/SLSA-format provenance statement;
- a machine-readable release manifest.

Release assets are write-once. The workflow never uses `--clobber`; a failed
or incorrect release is replaced by a new patch version. Rolling back means
pinning the previous manifest and tarball digests. It does not overwrite an
existing version.

Public pull-request CI uses no registry token and never reads a private
repository. A private product may pin these public tarballs by exact version
and digest, then upgrade them on its own schedule.
