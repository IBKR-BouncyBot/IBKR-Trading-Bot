# legacy release Stage 2 native-order diagnostics

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This version does not change the submitted native TRAIL order type. It adds diagnostics for cases where the chart uses a selected app price such as marketPrice while the broker-side trailing order uses triggerMethod=Last.

Stage 2 only advances when TWS reports a BUY fill. A selected marketPrice crossing the displayed initial stop is not enough if raw last/delayedLast has not crossed, and the actual native trailing stop can move inside TWS after submission.
