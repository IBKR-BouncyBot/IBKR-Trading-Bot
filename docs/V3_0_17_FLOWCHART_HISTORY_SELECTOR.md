# v3.0.17 Flowchart history selector and complete Windows test run

## Summary

The **Flowchart data** selector on the Strategy flowchart tab is now available in **Simple**, **Advanced**, and **Debug** modes.

Previously, entering Simple mode hid the history combo box even though completed-cycle data remained loaded. That made it impossible to switch from the active strategy to a previous trade without first changing the global GUI mode.

The Windows `run_all_tests.bat` path also now runs every collected pytest test in one Coverage.py invocation. It no longer excludes `soak` tests from the first pass and runs them again separately, so the full Windows gate has no pytest marker-based deselection.

## Behavior

- The selector always offers **Current strategy / active cycle** plus the completed cycles supplied by Trade history.
- A selected previous trade remains selected while a live strategy continues receiving snapshots.
- Live snapshots continue updating the panel's current-cycle cache in the background, but they do not force the selector back to the active cycle.
- Returning the selector to **Current strategy / active cycle** immediately displays the latest cached live state.
- Simple mode still hides the explanatory paragraph and other diagnostics; it no longer hides the data-source selector.
- The separate flowchart **View** selector continues to control which stage cards are shown.

## Test runner behavior

- `run_all_tests.bat` still performs compilation, statement/branch coverage, the callable-entry gate, mutation smoke tests, deterministic CSV simulations, Ruff, and Pyright.
- Its pytest stage has no `-m "not soak"` or `-m "soak"` selector.
- Bounded soak tests therefore receive the same `ResourceWarning` and Coverage.py checks as the rest of the pytest suite.
- The `soak` marker remains defined so maintainers can run only that subset manually when diagnosing a high-volume test.
- The Unix helper `scripts/run_tests.sh` retains its separate coverage/soak split; this release changes the requested Windows `run_all_tests.bat` path.

## Documentation-only public repository preparation

A follow-up documentation pass remains within v3.0.17 and makes no application-runtime change. It:

- audits current guides against the source and test scripts;
- moves v3.0.16 and earlier release-specific notes into `docs/legacy/`;
- adds a current documentation index and an archived-note index;
- corrects the soak-marker description and the location of the persistent readable audit log;
- documents 50-backup rotation, synchronized-folder risks, Git-history review, and sensitive generated artifacts;
- adds `SECURITY.md`; and
- adopts the PolyForm Noncommercial License 1.0.0, with the official license text in the repository root.

The Windows release assembly now includes the license and security documents alongside the README and changelog.

## Safety boundary

The application correction is GUI-only. It does not change strategy evaluation, broker communication, order submission, RTH behavior, ATR behavior, reconciliation, persistence, or Trade-history records. The additional change is confined to development/test scripts and documentation.

## Verification

Focused headless GUI tests cover selector visibility in compact/Simple and non-compact modes, completed-cycle population, persistence of a historical selection while an active cycle updates, and the existing full-strategy card behavior.

Test-infrastructure checks assert that the Windows PowerShell runner contains exactly one pytest invocation, applies no pytest marker filter, and no longer creates a separate soak-test log.

The release-equivalent offline validation completed with:

- 811 pytest cases collected in one unfiltered Coverage.py run;
- 810 passes and one strict documented expected failure;
- no skipped or deselected tests;
- 811/811 executable application callables entered;
- 6/6 safety mutants killed; and
- 58 deterministic CSV scenarios passed across 54 price-path files.

Ruff and Pyright could not be installed in the offline release environment. They remain required by `run_all_tests.bat` through `scripts/run_quality_checks.py --require-tools` and will make the Windows command fail if either tool is unavailable or reports an error.
