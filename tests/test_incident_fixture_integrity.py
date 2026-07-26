"""Integrity, provenance, and privacy checks for incident replay fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.incident_replay import (
    fixture_sha256,
    incident_fixture_paths,
    load_incident_manifest,
    privacy_violations,
    validate_incident_fixture,
)

EXPECTED_INCIDENT_IDS = {
    "foreign_order_ref_isolation_20260723",
    "iren_invalid_price_20260722",
    "nbis_partial_fill_cancel_race_20260720",
    "vwra_delayed_data_block_20260724",
    "vwra_lse_continuous_close_mismatch_20260724",
    "vwra_stage3_market_rule_rounding_20260724",
}


@pytest.mark.parametrize("path", incident_fixture_paths(), ids=lambda path: path.stem)
def test_incident_fixture_schema_provenance_and_privacy(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    validate_incident_fixture(data, source=path)

    assert privacy_violations(data) == []
    assert path.stat().st_size < 25_000


def test_incident_fixture_inventory_is_complete_and_unique() -> None:
    fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in incident_fixture_paths()]
    incident_ids = [str(item["incident_id"]) for item in fixtures]

    assert set(incident_ids) == EXPECTED_INCIDENT_IDS
    assert len(incident_ids) == len(set(incident_ids))


def test_incident_manifest_matches_files_and_byte_hashes() -> None:
    manifest = load_incident_manifest()
    entries = manifest["fixtures"]
    paths = {path.name: path for path in incident_fixture_paths()}

    assert manifest["schema_version"] == 1
    assert {entry["file"] for entry in entries} == set(paths)
    assert {entry["incident_id"] for entry in entries} == EXPECTED_INCIDENT_IDS

    for entry in entries:
        path = paths[entry["file"]]
        fixture = json.loads(path.read_text(encoding="utf-8"))
        assert entry["incident_id"] == fixture["incident_id"]
        assert entry["source_evidence"] == fixture["source_evidence"]
        assert entry["fixture_sha256"] == fixture_sha256(path)


def test_source_fingerprints_are_unique_within_each_fixture() -> None:
    for path in incident_fixture_paths():
        data = json.loads(path.read_text(encoding="utf-8"))
        digests = [str(item["sha256"]) for item in data["source_evidence"]]
        assert len(digests) == len(set(digests)), path


def test_private_raw_audit_artifacts_are_not_committed_as_fixtures() -> None:
    fixture_root = incident_fixture_paths()[0].parent
    forbidden_suffixes = {".sqlite", ".sqlite3", ".db", ".log", ".zip"}

    forbidden = [
        path.relative_to(fixture_root)
        for path in fixture_root.rglob("*")
        if path.is_file() and path.suffix.lower() in forbidden_suffixes
    ]

    assert forbidden == []
