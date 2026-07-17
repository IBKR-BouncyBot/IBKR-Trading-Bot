"""Single-instance lock scoped to the portable application folder.

The lock reduces the risk of two processes using the same SQLite state and API
client configuration. It stores the owner PID and removes a stale file only after
a Windows-safe process-existence check indicates that the owner is gone. A copy
of the application in another folder remains a separate operational instance.
"""

from __future__ import annotations

import ctypes
import errno
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import lock_file_path


class SingleInstanceError(RuntimeError):
    pass


_ACQUIRED_LOCK_PATHS: set[str] = set()


def _lock_key(path: Path) -> str:
    """Return a stable key for in-process duplicate-lock detection."""
    try:
        return str(path.resolve(strict=False))
    except Exception:
        return str(path.absolute())


def _pid_is_running_windows(
    pid: int,
    *,
    kernel32: Any | None = None,
    get_last_error: Callable[[], int] | None = None,
) -> bool:
    """Best-effort Windows process-existence check without sending console signals.

    On Unix, ``os.kill(pid, 0)`` is a harmless existence probe. On Windows,
    signal value 0 can behave like CTRL_C_EVENT / a console control event
    in some environments, which can interrupt the active build/test batch and trigger CMD's
    ``Terminate batch job (Y/N)?`` prompt. Use Win32 process handles instead.
    """
    if pid <= 0:
        return False
    try:
        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if get_last_error is None:
            get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    except Exception:
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        # OpenProcess can fail for protected/elevated processes. Treat access
        # denied as running so the lock file is not removed incorrectly; other
        # failures are treated as not running/stale.
        try:
            return bool(get_last_error() == error_access_denied)
        except Exception:
            return True
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_is_running(pid: int) -> bool:
    """Best-effort cross-platform process-existence check."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) != errno.ESRCH
    return True


@dataclass(slots=True)
class SingleInstanceLock:
    path: Path = field(default_factory=lock_file_path)
    fd: int | None = None

    def _remove_stale_lock_if_possible(self) -> bool:
        """Remove a lock file when it clearly belongs to a dead process."""
        try:
            text = self.path.read_text(encoding="ascii", errors="ignore").strip()
            pid = int(text) if text else 0
        except Exception:
            return False
        if _pid_is_running(pid):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except Exception:
            return False

    def acquire(self) -> None:
        key = _lock_key(self.path)
        if key in _ACQUIRED_LOCK_PATHS:
            raise SingleInstanceError(
                f"Another bot instance appears to be using this portable folder. "
                f"Close the other instance or remove stale lock file: {self.path}"
            )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        for attempt in range(2):
            try:
                self.fd = os.open(str(self.path), flags)
                os.write(self.fd, str(os.getpid()).encode("ascii", errors="ignore"))
                _ACQUIRED_LOCK_PATHS.add(key)
                return
            except FileExistsError as exc:
                if attempt == 0 and self._remove_stale_lock_if_possible():
                    continue
                raise SingleInstanceError(
                    f"Another bot instance appears to be using this portable folder. "
                    f"Close the other instance or remove stale lock file: {self.path}"
                ) from exc
            except Exception:
                _ACQUIRED_LOCK_PATHS.discard(key)
                raise

    def release(self) -> None:
        key = _lock_key(self.path)
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        finally:
            _ACQUIRED_LOCK_PATHS.discard(key)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
