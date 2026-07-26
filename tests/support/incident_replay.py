"""Helpers for compact, sanitized production-incident regression fixtures."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

INCIDENT_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "incidents"
INCIDENT_MANIFEST_PATH = INCIDENT_FIXTURE_DIR / "manifest.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_TOKEN_RE = re.compile(r"\b(?:DU|U)\d{5,}\b", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)")
_UNIX_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s\"'])/(?:home|Users|mnt|tmp|var)/", re.IGNORECASE)
_FORBIDDEN_KEYS = {
    "account",
    "account_id",
    "account_number",
    "acct_number",
    "client_id",
    "host_path",
    "local_path",
    "order_id",
    "perm_id",
    "username",
}


def incident_fixture_paths() -> list[Path]:
    """Return committed incident fixtures in deterministic order."""
    return sorted(
        path
        for path in INCIDENT_FIXTURE_DIR.glob("*.json")
        if path.name != INCIDENT_MANIFEST_PATH.name
    )


def load_incident_manifest() -> dict[str, Any]:
    """Load the committed fixture provenance manifest."""
    return json.loads(INCIDENT_MANIFEST_PATH.read_text(encoding="utf-8"))


def load_incident_fixture(name: str) -> dict[str, Any]:
    """Load one named fixture and validate its common envelope."""
    filename = name if name.endswith(".json") else f"{name}.json"
    path = INCIDENT_FIXTURE_DIR / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_incident_fixture(data, source=path)
    return data


def fixture_sha256(path: Path) -> str:
    """Return the byte-for-byte SHA-256 of one fixture file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_incident_fixture(data: dict[str, Any], *, source: Path | None = None) -> None:
    """Validate the stable fixture envelope shared by all incident types."""
    label = str(source or "incident fixture")
    assert data.get("schema_version") == 1, f"{label}: unsupported schema version"
    assert data.get("sanitized") is True, f"{label}: fixture must be marked sanitized"
    assert str(data.get("incident_id") or "").strip(), f"{label}: incident_id is required"
    assert str(data.get("title") or "").strip(), f"{label}: title is required"
    evidence = data.get("source_evidence")
    assert isinstance(evidence, list) and evidence, f"{label}: source_evidence is required"
    for item in evidence:
        assert isinstance(item, dict), f"{label}: evidence entries must be objects"
        assert str(item.get("kind") or "").strip(), f"{label}: evidence kind is required"
        digest = str(item.get("sha256") or "")
        assert _SHA256_RE.fullmatch(digest), f"{label}: invalid source SHA-256"


def flatten_strings(value: Any) -> Iterable[str]:
    """Yield every string in a nested fixture value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from flatten_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from flatten_strings(item)


def nested_keys(value: Any) -> Iterable[str]:
    """Yield normalized keys from a nested mapping/list structure."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).strip().lower()
            yield from nested_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from nested_keys(item)


def privacy_violations(data: dict[str, Any]) -> list[str]:
    """Return likely private identifiers, forbidden fields, or machine-local paths."""
    violations: list[str] = []
    for key in nested_keys(data):
        if key in _FORBIDDEN_KEYS:
            violations.append(f"forbidden key: {key}")
    for text in flatten_strings(data):
        if _ACCOUNT_TOKEN_RE.search(text):
            violations.append(f"account token: {text}")
        if _WINDOWS_ABSOLUTE_PATH_RE.search(text):
            violations.append(f"Windows path: {text}")
        if _UNIX_ABSOLUTE_PATH_RE.search(text):
            violations.append(f"Unix path: {text}")
    return sorted(set(violations))
