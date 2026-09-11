"""Self-tests for the repository's test and coverage gates."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.check_callable_coverage import check_callable_coverage, main


def _coverage_report(*, covered_lines: int, include_start_line: bool = True) -> dict[str, object]:
    uncovered: dict[str, object] = {
        "summary": {"num_statements": 2, "covered_lines": covered_lines},
    }
    if include_start_line:
        uncovered["start_line"] = 5
    return {
        "files": {
            "app/example.py": {
                "functions": {
                    "": {"start_line": 1, "summary": {"num_statements": 0, "covered_lines": 0}},
                    "covered": {"start_line": 1, "summary": {"num_statements": 1, "covered_lines": 1}},
                    "Example.uncovered": uncovered,
                }
            }
        }
    }


def test_callable_coverage_gate_reports_and_clears_unentered_callable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "example.py").write_text(
        "def covered():\n    return 1\n\nclass Example:\n    def uncovered(self):\n        return 2\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    failures, total, covered = check_callable_coverage(_coverage_report(covered_lines=0), source, tmp_path)
    assert total == 2
    assert covered == 1
    assert failures == ["app/example.py:5: Example.uncovered"]

    report_path = tmp_path / "coverage.json"
    report_path.write_text(json.dumps(_coverage_report(covered_lines=0)), encoding="utf-8")
    assert main(["--coverage-json", str(report_path), "--source", str(source)]) == 1

    report_path.write_text(json.dumps(_coverage_report(covered_lines=2)), encoding="utf-8")
    assert main(["--coverage-json", str(report_path), "--source", str(source)]) == 0


def test_callable_coverage_gate_derives_line_when_older_json_has_no_start_line(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "example.py").write_text(
        "def covered():\n    return 1\n\nclass Example:\n    def uncovered(self):\n        return 2\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    failures, total, covered = check_callable_coverage(
        _coverage_report(covered_lines=0, include_start_line=False),
        source,
        tmp_path,
    )

    assert total == 2
    assert covered == 1
    assert failures == ["app/example.py:5: Example.uncovered"]


def test_callable_coverage_gate_accepts_directory_and_file_sources(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "example.py").write_text("def example():\n    return 1\n", encoding="utf-8")
    entry_point = tmp_path / "main.py"
    entry_point.write_text("def main():\n    return 0\n", encoding="utf-8")
    report = {
        "files": {
            "app/example.py": {
                "functions": {
                    "": {"start_line": 1, "summary": {"num_statements": 0, "covered_lines": 0}},
                    "example": {"start_line": 1, "summary": {"num_statements": 1, "covered_lines": 1}},
                }
            },
            "main.py": {
                "functions": {
                    "": {"start_line": 1, "summary": {"num_statements": 0, "covered_lines": 0}},
                    "main": {"start_line": 1, "summary": {"num_statements": 1, "covered_lines": 1}},
                }
            },
        }
    }

    failures, total, covered = check_callable_coverage(report, [source, entry_point], tmp_path)

    assert failures == []
    assert total == 2
    assert covered == 2


def test_callable_coverage_gate_rejects_missing_report(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "example.py").write_text("def example():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["--coverage-json", "missing.json", "--source", "app"]) == 2


def test_primary_test_launchers_include_coverage_and_callable_gates() -> None:
    root = Path(__file__).resolve().parents[1]
    powershell = (root / "scripts" / "run_tests.ps1").read_text(encoding="utf-8")
    shell = (root / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    batch = (root / "run_all_tests.bat").read_text(encoding="utf-8")

    assert "--source=app,main" in powershell
    assert "check_callable_coverage.py" in powershell
    assert '"--source", "main.py"' in powershell
    assert "--source=app,main" in shell
    assert "check_callable_coverage.py" in shell
    assert "--source main.py" in shell
    assert powershell.count('"-m", "pytest"') == 1
    assert '"-m", "not soak"' not in powershell
    assert '"-m", "soak"' not in powershell
    assert "Run every pytest test" in powershell
    assert '-m "not soak"' in shell
    assert '-m soak' in shell
    assert "run_mutation_smoke.py" in powershell
    assert "run_mutation_smoke.py" in shell
    assert "every pytest test (including bounded soak tests)" in batch
    assert "No pytest marker filter is applied" in batch
    assert "safety mutation smoke tests" in batch
    assert "scripts\\run_tests.ps1" in batch
