# Release test parity and historical Claude hook migration

Release run 34538578659 for v0.36.1 failed the existing Stop-hook migration
regression: `test_r1_2_rerun_migrates_old_broken_entry_preserving_siblings`.
PR #23 had disclosed the same failure on unchanged main, but the test workflow
only triggered on release tags. Automated PR review did not enforce pytest.

Claude settings can retain an old `kin` executable path. Support historical
locations explicitly: `/opt/homebrew/bin/kin` and `/usr/local/bin/kin`, alongside
bare `kin` and the current executable. Still require the entire known command
template to match. Never discover owned binaries by searching settings for paths
ending in `/kin`. Commands from other locations require the installer's recorded
ownership or explicit retirement. Foreign siblings and their entry metadata
must survive migration.

The `CI` workflow in `.github/workflows/ci.yml` owns the full pytest job with
Python 3.12 and `.[dev,mcp]`. It runs on pull requests and main pushes and supports
manual dispatch and reusable calls. `Publish to PyPI` in `workflow.yml` triggers
only on `v*` tags and invokes that same CI workflow before build and publication.
Keep the test implementation in CI so PR validation cannot drift from release
validation or appear to publish a package.

On 2026-09-10, main had no branch protection and the repository had no rulesets.
Running the PR check does not itself make passing tests mandatory for merging;
that requires making `test` a required check in repository settings.

The next prepared package version is `0.36.2`. Keep pyproject, runtime version,
both Claude plugin manifests, MCP registry metadata, server card, public badges,
and changelog aligned. The failed `v0.36.1` tag contains `0.36.0` metadata and
must not be reused or moved.
