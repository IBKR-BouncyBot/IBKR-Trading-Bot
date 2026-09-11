# v3.5.0 GUI light-mode and layout corrections

**Release:** v3.5.0
**Scope:** GUI layout, theme startup, and GUI control-state restoration only

## Summary

v3.5.0 is a deliberately small presentation release on top of v3.4.0. It does not change strategy calculations, broker commands, recovery logic, database content, market-data selection, RTH handling, order ownership, or risk controls.

The release makes three operator-interface corrections:

1. The **Price data monitor** is now the first operational section below the stage ribbon in Simple, Advanced, and Debug views. In Advanced and Debug views it therefore appears before the connection and strategy configuration sections.
2. Every application process starts with the validated light Fusion palette, regardless of the Windows system theme. **View > Dark mode** and **View > Light mode** remain available for the current session.
3. Switching between light and dark mode now reapplies the active input-lock and workflow-button gates after Qt rebuilds its style objects. The five bottom workflow buttons no longer remain greyed out until the top lock button is toggled twice.

## Theme-switch control-state correction

The command cards cache their semantic state to avoid unnecessary repaint work. A Fusion style change can rebuild the native button style while that semantic state remains unchanged. v3.5.0 separates the cached visual state from the actual button enablement:

- a cached command-card update still reasserts the expected `enabled` state;
- the command-bar parent and view selector are re-enabled after the style rebuild;
- current stage/input-lock rules and command-button gates are recalculated immediately;
- the same reconciliation is repeated on the next Qt event-loop turn, after deferred style processing has completed.

This does not bypass any safety gate. The recalculation uses the same current snapshot, cycle stage, connection state, recovery state, and manual input-lock state that normally control the buttons.

## Startup appearance

The application now calls the light Fusion palette explicitly during process startup. Theme selection is not persisted to SQLite or another settings store. Consequently:

- every ordinary launch starts in light mode;
- every watchdog replacement process also starts in light mode;
- the operator can switch to dark mode for the running session from the **View** menu;
- an operating-system theme change does not silently override the operator-facing startup rule.

## Layout

The Live strategy content order is now:

1. Stage ribbon
2. Price data monitor
3. Connection and strategy configuration panels in Advanced/Debug view
4. Market and strategy graph
5. Market/order/P&L state
6. Recovery/audit event section
7. Fixed bottom workflow command bar

Simple view continues to hide the connection and strategy configuration panels, so its visible order remains consistent.

## Compatibility

v3.5.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.4.0 databases, cycles, orders, recovery checkpoints, watchdog files, captures, and portable folder state remain compatible.

The release does not change:

- the five-stage trading strategy;
- ATR calculations or defaults;
- price-source priority or market-data freshness rules;
- RTH/session calculations;
- BUY, protective SELL, or final SELL order construction;
- partial-fill and recovery handling;
- account, contract, `conId`, Client ID, or `OrderRef` ownership rules;
- automatic worker-watchdog recovery;
- any database schema or broker-facing command.

## Verification scope

The focused v3.5.0 regression layer verifies:

- Price data monitor placement before Advanced/Debug configuration sections;
- explicit light-mode process startup;
- absence of the automatic system-theme hook from the startup path;
- retained manual Light/Dark choices under the View menu;
- reassertion of button enablement when a command card's cached semantic state is unchanged;
- immediate and deferred post-theme interaction-state reconciliation;
- preservation of current stage and command-gating inputs during that reconciliation;
- preservation of the stale-worker fail-closed command-button override during a theme switch;
- current release metadata, documentation placement, and Windows build versioning.

The final release report in the repository records the complete test, coverage, mutation, simulation, static-analysis, and packaging results obtained for the released archive.
