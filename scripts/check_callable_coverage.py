#!/usr/bin/env python3
"""Fail when an executable application callable was never entered by tests.

Statement and branch percentages can hide completely untouched helpers in a
large module.  Coverage.py's JSON report includes per-function execution data;
this gate requires at least one executed statement in every executable function,
method, property, and nested helper under the selected source tree.

The check is deliberately complementary to, not a replacement for, assertions,
line coverage, branch coverage, static analysis, and integration simulations.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage-json",
        type=Path,
        default=Path("coverage.json"),
        help="Coverage.py JSON report generated with `coverage json`.",
    )
    parser.add_argument(
        "--source",
        action="append",
        type=Path,
        default=None,
        help=(
            "Application source directory or Python file whose callables must be entered. "
            "Repeat the option to cover multiple source roots; the default is `app`."
        ),
    )
    return parser


def _load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Coverage JSON does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read valid coverage JSON from {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError(f"Coverage JSON has no `files` mapping: {path}")
    return data


def _report_entry(files: dict[str, Any], source_file: Path, project_root: Path) -> tuple[str, dict[str, Any]] | None:
    resolved = source_file.resolve()
    candidates = {
        source_file.as_posix(),
        resolved.as_posix(),
    }
    try:
        candidates.add(source_file.resolve().relative_to(project_root).as_posix())
    except ValueError:
        pass
    for key, value in files.items():
        key_path = Path(key)
        if key in candidates:
            return key, value
        try:
            if key_path.resolve() == resolved:
                return key, value
        except OSError:
            continue
    return None


def _source_function_start_lines(source_file: Path) -> dict[str, int]:
    """Map Coverage.py-style qualified callable names to source line numbers."""
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    except (OSError, SyntaxError, UnicodeError):
        return {}

    lines: dict[str, int] = {}
    scope: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            scope.append(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines[".".join(scope)] = int(node.lineno)
            self.generic_visit(node)
            scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scope(node)

    Visitor().visit(tree)
    return lines


def _source_files(sources: Path | Sequence[Path]) -> list[Path]:
    """Return unique Python source files selected by directories or file paths."""
    selected = [sources] if isinstance(sources, Path) else list(sources)
    source_files: dict[Path, Path] = {}
    for source in selected:
        resolved = source.resolve()
        if resolved.is_dir():
            candidates = resolved.rglob("*.py")
        elif resolved.is_file() and resolved.suffix == ".py":
            candidates = (resolved,)
        else:
            raise ValueError(f"Source is not a Python file or directory: {source}")
        for candidate in candidates:
            if "__pycache__" not in candidate.parts:
                source_files[candidate.resolve()] = candidate.resolve()
    if not source_files:
        joined = ", ".join(str(source) for source in selected)
        raise ValueError(f"No Python source files found under: {joined}")
    return sorted(source_files.values())


def check_callable_coverage(
    report: dict[str, Any],
    source: Path | Sequence[Path],
    project_root: Path,
) -> tuple[list[str], int, int]:
    """Return uncovered descriptors, executable callable count, and covered count."""
    files = report["files"]
    failures: list[str] = []
    total = 0
    covered = 0

    for source_file in _source_files(source):
        entry = _report_entry(files, source_file, project_root)
        display_path = source_file.resolve().relative_to(project_root).as_posix()
        if entry is None:
            failures.append(f"{display_path}: module is absent from the coverage report")
            continue
        _, file_data = entry
        functions = file_data.get("functions")
        if not isinstance(functions, dict):
            failures.append(
                f"{display_path}: coverage report has no per-function data; use coverage.py 7.6 or newer"
            )
            continue
        source_lines = _source_function_start_lines(source_file)
        for qualified_name, function_data in sorted(
            functions.items(),
            key=lambda item: (
                int(item[1].get("start_line", 0) or source_lines.get(item[0], 0)),
                item[0],
            ),
        ):
            if not qualified_name:
                continue
            summary = function_data.get("summary") if isinstance(function_data, dict) else None
            if not isinstance(summary, dict):
                failures.append(f"{display_path}:{qualified_name}: malformed function summary")
                continue
            statement_count = int(summary.get("num_statements", 0) or 0)
            if statement_count <= 0:
                continue
            total += 1
            covered_lines = int(summary.get("covered_lines", 0) or 0)
            if covered_lines > 0:
                covered += 1
                continue
            line = int(function_data.get("start_line", 0) or source_lines.get(qualified_name, 0))
            failures.append(f"{display_path}:{line}: {qualified_name}")
    return failures, total, covered


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = Path.cwd().resolve()
    source_args = args.source or [Path("app")]
    sources = [source if source.is_absolute() else project_root / source for source in source_args]
    report_path = args.coverage_json if args.coverage_json.is_absolute() else project_root / args.coverage_json
    try:
        report = _load_report(report_path)
        failures, total, covered = check_callable_coverage(report, sources, project_root)
    except ValueError as exc:
        print(f"CALLABLE COVERAGE CHECK ERROR: {exc}", file=sys.stderr)
        return 2

    if failures:
        print("CALLABLE COVERAGE CHECK FAILED")
        print("The following application callables were never entered by the test suite:")
        for failure in failures:
            print(f"  - {failure}")
        print(f"Covered executable callables: {covered}/{total}")
        return 1

    print(f"CALLABLE COVERAGE CHECK PASSED: {covered}/{total} executable application callables entered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
