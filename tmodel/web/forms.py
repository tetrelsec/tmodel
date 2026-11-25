"""
Forms generated from the Pydantic models, so a schema change needs no UI
change. Each entity field becomes a FieldSpec by inspecting its annotation:

  str                       text (textarea for description / rationale / statement)
  int                       number
  Enum                      select
  Enum | None               select with an "(inherit)" blank
  ID reference (by pattern) select of valid IDs from the model
  list[ID reference]        checkbox group
  list[Enum]                checkbox group
  ID | Literal["none"]      select with a "none" option
  list[Step]                textarea, one step per line, "@SURF_X" marks `via`
  anything else             textarea holding YAML (nested models, dicts)

Required selects on a *new* entity start with a blank "select…" option so
nothing defaults silently; on edit the current value is selected. The `id`
field is always plain text, read-only once the entity exists.
"""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass, field
from enum import Enum

import yaml
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ..schema import Step, ThreatModel

LONG_TEXT = {"description", "rationale", "statement"}
STEP_VIA = re.compile(r"\s+@(SURF_[A-Z0-9_]+)\s*$")


# ---------------------------------------------------------------------------
# Collections: discovered from ThreatModel, keyed by attribute name.
# ---------------------------------------------------------------------------


def _entity_classes() -> dict[str, type[BaseModel]]:
    return ThreatModel.entity_classes()


COLLECTIONS: dict[str, type[BaseModel]] = _entity_classes()


def _pattern_in(metadata) -> str | None:
    """First `pattern` constraint in a FieldInfo.metadata list, or None."""
    for m in metadata:
        if getattr(m, "pattern", None):
            return m.pattern
    return None


def _prefixes(pattern: str) -> list[str]:
    """ID prefixes an ID pattern allows, default first: "^TS_..." gives
    ["TS"], "^(?:CG|CC)_..." gives ["CG", "CC"]."""
    head = pattern.lstrip("^").split("_")[0]
    return head.removeprefix("(?:").removesuffix(")").split("|")


def _collection_for(pattern: str) -> str | None:
    """Collection an ID pattern refers to, or None if its prefix is unknown
    (then it is just a constrained string)."""
    return PREFIX_TO_COLLECTION.get(_prefixes(pattern)[0])


PREFIX_TO_COLLECTION: dict[str, str] = {
    p: n for n, c in COLLECTIONS.items() for p in _prefixes(_pattern_in(c.model_fields["id"].metadata))
}


# ---------------------------------------------------------------------------
# Annotation classification
# ---------------------------------------------------------------------------


@dataclass
class Kind:
    base: str  # str | int | enum | ref | steps | raw
    many: bool = False
    nullable: bool = False
    enum: type[Enum] | None = None
    ref: str | None = None  # collection name, when base == "ref"
    literals: list[str] = field(default_factory=list)


def classify(ann, metadata=()) -> Kind:
    """Map a field annotation (plus its FieldInfo.metadata) to a Kind."""
    pat = _pattern_in(metadata)
    if pat and _collection_for(pat):
        return Kind("ref", ref=_collection_for(pat))

    origin = typing.get_origin(ann)
    if origin is typing.Annotated:
        inner, *extras = typing.get_args(ann)
        pat = _pattern_in([m for e in extras for m in (getattr(e, "metadata", None) or [e])])
        if pat and _collection_for(pat):
            return Kind("ref", ref=_collection_for(pat))
        return classify(inner)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(ann)
        literals = [v for a in args if typing.get_origin(a) is typing.Literal for v in typing.get_args(a)]
        rest = [a for a in args if a is not type(None) and typing.get_origin(a) is not typing.Literal]
        k = classify(rest[0]) if rest else Kind("str")
        k.nullable, k.literals = type(None) in args, literals
        return k
    if origin is list:
        k = classify(typing.get_args(ann)[0])
        if k.base in ("str", "int", "raw"):
            return Kind("raw")  # scalar/nested lists need YAML, not a single text input
        k.many = True
        return k
    if isinstance(ann, type):
        if issubclass(ann, Enum):
            return Kind("enum", enum=ann)
        if ann is Step:
            return Kind("steps")
        if ann is int:
            return Kind("int")
        if ann is str:
            return Kind("str")
    return Kind("raw")


# ---------------------------------------------------------------------------
# FieldSpec: what the template renders
# ---------------------------------------------------------------------------


@dataclass
class FieldSpec:
    name: str
    label: str
    widget: str  # text|textarea|number|select|checkboxes|steps
    value: object = None  # str | int | list[str] | None
    options: list[tuple[str, str]] = field(default_factory=list)
    required: bool = True
    help: str = ""
    readonly: bool = False


def _label_of(e: BaseModel, limit: int = 60) -> str:
    for attr in ("name", "description", "statement"):
        v = getattr(e, attr, "")
        if v:
            return v if len(v) <= limit else v[: limit - 3] + "..."
    return ""


def label_of(model: ThreatModel, eid: str, limit: int = 60) -> str:
    """Short human label (name, else description) for any entity ID; "" if unknown."""
    e = model._index.get(eid)
    return _label_of(e, limit) if e is not None else ""


# ---------------------------------------------------------------------------
# References: which fields point at which collection, and the reverse.
# ---------------------------------------------------------------------------


def collection_of(eid: str) -> str | None:
    """Collection an entity ID belongs to, from its prefix (TS_01 -> threats)."""
    return PREFIX_TO_COLLECTION.get(eid.split("_", 1)[0])


def refs_in(e: BaseModel) -> list[tuple[str, str]]:
    """(field, referenced id) for every reference held by `e`, including
    references nested in sub-models (an attack step's `via`). Literal
    alternatives such as "none" are not references."""
    out: list[tuple[str, str]] = []
    for name, fi in type(e).model_fields.items():
        if name == "id":  # an entity's own ID matches its collection's pattern; not a reference
            continue
        value = getattr(e, name)
        k = classify(fi.annotation, fi.metadata)
        if k.base == "ref":
            for v in value if k.many else [value]:
                if v and v not in k.literals:
                    out.append((name, v))
            continue
        for sub in value if isinstance(value, list) else [value]:
            if isinstance(sub, BaseModel):
                out += [(name, v) for _, v in refs_in(sub)]
    return out


def used_by(model: ThreatModel, eid: str) -> list[tuple[str, str]]:
    """(entity id, field) for every entity that references `eid`, in model order."""
    out = []
    for coll in COLLECTIONS:
        for e in getattr(model, coll):
            for name, ref in refs_in(e):
                if ref == eid and (e.id, name) not in out:
                    out.append((e.id, name))
    return out


def ref_kind(cls: type[BaseModel], name: str) -> Kind | None:
    """The Kind of field `name` on `cls` if it is a reference field, else None
    (`id` never is: it names the entity itself)."""
    fi = cls.model_fields.get(name)
    if fi is None or name == "id":
        return None
    k = classify(fi.annotation, fi.metadata)
    return k if k.base == "ref" else None


def _ref_options(model: ThreatModel, coll: str) -> list[tuple[str, str]]:
    return [(e.id, f"{e.id}  {_label_of(e)}".rstrip()) for e in getattr(model, coll)]


def _jsonable(v):
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json", exclude_none=True)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def steps_to_text(steps: list[Step]) -> str:
    return "\n".join(s.text + (f"  @{s.via}" if s.via else "") for s in steps)


def text_to_steps(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = STEP_VIA.search(line)
        if m:
            out.append({"text": line[: m.start()].strip(), "via": m.group(1)})
        else:
            out.append({"text": line})
    return out


def form_fields(
    cls: type[BaseModel], model: ThreatModel, instance: BaseModel | None = None
) -> list[FieldSpec]:
    specs = []
    for name, fi in cls.model_fields.items():
        k = classify(fi.annotation, fi.metadata)
        label = name.replace("_", " ").capitalize()
        value = (
            getattr(instance, name)
            if instance is not None
            else (None if fi.default is PydanticUndefined else fi.default)
        )
        spec = FieldSpec(
            name,
            label,
            "text",
            required=fi.is_required(),
            help=fi.description or "",
            readonly=(name == "id" and instance is not None),
        )

        if k.base == "raw":
            # Fallback for any type without a dedicated widget: edit as YAML.
            spec.widget = "raw"
            dumped = _jsonable(value)
            spec.value = "" if not dumped else yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True)
            spec.help = (spec.help + " " if spec.help else "") + "(edited as YAML; no dedicated widget yet)"
            specs.append(spec)
            continue
        if name == "id":
            spec.widget, spec.value = "text", value or ""
            spec.help = spec.help.replace("e.g.", "Stable, never reused. e.g.")
        elif k.base == "steps":
            spec.widget = "steps"
            spec.value = steps_to_text(value or [])
            spec.help = "One step per line. End a line with  @SURF_X  where that step crosses a surface."
            # The IDs a step may cross, listed under the textarea (tap to append).
            via = next((rk for f in Step.model_fields if (rk := ref_kind(Step, f))), None)
            spec.options = _ref_options(model, via.ref) if via else []
        elif k.base == "enum":
            opts = [(e.value, e.value) for e in k.enum]
            if k.many:
                spec.widget, spec.options = "checkboxes", opts
                spec.value = [getattr(v, "value", v) for v in (value or [])]
            else:
                spec.widget = "select"
                blank = [("", "(inherit)")] if k.nullable else ([("", "select…")] if instance is None else [])
                spec.options = blank + opts
                spec.value = getattr(value, "value", value) if value is not None else ""
        elif k.base == "ref":
            opts = _ref_options(model, k.ref)
            if k.many:
                spec.widget, spec.options, spec.value = "checkboxes", opts, list(value or [])
            else:
                spec.widget = "select"
                blank = [("", "" if k.nullable else "select…")] if (k.nullable or instance is None) else []
                spec.options = blank + [(lit, lit) for lit in k.literals] + opts
                spec.value = value or ""
        elif k.base == "int":
            spec.widget = "number"
            spec.value = "" if value is None else value
        else:
            spec.widget = "textarea" if name in LONG_TEXT else "text"
            spec.value = value or ""
        specs.append(spec)
    return specs


def parse_form(cls: type[BaseModel], form, only: list[str] | None = None) -> dict:
    """Form data -> dict ready for cls.model_validate. Empty optionals are
    omitted. `only` restricts parsing to those fields (inline row edits)."""
    data: dict = {}
    for name, fi in cls.model_fields.items():
        if only is not None and name not in only:
            continue
        k = classify(fi.annotation, fi.metadata)
        if k.base == "raw":
            raw = (form.get(name) or "").strip()
            if raw:
                try:
                    data[name] = yaml.safe_load(raw)
                except yaml.YAMLError as e:
                    raise ValueError(f"{name}: invalid YAML: {e}") from e
        elif k.base == "steps":
            data[name] = text_to_steps(form.get(name, ""))
        elif k.many:
            data[name] = form.getlist(name)
        else:
            raw = (form.get(name) or "").strip()
            if raw == "":
                continue  # omitted: required fields fail as "Field required"
            data[name] = raw  # Pydantic reports numeric errors with the field location.
    return data


def table_columns(cls: type[BaseModel], max_cols: int = 8) -> list[str]:
    """Default columns: id, then scalar fields in schema order. Multi-line
    and raw fields are skipped, and so is long text when the class has a
    `name` to serve as its label."""
    has_name = "name" in cls.model_fields
    cols = ["id"]
    for name, fi in cls.model_fields.items():
        k = classify(fi.annotation, fi.metadata)
        if name == "id" or k.base in ("steps", "raw") or (has_name and name in LONG_TEXT):
            continue
        cols.append(name)
    return cols[:max_cols]


def root_fields(model: ThreatModel) -> list[FieldSpec]:
    """Scalar fields on the root model (everything that isn't a collection)."""
    specs = form_fields(ThreatModel, model, model)
    return [f for f in specs if f.name not in COLLECTIONS and f.name != "schema_version"]


def typed_values(fields: list[FieldSpec], form) -> None:
    """Overwrite FieldSpec values with what was submitted, so a form can be
    re-rendered with the user's input after a validation error."""
    for f in fields:
        if f.widget == "checkboxes":
            f.value = form.getlist(f.name)
        else:
            f.value = form.get(f.name, "")


def next_id(cls: type[BaseModel], existing: list[str]) -> str:
    """Suggest an ID for a new entity: PREFIX_<n+1>, zero-padded like the
    existing numeric IDs. With named IDs in use, just the prefix stub."""
    prefix = _prefixes(_pattern_in(cls.model_fields["id"].metadata))[0]
    existing = [x for x in existing if x.startswith(f"{prefix}_")]
    nums = [x[len(prefix) + 1 :] for x in existing if re.fullmatch(rf"{prefix}_\d+", x)]
    if not nums and existing:
        return f"{prefix}_"
    width = max((len(n) for n in nums), default=2)
    return f"{prefix}_{max((int(n) for n in nums), default=0) + 1:0{width}d}"
