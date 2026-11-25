"""Form generation from the Pydantic models. No server needed."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from tmodel import load
from tmodel import schema as S
from tmodel.io import dumps
from tmodel.web import forms as F

EXAMPLES = Path(__file__).parent.parent / "examples"
SAMPLE = EXAMPLES / "greenhouse.yaml"


class _Form(dict):
    """Minimal stand-in for Starlette's FormData."""

    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v is not None else [])


def test_collections_discovered_from_root_model():
    assert list(F.COLLECTIONS) == ["domains", "surfaces", "assets", "damages", "threats", "goals"]
    assert F.PREFIX_TO_COLLECTION["TS"] == "threats"
    assert F.PREFIX_TO_COLLECTION["CG"] == F.PREFIX_TO_COLLECTION["CC"] == "goals"


def test_widgets_for_threat():
    m = load(SAMPLE)
    w = {f.name: f.widget for f in F.form_fields(S.Threat, m, m.threats[0])}
    assert w == {
        "id": "text",
        "damage": "select",
        "surface": "select",
        "description": "textarea",
        "attack_path": "steps",
        "vector": "select",
        "feasibility": "select",
        "treatment": "select",
        "rationale": "textarea",
        "goals": "checkboxes",
    }
    surface = next(f for f in F.form_fields(S.Threat, m, m.threats[0]) if f.name == "surface")
    assert surface.options[0] == ("none", "none")
    assert ("SURF_CONSOLE", "SURF_CONSOLE  Console API") in surface.options
    vector = next(f for f in F.form_fields(S.Threat, m) if f.name == "vector")
    assert vector.options[0] == ("", "(inherit)")


def test_id_is_text_and_readonly_on_edit():
    m = load(SAMPLE)
    new = next(f for f in F.form_fields(S.Threat, m) if f.name == "id")
    edit = next(f for f in F.form_fields(S.Threat, m, m.threats[0]) if f.name == "id")
    assert new.widget == "text" and not new.readonly
    assert edit.readonly and edit.value == "TS_01"


def test_steps_text_round_trip():
    steps = [S.Step(text="a"), S.Step(text="b", via="SURF_SMC")]
    text = F.steps_to_text(steps)
    assert text == "a\nb  @SURF_SMC"
    assert F.text_to_steps(text + "\n\n") == [{"text": "a"}, {"text": "b", "via": "SURF_SMC"}]


def test_parse_form_omits_empty_optionals():
    data = F.parse_form(
        S.Threat,
        _Form(
            id="TS_X",
            damage="DS_01",
            surface="SURF_CONSOLE",
            description="d",
            attack_path="a",
            vector="",
            feasibility="Low",
            treatment="Reduce",
            rationale="",
            goals=["CG_01"],
        ),
    )
    assert "vector" not in data and "rationale" not in data
    assert data["goals"] == ["CG_01"]


def test_table_columns_derived():
    assert F.table_columns(S.Damage) == ["id", "asset", "description", "impact", "properties"]
    assert F.table_columns(S.Domain) == ["id", "name", "parent", "scope"]  # description skipped: has name
    assert F.table_columns(S.Goal) == ["id", "owner", "name"]


def test_next_id_uses_default_prefix():
    assert F.next_id(S.Goal, ["CG_01", "CG_02", "CC_01"]) == "CG_03"
    assert F.next_id(S.Goal, ["CC_01"]) == "CG_01"


def test_root_fields_exclude_collections_and_schema_version():
    m = load(SAMPLE)
    assert [f.name for f in F.root_fields(m)] == [
        "system",
        "revision",
        "description",
        "retired_ids",
        "risk_matrix",
    ]


def test_ui_follows_schema_changes():
    """Forms, tables and dumps must track the Pydantic models without edits
    to the web layer. Simulates a schema evolution in-process."""

    class Evidence(S._Base):
        kind: Literal["test", "fuzz"]
        ref: str = ""

    class Assumption(S._Base):
        id: str = Field(pattern=r"^AS_[A-Z0-9_]+$")
        statement: str

    class Threat2(S.Threat):
        owner: str = ""
        evidence: list[Evidence] = Field(default_factory=list)

    class Model2(S.ThreatModel):
        threats: list[Threat2]
        assumptions: list[Assumption] = Field(default_factory=list)
        reviewed_by: str = ""

        def _collections(self):
            return {**super()._collections(), "assumptions": self.assumptions}

    base = load(SAMPLE).model_dump(mode="json")
    m = Model2.model_validate({**base, "assumptions": [{"id": "AS_01", "statement": "s"}]})

    # New fields get widgets; a type without one falls back to raw YAML.
    w = {f.name: f.widget for f in F.form_fields(Threat2, m, m.threats[0])}
    assert w["owner"] == "text" and w["evidence"] == "raw" and w["damage"] == "select"
    # Columns derive for a class the app has never seen.
    assert F.table_columns(Assumption) == ["id", "statement"]
    # The raw widget round-trips through parse_form.
    data = F.parse_form(
        Threat2,
        _Form(
            id="TS_X",
            damage="DS_01",
            surface="SURF_CONSOLE",
            description="d",
            attack_path="a",
            feasibility="Low",
            treatment="Reduce",
            evidence="- kind: fuzz\n  ref: r",
        ),
    )
    assert Threat2.model_validate(data).evidence[0].kind == "fuzz"
    # Dumps keep root keys they have never heard of.
    assert "assumptions:" in dumps(m) and "AS_01" in dumps(m)


def test_new_collection_works_through_running_app(tmp_path):
    """Evolve an isolated package, not just helpers: routes/forms/save must all follow."""
    import shutil
    import subprocess
    import sys

    package = tmp_path / "tmodel"
    shutil.copytree(EXAMPLES.parent / "tmodel", package, ignore=shutil.ignore_patterns("__pycache__"))
    path = package / "schema.py"
    source = path.read_text()
    source = source.replace(
        "class ThreatModel(_Base):",
        """class Assumption(Entity):
    id: Annotated[str, Field(pattern=r"^AS_[A-Z0-9_]+$")]
    statement: str


class ThreatModel(_Base):""",
    )
    source = source.replace("    system: str\n", '    system: str\n    reviewed_by: str = ""\n')
    source = source.replace(
        "    domains: list[Domain]\n",
        "    assumptions: list[Assumption] = Field(default_factory=list)\n    domains: list[Domain]\n",
    )
    path.write_text(source)
    probe = f"""
import sys, asyncio
from pathlib import Path
sys.path.insert(0, {str(tmp_path)!r})
from tmodel.web.app import create_app, Host
from tmodel.web.browser import call_asgi
app = create_app(Path({str(tmp_path / "models")!r}), git=False, host=Host(render_svg=lambda _: None))
async def run():
    headers = [("Content-Type", "application/x-www-form-urlencoded")]
    assert (await call_asgi(app, "POST", "/models", headers, b"id=fresh&system=Fresh"))["status"] == 303
    page = await call_asgi(app, "GET", "/m/fresh/assumptions/new", [], b"")
    assert page["status"] == 200 and b'name="statement"' in page["body"]
    result = await call_asgi(app, "POST", "/m/fresh/assumptions/new", headers, b"id=AS_01&statement=New+assumption")
    assert result["status"] == 303, result
    page = await call_asgi(app, "GET", "/m/fresh/assumptions", [], b"")
    assert page["status"] == 200 and b"AS_01" in page["body"]
    settings = await call_asgi(app, "GET", "/m/fresh/settings", [], b"")
    assert b'name="reviewed_by"' in settings["body"]
    assert app.state.store.load("fresh").assumptions[0].statement == "New assumption"
asyncio.run(run())
"""
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_required_selects_start_blank_for_new_entities_only():
    m = load(SAMPLE)
    new = {f.name: f for f in F.form_fields(S.Damage, m)}
    edit = {f.name: f for f in F.form_fields(S.Damage, m, m.damages[0])}
    assert new["asset"].options[0] == ("", "select…") and new["impact"].options[0] == ("", "select…")
    assert edit["asset"].options[0][0] == "A_01" and edit["impact"].options[0][0] == "Negligible"


def test_parse_form_omits_blank_required_so_pydantic_reports_it():
    data = F.parse_form(S.Damage, _Form(id="DS_X", asset="", description="d", impact=""))
    assert "asset" not in data and "impact" not in data
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="asset"):
        S.Damage.model_validate(data)


def test_next_id():
    assert F.next_id(S.Threat, ["TS_01", "TS_11"]) == "TS_12"
    assert F.next_id(S.Threat, ["TS_001"]) == "TS_002"
    assert F.next_id(S.Threat, []) == "TS_01"
    assert F.next_id(S.Domain, ["DOM_MCU", "DOM_TZ"]) == "DOM_"


def test_references_and_reverse_references():
    m = load(SAMPLE)
    t = m.threats[0]
    refs = F.refs_in(t)
    assert ("damage", t.damage) in refs and ("id", t.id) not in refs  # own ID is not a reference
    vias = [s.via for s in t.attack_path if s.via]
    assert all(("attack_path", v) in refs for v in vias)  # nested in steps
    users = F.used_by(m, t.damage)
    assert (t.id, "damage") in users and all(u != t.damage for u, _ in users)
    assert F.used_by(m, "TS_NOPE") == []
    assert F.collection_of("TS_01") == "threats" and F.collection_of("CC_01") == "goals"
    assert F.collection_of("XX_01") is None
    assert F.ref_kind(S.Threat, "id") is None and F.ref_kind(S.Threat, "damage").ref == "damages"


def test_domain_parent_is_optional_reference():
    m = load(SAMPLE)
    fields = {f.name: f for f in F.form_fields(S.Domain, m)}
    assert fields["parent"].widget == "select"
    assert not fields["parent"].required
    assert fields["parent"].options[0][0] == ""
    assert {d.id for d in m.domains} <= {v for v, _ in fields["parent"].options}
    child = S.Domain(id="DOM_CHILD", name="Child", parent=m.domains[0].id)
    assert F.refs_in(child) == [("parent", m.domains[0].id)]
    assert "parent" not in F.parse_form(S.Domain, _Form(id="DOM_CHILD", name="Child", parent=""))
    data = m.model_dump()
    data["domains"].append(child.model_dump())
    nested = S.ThreatModel.model_validate(data)
    assert ("DOM_CHILD", "parent") in F.used_by(nested, m.domains[0].id)
    assert "parent" in F.table_columns(S.Domain)


def test_steps_widget_lists_surfaces():
    m = load(SAMPLE)
    steps = next(f for f in F.form_fields(S.Threat, m, m.threats[0]) if f.name == "attack_path")
    assert [v for v, _ in steps.options] == [s.id for s in m.surfaces]
