# Security policy

## Supported version

Security and correctness fixes are applied to the current repository version, v3.2.0. Older archived release notes do not define supported behavior.

## Reporting a vulnerability

Do not post sensitive trading data in a public issue, discussion, pull request, or commit. Use GitHub private vulnerability reporting when it is enabled for the repository. Otherwise, open a minimal public issue that contains no account, order, execution, position, filesystem, or credential details and asks the repository owner for a private contact channel.

A useful private report should include:

- the affected version and operating system;
- the relevant code path or reproducible sequence;
- the expected and observed behavior;
- a minimal sanitized log excerpt;
- the potential effect on order placement, cancellation, recovery, persistence, or data exposure.

## Sensitive artifacts

Treat these as private unless they have been deliberately sanitized:

- `bot_state.sqlite` and its WAL/SHM files;
- files under `backups/`, `exports/`, `debug_reports/`, and `debug_captures/`;
- audit bundles and history exports;
- screenshots showing account identifiers, positions, executions, order references, local usernames, or paths;
- `.env` files, private keys, certificates, and broker credentials.

The application does not store TWS/IB Gateway passwords, but generated diagnostics can contain account and trading information. Review every attachment before sharing it.

## Operational scope

This project can transmit live orders. A security report is not a substitute for monitoring TWS/IB Gateway, broker orders, positions, and executions. When a suspected issue may affect a live position, use the broker interface and the application's documented reconciliation process to establish the current broker state first.
