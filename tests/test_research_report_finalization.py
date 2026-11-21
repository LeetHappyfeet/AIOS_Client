"""Report-artifact finalization is limited to explicitly expected JSON files."""
from __future__ import annotations

import json
import stat

import pytest
from pathlib import Path
from types import SimpleNamespace

from aios_client.research.finalize import materialize_research_report

NAME = "aios-experiment-01c1ab8f-5c15-557c-94f8-f6def75b3efc.json"
REPORT = json.dumps({
    "objective": "Use only Calibre",
    "outcome": "partial",
    "summary": "No verified passages",
    "evidence": [],
    "limitations": ["HTTP 404 for several search expressions"],
})


def make_workspace(tmp_path, mode="research", read_only=False):
    return SimpleNamespace(
        registry=SimpleNamespace(tool_mode=mode, read_only=read_only),
        guard=lambda: None,
        clean=lambda data: data,
        path=lambda rel: tmp_path / rel,
    )


def make_spec(paths=(NAME,), inspection=False):
    return SimpleNamespace(
        expected_outputs=[SimpleNamespace(path=p, kind="file") for p in paths],
        inspection_only=lambda: inspection,
    )


@pytest.mark.parametrize("name", [NAME, NAME.replace("experiment", "work-report")])
def test_required_report_is_written_and_readable_for_host_verification(tmp_path, name):
    workspace = make_workspace(tmp_path)
    spec = make_spec((name,))
    assert materialize_research_report(workspace, spec, REPORT) == [name]
    data = json.loads((tmp_path / name).read_text())
    assert data["objective"] == "Use only Calibre"
    assert data["outcome"] == "partial"
    assert data["evidence"] == []
    assert isinstance(data["limitations"], list)
    assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    assert materialize_research_report(workspace, spec, REPORT) == []


def test_never_write_unrequested_report_or_arbitrary_paths(tmp_path):
    workspace = make_workspace(tmp_path)
    assert materialize_research_report(workspace, make_spec(("notes.json",)), REPORT) == []
    assert materialize_research_report(workspace, make_spec(("../" + NAME,)), REPORT) == []
    assert materialize_research_report(workspace, make_spec(("folder/" + NAME,)), REPORT) == []
    assert list(tmp_path.iterdir()) == []


def test_inspection_and_nonresearch_cannot_materialize(tmp_path):
    assert materialize_research_report(
        make_workspace(tmp_path, read_only=True), make_spec(), REPORT
    ) == []
    assert materialize_research_report(
        make_workspace(tmp_path), make_spec(inspection=True), REPORT
    ) == []
    assert materialize_research_report(
        make_workspace(tmp_path, mode="workspace"), make_spec(), REPORT
    ) == []
    assert list(tmp_path.iterdir()) == []


def test_existing_report_is_never_overwritten(tmp_path):
    path = tmp_path / NAME
    path.write_text("previous report")
    assert materialize_research_report(make_workspace(tmp_path), make_spec(), REPORT) == []
    assert path.read_text() == "previous report"


def test_missing_evidence_fields_or_invalid_report_never_writes(tmp_path):
    for report in ('{"outcome":"partial"}', "not json", json.dumps({
        "objective": "research", "outcome": "completed", "evidence": "not-list",
        "limitations": [],
    })):
        assert materialize_research_report(
            make_workspace(tmp_path), make_spec(), report
        ) == []
    assert list(tmp_path.iterdir()) == []


def test_report_symlink_is_not_followed(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("do not change")
    (tmp_path / NAME).symlink_to(target)
    assert materialize_research_report(make_workspace(tmp_path), make_spec(), REPORT) == []
    assert target.read_text() == "do not change"


def test_finalized_report_uses_authoritative_assignment_objective(tmp_path):
    spec = make_spec()
    spec.objective = "Research Pleistocene glaciation using only authorised Calibre"
    claim = json.dumps({
        "objective": "Different task inserted by model",
        "outcome": "partial", "evidence": [],
        "limitations": ["Search returned HTTP 404"],
    })
    assert materialize_research_report(make_workspace(tmp_path), spec, claim) == [NAME]
    on_disk = json.loads((tmp_path / NAME).read_text())
    assert on_disk["objective"] == spec.objective
    assert on_disk["outcome"] == "partial"
