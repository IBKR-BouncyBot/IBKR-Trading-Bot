"""Validate the interpreter before installing, testing or launching this release.

This script needs only the standard library, so an old virtual environment can
be rejected before its packages are changed. It never changes files or state.
"""
from __future__ import annotations

import platform
import sys
import sysconfig


def runtime_requirement_error(
    version: tuple[int, int], implementation: str, gil_disabled: object,
) -> str | None:
    if implementation != "CPython" or version != (3, 14):
        return (
            "BouncyBot 5.3.0 requires standard CPython 3.14.x; "
            f"found {implementation} {version[0]}.{version[1]}."
        )
    if gil_disabled:
        return "BouncyBot 5.3.0 requires the standard GIL-enabled build, not free-threaded Python 3.14t."
    return None


def main() -> int:
    error = runtime_requirement_error(
        (sys.version_info.major, sys.version_info.minor),
        platform.python_implementation(),
        sysconfig.get_config_var("Py_GIL_DISABLED"),
    )
    if error:
        print(error)
        return 1
    print(f"Python runtime verified: CPython {platform.python_version()} (standard GIL build).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
