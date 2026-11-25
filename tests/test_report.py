"""Report views (register and heat map). No server needed."""

import copy

from test_schema import MINIMAL

from tmodel import ThreatModel, report
from tmodel.schema import Feasibility, Impact


def two_threats() -> ThreatModel:
    d = copy.deepcopy(MINIMAL)
    t2 = {
        **d["threats"][0],
        "id": "TS_02",
        "feasibility": "High",
        "attack_path": [{"text": "x", "via": "SURF_JTAG"}],
    }
    d["threats"].append(t2)
    return ThreatModel.model_validate(d)


def test_register_worst_first_and_resolved():
    m = two_threats()
    entries = report.register(m)
    assert [e.threat.id for e in entries] == ["TS_02", "TS_01"]
    assert entries[0].risk == m.risk_of(m.get("TS_02")) > entries[1].risk
    e = entries[0]
    assert e.asset == m.get(m.get(e.threat.damage).asset).name
    assert e.steps[0].via == m.get("SURF_JTAG").name and e.steps[0].via_id == "SURF_JTAG"
    assert [g.id for g in e.goals] == ["CG_01"]


def test_register_filters_to_a_cell():
    m = two_threats()
    impact = m.get(m.threats[0].damage).impact
    assert [e.threat.id for e in report.register(m, impact, Feasibility.high)] == ["TS_02"]
    assert report.register(m, Impact.negligible, Feasibility.high) == []


def test_heatmap_grid_and_placement():
    m = two_threats()
    grid = report.heatmap(m)
    assert (
        [row[0].impact for row in grid]
        == report.IMPACTS
        == [Impact.severe, Impact.major, Impact.moderate, Impact.negligible]
    )
    assert [c.feasibility for c in grid[0]] == list(Feasibility)
    cells = {c.threats[0]: c for row in grid for c in row if c.threats}
    assert cells["TS_02"].feasibility == Feasibility.high
    assert all(c.risk == m.risk_of_cell(c.impact, c.feasibility) for row in grid for c in row)


def test_damage_review_includes_unassessed_scenarios_and_orders_threats():
    d = two_threats().model_dump()
    d["damages"].extend(
        [
            {"id": "DS_02", "asset": "A_01", "description": "unassessed", "impact": "Severe"},
            {"id": "DS_03", "asset": "A_01", "description": "also major", "impact": "Major"},
        ]
    )
    m = ThreatModel.model_validate(d)
    groups = report.damage_groups(m)
    assert [g.damage.id for g in groups] == ["DS_02", "DS_01", "DS_03"]
    assert groups[0].threats == groups[2].threats == []
    assert [e.threat.id for e in groups[1].threats] == ["TS_02", "TS_01"]
    assert groups[1].asset == "fw" and groups[1].domain == "A"
    assert sum(len(g.threats) for g in groups) == len(m.threats)


def test_goal_mapping_many_to_many_claims_and_unmapped_goals():
    d = two_threats().model_dump()
    d["goals"].extend(
        [
            {"id": "CG_02", "name": "unmapped"},
            {"id": "CC_01", "name": "operator", "owner": "external"},
        ]
    )
    d["threats"][1]["goals"].append("CC_01")
    m = ThreatModel.model_validate(d)
    mappings = report.goal_mappings(m)
    assert [g.goal.id for g in mappings] == ["CG_01", "CG_02", "CC_01"]
    assert [[e.threat.id for e in g.threats] for g in mappings] == [["TS_02", "TS_01"], [], ["TS_02"]]


def test_review_empty_model_and_threat_without_goals():
    m = ThreatModel(system="empty", domains=[], surfaces=[], assets=[], damages=[], threats=[])
    assert report.damage_groups(m) == report.goal_mappings(m) == []
    d = copy.deepcopy(MINIMAL)
    d["threats"][0]["goals"] = []
    m = ThreatModel.model_validate(d)
    assert report.damage_groups(m)[0].threats[0].goals == []
    assert report.goal_mappings(m)[0].threats == []


def test_register_table_for_spreadsheets():
    m = two_threats()
    header, rows = report.register_table(m)
    assert len(rows) == 2 and all(len(r) == len(header) for r in rows)
    first = dict(zip(header, rows[0], strict=True))
    assert first["ID"] == "TS_02" and first["Risk"] == str(m.risk_of(m.get("TS_02")))
    assert first["Attack path"] == "1. x (via JTAG)"  # names, not IDs
