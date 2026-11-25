"""Validator and lint rules, one test each. These rules are the product."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from tmodel import ThreatModel, dumps, lint, load, loads
from tmodel.schema import AttackVector, Feasibility, Impact

EXAMPLES = Path(__file__).parent.parent / "examples"

MINIMAL = {
    "schema_version": "0.1",
    "system": "t",
    "domains": [{"id": "DOM_A", "name": "A"}, {"id": "DOM_X", "name": "X", "scope": "out"}],
    "surfaces": [
        {"id": "SURF_CAN", "name": "CAN", "connects": ["DOM_A", "DOM_X"], "vector": "Local"},
        {"id": "SURF_JTAG", "name": "JTAG", "connects": ["DOM_A"], "vector": "Physical"},
    ],
    "assets": [{"id": "A_01", "name": "fw", "domain": "DOM_A"}],
    "damages": [
        {"id": "DS_01", "asset": "A_01", "description": "bad fw", "impact": "Major", "properties": ["I"]}
    ],
    "threats": [
        {
            "id": "TS_01",
            "damage": "DS_01",
            "surface": "SURF_CAN",
            "description": "flash over uds",
            "attack_path": ["get on bus", "flash"],
            "feasibility": "Low",
            "treatment": "Reduce",
            "goals": ["CG_01"],
        }
    ],
    "goals": [{"id": "CG_01", "name": "signing"}],
}


def model(**changes) -> ThreatModel:
    d = copy.deepcopy(MINIMAL)
    d.update(changes)
    return ThreatModel.model_validate(d)


def threat(**changes) -> ThreatModel:
    d = copy.deepcopy(MINIMAL)
    d["threats"][0].update(changes)
    return ThreatModel.model_validate(d)


def rules(m: ThreatModel) -> set[str]:
    return {f.rule for f in lint(m)}


# Validation -----------------------------------------------------------------


def test_minimal_is_clean():
    assert rules(model()) == {"surface-unused"}  # JTAG has no threat; fine


@pytest.mark.parametrize(
    "field,bad",
    [
        ("domain", "DOM_NOPE"),
        ("surface", "SURF_NOPE"),
        ("damage", "DS_NOPE"),
    ],
)
def test_unknown_reference_rejected(field, bad):
    d = copy.deepcopy(MINIMAL)
    target = {"domain": d["assets"][0], "surface": d["threats"][0], "damage": d["threats"][0]}[field]
    target[field] = bad
    with pytest.raises(ValidationError, match=bad):
        ThreatModel.model_validate(d)


def test_domain_parent_defaults_to_none_and_round_trips():
    m = model()
    assert all(d.parent is None for d in m.domains)
    assert "parent:" not in dumps(m)
    m = model(
        domains=[
            {"id": "DOM_A", "name": "VM", "parent": "DOM_X"},
            {"id": "DOM_X", "name": "Cloud", "scope": "out"},
            {"id": "DOM_OTHER", "name": "Other root"},
        ]
    )
    assert loads(dumps(m)) == m
    assert m.get("DOM_A").parent == "DOM_X"
    assert m.get("DOM_A").scope.value == "in"  # explicit, not inherited
    assert m.assets[0].domain == "DOM_A"


@pytest.mark.parametrize(
    "domains, message",
    [
        ([{"id": "DOM_A", "name": "A", "parent": "DOM_NOPE"}], "unknown domain DOM_NOPE"),
        ([{"id": "DOM_A", "name": "A", "parent": "DOM_A"}], "parent cycle"),
        (
            [
                {"id": "DOM_A", "name": "A", "parent": "DOM_X"},
                {"id": "DOM_X", "name": "X", "parent": "DOM_A"},
            ],
            "parent cycle",
        ),
        (
            [
                {"id": "DOM_A", "name": "A", "parent": "DOM_X"},
                {"id": "DOM_X", "name": "X", "parent": "DOM_Y"},
                {"id": "DOM_Y", "name": "Y", "parent": "DOM_A"},
            ],
            "parent cycle",
        ),
    ],
)
def test_invalid_domain_parents_rejected(domains, message):
    with pytest.raises(ValidationError, match=message):
        model(domains=domains)


def test_domain_tree_is_order_independent_and_deep():
    domains = [{"id": "DOM_A", "name": "A", "parent": "DOM_0"}, {"id": "DOM_X", "name": "X"}]
    domains += [{"id": f"DOM_{i}", "name": str(i), "parent": f"DOM_{i + 1}"} for i in range(1500)]
    domains.append({"id": "DOM_1500", "name": "root"})
    m = model(domains=domains)
    assert m.get("DOM_A").parent == "DOM_0"
    assert loads(dumps(m)) == m


def test_unknown_via_rejected():
    with pytest.raises(ValidationError, match="SURF_NOPE"):
        threat(attack_path=[{"text": "x", "via": "SURF_NOPE"}])


def test_duplicate_id_rejected():
    d = copy.deepcopy(MINIMAL)
    d["goals"].append({"id": "CG_01", "name": "dup"})
    with pytest.raises(ValidationError, match="duplicate"):
        ThreatModel.model_validate(d)


@pytest.mark.parametrize("bad", ["ts_01", "T_01", "TS-01", "TS_", "01"])
def test_id_pattern(bad):
    with pytest.raises(ValidationError):
        threat(id=bad)


def test_named_ids_allowed():
    threat(id="TS_CAN_UDS_FLASH")


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        threat(likelihood="high")


def test_wrong_schema_version_rejected():
    with pytest.raises(ValidationError):
        model(schema_version="0.9")


def test_surface_none_requires_vector():
    with pytest.raises(ValidationError, match="vector is required"):
        threat(surface="none")
    threat(surface="none", vector="Physical")


def test_string_steps_coerced():
    m = threat(attack_path=["a", {"text": "b", "via": "SURF_JTAG"}])
    t = m.threats[0]
    assert t.attack_path[0].text == "a" and t.attack_path[0].via is None
    assert t.attack_path[1].via == "SURF_JTAG"


# Derived values -------------------------------------------------------------


def test_vector_inherited_from_surface():
    m = model()
    assert m.vector_of(m.threats[0]) == AttackVector.local


def test_vector_override_kept():
    m = threat(vector="Physical")
    assert m.vector_of(m.threats[0]) == AttackVector.physical


def test_risk_derived_from_matrix():
    assert model().risk_of(model().threats[0]) == 2  # Major x Low


def test_risk_matrix_override():
    matrix = {
        i: {f: 5 for f in ["Very Low", "Low", "Medium", "High"]}
        for i in ["Negligible", "Moderate", "Major", "Severe"]
    }
    m = model(risk_matrix=matrix)
    assert m.risk_of(m.threats[0]) == 5


def test_incomplete_risk_matrix_rejected():
    with pytest.raises(ValidationError, match="risk_matrix"):
        model(risk_matrix={"Major": {"Low": 2}})


@pytest.mark.parametrize("score", [0, -1, 6, 999])
def test_out_of_range_risk_rejected(score):
    matrix = {i: {f: score for f in Feasibility} for i in Impact}
    with pytest.raises(ValidationError, match="risk_matrix"):
        model(risk_matrix=matrix)


def test_retired_ids_are_valid_unique_and_not_active():
    for ids, message in [
        (["bad"], "invalid retired"),
        (["CG_X", "CG_X"], "duplicate retired"),
        (["TS_01"], "retired IDs cannot be reused"),
    ]:
        with pytest.raises(ValidationError, match=message):
            model(retired_ids=ids)
    assert model(retired_ids=["CG_OLD"]).retired_ids == ["CG_OLD"]


# Lint -------------------------------------------------------------------------


def test_lint_vector_exceeds_surface():
    assert "vector-exceeds-surface" in rules(threat(vector="Network"))
    assert "vector-exceeds-surface" not in rules(threat(vector="Physical"))


def test_lint_vector_redundant_is_info():
    f = [f for f in lint(threat(vector="Local")) if f.rule == "vector-redundant"]
    assert f and f[0].level == "info"


def test_lint_no_surface_is_info():
    f = [f for f in lint(threat(surface="none", vector="Physical")) if f.rule == "threat-no-surface"]
    assert f and f[0].level == "info"


def test_lint_path_unreachable_and_fixed_by_via():
    d = copy.deepcopy(MINIMAL)
    d["domains"].append({"id": "DOM_B", "name": "B"})
    d["surfaces"].append({"id": "SURF_IPC", "name": "ipc", "connects": ["DOM_A", "DOM_B"], "vector": "Local"})
    d["assets"].append({"id": "A_02", "name": "key", "domain": "DOM_B"})
    d["damages"].append(
        {"id": "DS_02", "asset": "A_02", "description": "leak", "impact": "Severe", "properties": ["C"]}
    )
    d["threats"].append(
        {
            "id": "TS_02",
            "damage": "DS_02",
            "surface": "SURF_CAN",
            "description": "pivot",
            "attack_path": ["rce in A", "pivot to B"],
            "feasibility": "Low",
            "treatment": "Retain",
        }
    )
    m = ThreatModel.model_validate(d)
    assert "path-unreachable" in rules(m)

    d["threats"][1]["attack_path"][1] = {"text": "pivot to B", "via": "SURF_IPC"}
    m = ThreatModel.model_validate(d)
    assert "path-unreachable" not in rules(m)
    assert "surface-unused" not in {f.rule for f in lint(m) if f.entity == "SURF_IPC"}


def test_lint_path_disconnected():
    d = copy.deepcopy(MINIMAL)
    d["domains"].append({"id": "DOM_B", "name": "B"})
    d["surfaces"].append({"id": "SURF_FAR", "name": "far", "connects": ["DOM_B"], "vector": "Physical"})
    d["threats"][0]["attack_path"] = [{"text": "jump", "via": "SURF_FAR"}]
    assert "path-disconnected" in rules(ThreatModel.model_validate(d))


@pytest.mark.parametrize(
    "treatment,linked,rule",
    [
        ("Reduce", [], "treatment-no-goal"),
        ("Avoid", [], "treatment-no-goal"),
        ("Reduce", ["CC_01"], "treatment-no-goal"),  # an external goal does not count
        ("Share", [], "treatment-no-claim"),
        ("Retain", [], "treatment-no-claim"),
        ("Share", ["CG_01"], "treatment-no-claim"),  # nor does a product goal
    ],
)
def test_lint_treatment_links(treatment, linked, rule):
    d = copy.deepcopy(MINIMAL)
    d["goals"].append({"id": "CC_01", "owner": "external", "name": "OEM isolates the bus"})
    d["threats"][0].update(treatment=treatment, goals=linked)
    assert rule in rules(ThreatModel.model_validate(d))


@pytest.mark.parametrize(
    "treatment,linked", [("Reduce", ["CG_01"]), ("Share", ["CC_01"]), ("Retain", ["CC_01"])]
)
def test_lint_treatment_links_satisfied(treatment, linked):
    d = copy.deepcopy(MINIMAL)
    d["goals"].append({"id": "CC_01", "owner": "external", "name": "OEM isolates the bus"})
    d["threats"][0].update(treatment=treatment, goals=linked)
    r = rules(ThreatModel.model_validate(d))
    assert "treatment-no-goal" not in r and "treatment-no-claim" not in r


def test_goal_owner_defaults_to_product_and_both_prefixes_load():
    d = copy.deepcopy(MINIMAL)
    d["goals"].append({"id": "CC_01", "owner": "external", "name": "x"})
    m = ThreatModel.model_validate(d)
    assert m.get("CG_01").owner == "product" and m.get("CC_01").owner == "external"
    d["goals"].append({"id": "CX_01", "name": "x"})
    with pytest.raises(ValidationError):
        ThreatModel.model_validate(d)


def test_lint_goal_prefix_owner():
    d = copy.deepcopy(MINIMAL)
    d["goals"] = [{"id": "CG_01", "owner": "external", "name": "x"}, {"id": "CC_01", "name": "y"}]
    found = {(f.entity, f.rule) for f in lint(ThreatModel.model_validate(d))}
    assert {("CG_01", "goal-prefix-owner"), ("CC_01", "goal-prefix-owner")} <= found
    assert "goal-prefix-owner" not in rules(model())


def test_lint_orphans():
    d = copy.deepcopy(MINIMAL)
    d["goals"].append({"id": "CG_02", "name": "unused"})
    d["assets"].append({"id": "A_02", "name": "lonely", "domain": "DOM_X"})
    d["damages"].append({"id": "DS_02", "asset": "A_01", "description": "x", "impact": "Major"})
    r = rules(ThreatModel.model_validate(d))
    assert {
        "goal-unused",
        "asset-no-damage",
        "asset-out-of-scope",
        "damage-no-threat",
        "damage-no-properties",
    } <= r


# IO -------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.yaml")))
def test_examples_load_and_round_trip(name):
    m = load(EXAMPLES / name)
    text = dumps(m)
    m2 = loads(text)
    assert m2 == m
    assert dumps(m2) == text


def test_dump_omits_derived_but_keeps_identity():
    text = dumps(model())
    assert "schema_version" in text and "revision: 1" in text
    assert "risk:" not in text and "vector" not in text.split("threats:")[1]


def test_committed_json_schema_is_current():
    """threat-model.schema.json is what editors read; regenerate with `tmodel schema -o`."""
    import json

    committed = json.loads((EXAMPLES.parent / "threat-model.schema.json").read_text())
    assert committed == ThreatModel.model_json_schema(), (
        "run: python -m tmodel schema -o threat-model.schema.json"
    )
