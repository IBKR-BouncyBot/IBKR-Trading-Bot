# Production incident replay and historical migration tests

This replay layer converts selected real BouncyBot incidents into small, deterministic, privacy-reduced regression fixtures. The fixtures and sanitizer do not affect live trading. They do not include the original audit ZIPs, SQLite databases, Gateway logs, account identifiers, broker order identifiers, permanent identifiers, execution identifiers, usernames, or machine-local paths.

## Included incident regressions

The committed fixtures cover six observed incidents:

1. **IREN invalid trailing-BUY price**
   - preserves `ContractDetails.minTick=0.0001`, SMART market rule 557, the applicable one-cent price band, the malformed historical what-if response, and the broker's invalid-price rejection;
   - verifies market-rule normalization, BUY-up rounding, strict what-if validation, broker-error retention, and the structural-rejection circuit breaker.

2. **NBIS partial-fill cancellation race**
   - preserves two 28-share BUY executions, the cancellation between them, and late commissions;
   - verifies that Stage 2 remains active until the original BUY is terminal, all 56 shares are reconciled, commissions are idempotent, and the app-owned unsold quantity is correct.

3. **Cross-instance Master-client callbacks**
   - preserves foreign NBIS/LAC commission and order-error callback shapes while the local cycle is IREN;
   - verifies that unmatched complete `OrderRef` values remain unowned and cannot change the local cycle, status, execution ledger, commissions, or decision stream.

4. **VWRA delayed market data**
   - preserves market-data type 3, recently received delayed quotes, and an otherwise eligible BUY setup;
   - verifies that live trading is blocked before price normalization, an order intent, broker order, or execution is created; repeated unchanged warnings are bounded, and the cycle records `PreflightBlocked`.

5. **VWRA Stage-3 market-rule rounding**
   - preserves a selected price above the theoretical trigger while the broker-valid SELL stop still fails the rounded minimum-profit stop;
   - verifies that the controller remains in Stage 3 until the normalized SELL stop protects the normalized minimum-profit boundary.

6. **VWRA LSE continuous-close mismatch**
   - preserves IBKR `liquidHours` ending at 16:50 London time and the independently identified 16:30 continuous-session close;
   - verifies that `LSE`/`LSEETF` timing-sensitive actions use the earlier 16:30 continuous-session boundary while retaining the raw IBKR boundary for diagnostics.

## Resolved incident regressions in v3.2.1

The three former strict expected-failure sentinels are ordinary passing regressions in v3.2.1:

- timing-sensitive `LSE` and `LSEETF` actions use the earlier verified 08:00-16:30 `Europe/London` continuous-session boundary when IBKR `liquidHours` extends later;
- unchanged delayed-data BUY preflight warnings use a stable cycle-and-blocker key and are limited to one audit event per 60 seconds while the guard remains enforced on every evaluation; and
- a local BUY block before broker submission records `PreflightBlocked`, while `SubmitFailed` remains reserved for an actual submission attempt that fails before acceptance can be confirmed.

The focused tests also preserve the raw IBKR boundary for diagnostics, verify that an earlier IBKR holiday/early-close boundary still wins, close cached RTH state at the effective boundary, and confirm that no order intent is written for a preflight block.

## Fixture provenance and privacy

Each JSON fixture contains:

- a schema version;
- a stable incident identifier;
- a sanitized marker;
- SHA-256 fingerprints of the private source evidence;
- only the contract, price, quantity, status, and event fields required for the regression.

`tests/test_incident_fixture_integrity.py` verifies the fixture envelope, manifest, byte hashes, privacy rules, inventory, and exclusion of raw databases, logs, and ZIP files.

`scripts/sanitize_audit_bundle.py` provides a deterministic intermediate sanitizer for future audit bundles. It applies ZIP path and uncompressed-size checks, aliases identifiers, removes account/path fields, and emits sorted JSON. Its output is an intermediate review artifact; committed incident fixtures remain manually minimized and asserted.

## Historical SQLite fixtures

The migration corpus contains synthetic SQL snapshots generated with the v3.0.19, v3.1.1, and v3.1.2 storage implementations. The fixtures use only the synthetic ticker `SYNTH`.

The current storage layer must:

- open each historical schema;
- preserve cycles, orders, executions, commissions, events, settings, and active state;
- add current additive columns and indexes;
- infer the database currency safely;
- produce the same schema and row counts when migration is run twice.

## Expanded safety mutations

The mutation smoke gate now contains 17 deliberate defects. In addition to the original strategy, sizing, P/L, and app-owned-position probes, it checks:

- BUY price rounding remains upward;
- SELL price rounding remains downward;
- exact market-rule low-edge boundaries select the new band;
- what-if trailing orders keep `transmit=True`;
- the one-currency database lock rejects a different currency;
- automatic reconnect remains ten seconds;
- execution IDs remain idempotency keys;
- a late zero commission cannot erase a known non-zero commission;
- the LSE continuous-session close remains 16:30 London;
- a local BUY preflight guard remains `PreflightBlocked`; and
- the first throttled warning is emitted even when the monotonic clock begins at zero.

Each mutant is applied only in a temporary copy of `app/`. Production source files are never edited by the mutation runner.

## Commands

Run the incident and migration layer directly:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_audit_bundle_sanitizer.py `
  tests\test_incident_fixture_integrity.py `
  tests\test_historical_database_fixture_corpus.py `
  tests\test_production_incident_replays.py `
  tests\test_v321_incident_gap_fixes.py `
  -q -rx
```

Run the complete repository gate normally:

```powershell
.\run_all_tests.bat
```

The new pytest files are collected automatically, and `scripts/run_mutation_smoke.py` automatically executes all 14 mutants.

## Limits

These fixtures preserve real incident structure but do not replay the IBKR network protocol byte-for-byte and do not prove future exchange or Gateway behavior. They complement, rather than replace:

- a clean Windows packaged-executable test;
- representative IBKR paper-account tests;
- live market-data entitlement checks;
- independent exchange-calendar maintenance;
- operator review of actual account positions and executions.
