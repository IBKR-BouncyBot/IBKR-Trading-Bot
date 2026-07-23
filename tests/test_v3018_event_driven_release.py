from pathlib import Path


def test_v3018_release_note_is_archived_and_changelog_is_retained() -> None:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    archived_note = Path("docs/legacy/V3_0_18_EVENT_DRIVEN_CADENCES.md")
    archived_report = Path("docs/legacy/V3_0_18_IMPLEMENTATION_TEST_REPORT.txt")

    assert "## v3.0.18" in changelog
    assert archived_note.is_file()
    assert archived_report.is_file()
    assert "# v3.0.18 Event-driven controller cadences" in archived_note.read_text(encoding="utf-8")
    # The attached v3.0.19 source intentionally retains this historical note
    # at the repository-root docs path as well as in the archive.
    assert Path("docs/V3_0_18_EVENT_DRIVEN_CADENCES.md").is_file()
    assert Path("docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md").is_file()
