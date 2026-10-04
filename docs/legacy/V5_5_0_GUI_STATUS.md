# v5.5.0 consistent connection and market-data status

Version 5.5.0 changes GUI presentation only. It separates connection health, market-data freshness and trading eligibility without changing the strategy or any trading safeguard. The layout, subscriptions, broker callbacks, order handling, RTH and ATR checks, database writes, backup policy and timer intervals are retained from 5.4.0.

## Status ribbon

| Box | Meaning and presentation |
|---|---|
| Connection | Reports the local API and Gateway/TWS-to-IBKR links. With both available and reconciliation complete, **Connected** remains the label even while prices are stale or an actual update is awaited. Connection faults, reconciliation and worker/storage faults retain their existing warnings. The tooltip no longer describes an earlier fresh event as data arriving now. |
| Data | Separates the reported subscription type from the age of the last actual update. **Live / Stale 6.3h**, for example, describes a live subscription whose most recent actual update is old. A known old update receives the same stale presentation whether or not another update is required after a recovery/farm notification. That waiting reason remains in the tooltip. No observed update is not presented as a known stale price, and delayed/frozen modes remain identified. |
| Trading | For an otherwise normal BUY/SELL wait whose only blockers are closed RTH and stale/pending market data, the summary reads **BUY blocked: RTH closed** or **SELL blocked: RTH closed**. All blockers remain in the tooltip. Other risk, recovery, connectivity or worker/storage faults retain their priority. |

Thus two connected instances outside RTH with old market data present the same status categories, despite different ordering of their last tick and a data-farm notification. Each keeps its own measured age and contract-specific RTH countdown. This does not make the cached prices tradeable or remove the requirement for a new event where the controller requires one.

## Price data monitor

The source line uses **Last market update** for the recorded receipt time of an actual market-data event. It does not use the timestamp of a cached snapshot read as a substitute. Missing update timestamps remain unavailable.

**Cached snapshot checked: … ago** separately describes how recently the app read the snapshot. A recent check can coexist with an old last market update. The actual-update timestamp is not a claim about the exchange's execution time or the independent freshness of every displayed bid, ask and Last field.

When a displayed price is stale or invalidated, its diagnostic status says **Cached price — market data stale** or **Cached price — awaiting a new market update**, as applicable. The legacy feed summary follows the same freshness presentation. Prices remain visible for diagnosis; the controller's existing field-level and subscription-generation checks still determine whether they can be used.

## Scope and upgrade

Only `app/gui.py` changes runtime behavior, and those changes affect presentation. No controller, broker adapter, strategy, storage, model, ATR or dependency change is included. Existing settings and databases remain compatible; 5.4.0's schema stamp, validated 20-file backup retention and 24-hour ATR seed age limit are unchanged.

1. Close the instance before replacing files. Preserve its database, settings, captures and backups.
2. Use `IBKR_Trading_Bot_5.5.0_Source.zip` or the `IBKR_Trading_Bot_5.5.0_Release_files.zip` overlay according to its included baseline instructions. Keep each portable instance's own directory, database and client ID.
3. Run `run_all_tests.bat` in the target Windows environment. Rebuild an existing packaged executable from the updated source; replacing Python files does not update that executable. Standard GIL-enabled CPython 3.14.x remains required.

## Verification boundaries

`tests/test_v550_gui_status.py` exercises the status mapping and timestamp/cached-price presentation, including equivalent old-data states, retained waiting diagnostics, normal closed-RTH precedence and other fault priority. Existing GUI, controller, adapter and safety tests remain relevant to compatibility.

The root [implementation and test report](V5_5_0_IMPLEMENTATION_TEST_REPORT.txt) records actual results, source-scope checks and any unavailable gates. Test definitions alone do not establish that native Windows/Qt, Python 3.14, quality tools or live-broker checks ran. The [manual test plan](../TEST_PLAN.md#connection-data-and-trading-presentation) covers native display checks and the transition to a new market-data update.

The preceding [5.4.0 reliability note](V5_4_0_RELIABILITY.md) and [verification report](V5_4_0_IMPLEMENTATION_TEST_REPORT.txt) remain available as history.
