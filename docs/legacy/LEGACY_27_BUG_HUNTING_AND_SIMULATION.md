# legacy release bug-hunting and simulation update

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release focuses on reliability under prolonged runtime, deterministic simulated-data coverage, and reverting the Recovery tab back into the main tab strip.

## Bugs and risks found during review

### Protective SELL cancel/replace sequencing

The final profit-protecting SELL trailing-stop could be requested in the same decision window as a protective SELL cancellation. The strategy now requests cancellation first and waits for the protective SELL to no longer be working before it requests the final SELL trail. This avoids two active SELL exits for the same app-managed position.

### Protective SELL recovery after offline fills

A protective SELL can fill while the app is closed or disconnected. Recovery now checks app-owned open orders, recent executions, and order polling for the protective SELL reference before deciding whether the cycle can be completed or must enter manual review.

### Duplicate price snapshot recording during ticker confirmation

Ticker confirmation recorded the same first-price snapshot twice in one path. This inflated the API read counters and graph buffer faster than real updates. The duplicate call was removed.

### Logging could fail on missing cycle rows

A warning event that referenced a cycle ID not yet present in SQLite could raise a foreign-key error. Logging now keeps the warning but drops the dangling cycle reference instead of letting an audit event crash the app.

### SQLite backup consistency in WAL mode

SQLite runs in WAL mode. Copying only `bot_state.sqlite` can miss committed pages that still live in the `-wal` file. Backups now use the SQLite online backup API so the backup is a readable, consistent single-file snapshot.

### Rapid backup filename collision

Multiple backups created within one second could reuse the same filename and overwrite an earlier backup. Backup filenames now include microseconds.

### Stale single-instance lock after app/Windows crash

A crash could leave a lock file that blocked the next launch. The lock now records the process ID and removes the stale file when that process is no longer running.

### Warning-throttle cache growth

Repeated unique warning messages could grow the warning-throttle cache during long runtimes. The cache is now pruned when it exceeds the bounded limit.

## Added simulated-data fixtures

The build now runs every CSV in `tests/simulated_data` through the deterministic strategy simulator. Fixtures cover:

- full profitable cycle
- no initial drop
- repeated anchor resets before drop
- prolonged flat/rising price path with no order
- BUY trail following lower prices until rebound
- slippage-buffer quantity sizing
- protective SELL loss exit
- protective SELL cancellation before final profit SELL
- protective SELL replacement by profit SELL
- no sell trigger / held position
- RTH closed/reopened around entry conditions
- longer anchor-reset/drop paths for prolonged runtime behavior

## Build gates

The Windows build script now runs unit/integration tests and then executes every CSV scenario:

```powershell
.\.venv\Scripts\python.exe -m pytest
Get-ChildItem tests\simulated_data\*.csv | ForEach-Object {
    .\.venv\Scripts\python.exe scripts\run_simulated_strategy.py $_.FullName > $null
}
```

The assertion-heavy behavior checks live in pytest. The build-loop check guarantees every CSV fixture remains parseable and runnable through the deterministic simulator.

## Recovery tab placement

The Recovery tab is restored to the main tab strip as the rightmost tab:

1. Live strategy
2. Strategy flowchart
3. Trade history
4. Trade recovery

## Validation performed for this release

- Python compile check for `app`, `tests`, and `scripts`
- complete pytest suite
- every CSV fixture in `tests/simulated_data`

Live IB Gateway/TWS validation still has to be performed on Windows against the user's actual IBKR session.
