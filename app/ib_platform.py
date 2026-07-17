"""Connection-profile, port, executable-discovery, and launch helpers.

Profiles define platform and paper/live mode while leaving host and port editable.
The launch helper starts a local TWS/Gateway executable only; it does not store
credentials, complete authentication, or establish the API connection.
"""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

TWS_PLATFORM = "tws"
GATEWAY_PLATFORM = "gateway"
SUPPORTED_PLATFORMS = {TWS_PLATFORM, GATEWAY_PLATFORM}


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    key: str
    label: str
    platform: str
    trading_mode: str
    host: str
    port: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


DEFAULT_CONNECTION_PROFILES: tuple[ConnectionProfile, ...] = (
    ConnectionProfile("gateway_live", "IB Gateway Live - 127.0.0.1:4001", GATEWAY_PLATFORM, "live", "127.0.0.1", 4001),
    ConnectionProfile("gateway_paper", "IB Gateway Paper - 127.0.0.1:4002", GATEWAY_PLATFORM, "paper", "127.0.0.1", 4002),
    ConnectionProfile("tws_live", "TWS Live - 127.0.0.1:7496", TWS_PLATFORM, "live", "127.0.0.1", 7496),
    ConnectionProfile("tws_paper", "TWS Paper - 127.0.0.1:7497", TWS_PLATFORM, "paper", "127.0.0.1", 7497),
)


def normalize_profile_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete profile dictionary for GUI/state use.

    The profile selector is the source of truth for paper/live mode. The host
    and port stay editable, so custom profiles preserve the last explicit
    platform/mode while still falling back to IB Gateway Live.
    """
    raw = dict(data or {})
    key = str(raw.get("key") or "gateway_live")
    defaults = DEFAULT_CONNECTION_PROFILES[0]
    matched = next((profile for profile in DEFAULT_CONNECTION_PROFILES if profile.key == key), defaults)
    platform = str(raw.get("platform") or matched.platform or GATEWAY_PLATFORM).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        platform = GATEWAY_PLATFORM
    mode = str(raw.get("trading_mode") or matched.trading_mode or "live").strip().lower()
    if mode not in {"paper", "live"}:
        mode = "live"
    host = str(raw.get("host") or matched.host or "127.0.0.1").strip()
    try:
        port = int(raw.get("port") or matched.port or default_port(platform, mode))
    except Exception:
        port = default_port(platform, mode)
    return {
        "key": key,
        "label": str(raw.get("label") or matched.label),
        "platform": platform,
        "trading_mode": mode,
        "host": host,
        "port": port,
    }


def profile_label_for(key: str) -> str:
    for profile in DEFAULT_CONNECTION_PROFILES:
        if profile.key == key:
            return profile.label
    return "Custom"


def platform_label(platform: str) -> str:
    normalized = (platform or "").strip().lower()
    if normalized == GATEWAY_PLATFORM:
        return "IB Gateway"
    return "Trader Workstation"


def default_port(platform: str, trading_mode: str) -> int:
    normalized_platform = (platform or TWS_PLATFORM).strip().lower()
    normalized_mode = (trading_mode or "paper").strip().lower()
    if normalized_platform == GATEWAY_PLATFORM:
        return 4001 if normalized_mode == "live" else 4002
    return 7496 if normalized_mode == "live" else 7497


def profile_key_for(platform: str, trading_mode: str, host: str, port: int) -> str:
    normalized = normalize_profile_dict({
        "platform": platform,
        "trading_mode": trading_mode,
        "host": host,
        "port": port,
    })
    for profile in DEFAULT_CONNECTION_PROFILES:
        if (
            profile.platform == normalized["platform"]
            and profile.trading_mode == normalized["trading_mode"]
            and profile.host == normalized["host"]
            and profile.port == normalized["port"]
        ):
            return profile.key
    return "custom"


@dataclass(slots=True)
class PlatformLaunchResult:
    started: bool
    executable: str = ""
    message: str = ""


_COMMON_TWS_PATHS = (
    r"C:\\Jts\\tws.exe",
    r"C:\\Jts\\Trader Workstation\\tws.exe",
)

_COMMON_GATEWAY_PATTERNS = (
    r"C:\\Jts\\ibgateway\\*\\ibgateway.exe",
    r"C:\\Jts\\ibgateway\\ibgateway.exe",
)


def _existing_file(path_text: str) -> Optional[str]:
    if not path_text:
        return None
    try:
        path = Path(os.path.expandvars(os.path.expanduser(path_text.strip().strip('"'))))
    except Exception:
        return None
    if path.is_file():
        return str(path)
    return None


def find_platform_executable(platform: str, configured_path: str = "") -> Optional[str]:
    configured = _existing_file(configured_path)
    if configured:
        return configured
    if sys.platform != "win32":
        return None
    normalized = (platform or TWS_PLATFORM).strip().lower()
    if normalized == GATEWAY_PLATFORM:
        matches: list[str] = []
        for pattern in _COMMON_GATEWAY_PATTERNS:
            matches.extend(glob.glob(os.path.expandvars(pattern)))
        files = [Path(path) for path in matches if Path(path).is_file()]
        if files:
            # IB Gateway folders usually contain version numbers. Pick newest by
            # modification time to avoid hard-coding a version directory.
            files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return str(files[0])
        return None
    for path in _COMMON_TWS_PATHS:
        found = _existing_file(path)
        if found:
            return found
    return None


def launch_platform(platform: str, configured_path: str = "") -> PlatformLaunchResult:
    label = platform_label(platform)
    executable = find_platform_executable(platform, configured_path)
    if not executable:
        return PlatformLaunchResult(
            started=False,
            message=(
                f"Could not find {label}. Set the executable path in the connection panel, "
                "then click Start selected IBKR app again."
            ),
        )
    try:
        subprocess.Popen([executable], cwd=str(Path(executable).parent), close_fds=True)  # noqa: S603
    except Exception as exc:
        return PlatformLaunchResult(started=False, executable=executable, message=f"Could not start {label}: {exc}")
    return PlatformLaunchResult(
        started=True,
        executable=executable,
        message=f"Started {label}. Complete login and 2FA, then wait for the API socket to become available.",
    )


@dataclass(slots=True)
class SocketProbeResult:
    reachable: bool
    error: str = ""


def probe_socket(host: str, port: int, timeout: float = 1.0) -> SocketProbeResult:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return SocketProbeResult(True, "")
    except Exception as exc:
        return SocketProbeResult(False, str(exc))


def connection_helper_text(platform: str, host: str, port: int, exc: Exception | str = "") -> str:
    label = platform_label(platform)
    probe = probe_socket(host, port, timeout=0.5)
    reason = f" Details: {exc}" if exc else ""
    if not probe.reachable:
        return (
            f"{label} API socket is not reachable at {host}:{port}. "
            f"Start {label}, log in, complete 2FA, enable socket clients, disable Read-Only API, "
            f"and verify the socket port.{reason}"
        )
    return (
        f"{label} socket is reachable at {host}:{port}, but the API login/handshake failed. "
        f"Verify Client ID, API settings, trusted IPs, and that {label} is fully logged in.{reason}"
    )
