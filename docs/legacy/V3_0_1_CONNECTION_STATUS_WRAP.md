# v3.0.1 connection-status wrapping

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This patch keeps trading, persistence, and broker behavior unchanged.

Changes:

- The IBKR API connection status label now wraps long connection failures instead of expanding the IBKR API connection form horizontally.
- The status text remains selectable so long IBKR/TWS error messages can be copied for diagnostics.
- Version metadata and visible app labels were bumped to `3.0.1`.

Validation:

- Static GUI regression tests verify the wrapping/size-policy configuration.
- Version metadata tests verify the visible GUI title, README title, and package metadata.
