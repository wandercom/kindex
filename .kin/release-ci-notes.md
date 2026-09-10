# Release test parity and historical Claude hook migration

Release run 34538578659 for v0.36.1 failed the existing Stop-hook migration
regression: `test_r1_2_rerun_migrates_old_broken_entry_preserving_siblings`.
PR #23 had disclosed the same failure on unchanged main, but the test workflow
only triggered on release tags. Automated PR review did not enforce pytest.

Claude settings can retain a different absolute `kin` executable path after
moving installations or machines. Historical ownership recognition must account
for that path while still matching the entire known command template. A path
or a Kindex-looking substring alone is not permission to remove a handler.
Foreign siblings and their entry metadata must survive migration.

The same workflow test job now runs full pytest with Python 3.12 and
`.[dev,mcp]` on pull requests, main pushes, and release tags. Only tag pushes
can build, and publication depends on the tested build. Keep these events on
one job so PR validation cannot drift from the release gate.

On 2026-09-10, main had no branch protection and the repository had no rulesets.
Running the PR check does not itself make passing tests mandatory for merging;
that requires making `test` a required check in repository settings.
