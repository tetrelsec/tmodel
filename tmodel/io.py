"""Load and dump threat models as YAML. Dump is minimal: defaults and
derived values that were not given are left out, so a round trip of a
hand-written file stays close to what was written."""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import ThreatModel

# Top-level key order for dumps, so files read in the same order every time.
_ORDER = [
    "schema_version",
    "system",
    "revision",
    "description",
    "risk_matrix",
    "domains",
    "surfaces",
    "assets",
    "damages",
    "threats",
    "goals",
]


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data: str):
    if "\n" in data or len(data) > 80:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def loads(text: str) -> ThreatModel:
    return ThreatModel.model_validate(yaml.safe_load(text))


def load(path: str | Path) -> ThreatModel:
    return loads(Path(path).read_text())


def dumps(m: ThreatModel) -> str:
    data = m.model_dump(mode="json", exclude_none=True, exclude_defaults=True, context={"compact": True})
    # Always written even when at default: these two identify the file.
    data["schema_version"] = m.schema_version
    data["revision"] = m.revision
    # Known keys first in a fixed order, then anything new in schema order.
    ordered = {k: data[k] for k in _ORDER if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})
    return yaml.dump(ordered, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def dump(m: ThreatModel, path: str | Path) -> None:
    Path(path).write_text(dumps(m))
