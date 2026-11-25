"""
Threat model schema.

Close to an ISO/SAE 21434 TARA (assets, damage scenarios, threat scenarios,
attack paths, feasibility, risk, treatment, goals and claims), plus:

  * Goals and claims are one entity. A goal's `owner` says who must make it
    hold: the product (a 21434 cybersecurity goal, CG_*) or someone else (a
    cybersecurity claim, CC_*).

  * Domains: trust boundaries (a chip, a TEE, an app, a network zone).
    Assets live in exactly one domain.
  * Surfaces: the interfaces through which a domain is reached. A surface
    connects one or more domains, so domains and surfaces form a graph.
    A surface carries the attack vector (how close an attacker must be),
    which threats inherit unless they say otherwise.

Every entity has a stable, type-prefixed ID. References are by ID and are
checked on load. Risk is always derived from impact x feasibility and is
never stored. Vector is inherited from the surface unless a threat sets it.
"""

from __future__ import annotations

import re
from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal, cast, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

SCHEMA_VERSION: Literal["0.1"] = "0.1"
NO_SURFACE = "none"


# ---------------------------------------------------------------------------
# IDs: one rule for every type. PREFIX_ then upper-case letters, digits or
# underscores. Numeric (TS_01) and named (TS_CAN_UDS_FWUPDATE) both work.
# ---------------------------------------------------------------------------


def _id(prefix: str, example: str) -> type:
    """`prefix` may be an alternation like "CG|CC"; the first is the default."""
    p = f"(?:{prefix})" if "|" in prefix else prefix
    return Annotated[str, Field(pattern=rf"^{p}_[A-Z0-9_]+$", description=f"e.g. {example}")]


DomainId = _id("DOM", "DOM_MCU")
SurfaceId = _id("SURF", "SURF_CAN")
AssetId = _id("A", "A_01")
DamageId = _id("DS", "DS_01")
ThreatId = _id("TS", "TS_01")
GoalId = _id("CG|CC", "CG_01, or CC_01 for an external goal")


# ---------------------------------------------------------------------------
# Enumerations (ISO/SAE 21434 scales)
# ---------------------------------------------------------------------------


class Property(StrEnum):
    confidentiality = "C"
    integrity = "I"
    availability = "A"


class Impact(StrEnum):
    negligible = "Negligible"
    moderate = "Moderate"
    major = "Major"
    severe = "Severe"


class Feasibility(StrEnum):
    very_low = "Very Low"
    low = "Low"
    medium = "Medium"
    high = "High"


class AttackVector(StrEnum):
    """How close an attacker must be. Ordered: Physical is the least remote."""

    physical = "Physical"
    local = "Local"
    adjacent = "Adjacent"
    network = "Network"

    @property
    def rank(self) -> int:
        return list(AttackVector).index(self)


class Treatment(StrEnum):
    avoid = "Avoid"
    reduce = "Reduce"
    share = "Share"
    retain = "Retain"


class Owner(StrEnum):
    """Who must make a goal hold."""

    product = "product"  # 21434 cybersecurity goal; verifiable against the product
    external = "external"  # 21434 cybersecurity claim; an assumption about someone else


# ID prefix each owner is conventionally written with.
OWNER_PREFIX = {Owner.product: "CG", Owner.external: "CC"}


class Scope(StrEnum):
    in_scope = "in"
    out_of_scope = "out"


# Default 21434-style risk matrix: impact x feasibility -> 1..5.
_F = list(Feasibility)  # column order: Very Low, Low, Medium, High
DEFAULT_RISK_MATRIX: dict[Impact, dict[Feasibility, int]] = {
    Impact.severe:     dict(zip(_F, (2, 3, 4, 5), strict=True)),
    Impact.major:      dict(zip(_F, (1, 2, 3, 4), strict=True)),
    Impact.moderate:   dict(zip(_F, (1, 2, 2, 3), strict=True)),
    Impact.negligible: dict(zip(_F, (1, 1, 1, 1), strict=True)),
}  # fmt: skip


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class Entity(_Base):
    """Common typed identity; concrete entities constrain the ID prefix."""

    id: str


class Domain(Entity):
    """A trust boundary. A chip or execution environment for a device; a
    device or zone for a plant or network."""

    id: DomainId
    name: str
    description: str = ""
    parent: DomainId | None = Field(
        None,
        description="Containing domain; blank for a root. Nesting does not imply connectivity "
        "or inherit scope. Domains form a forest with no cycles.",
    )
    scope: Scope = Field(
        Scope.in_scope,
        description="'out' marks external entities (another ECU, a charger, "
        "the internet) that appear on diagrams but aren't assessed.",
    )


class Surface(Entity):
    """An interface through which a domain can be reached."""

    id: SurfaceId
    name: str
    description: str = ""
    connects: list[DomainId] = Field(
        min_length=1,
        description="Domains joined by this surface. One domain: exposed "
        "directly (a debug port). Two or more: an edge between them.",
    )
    vector: AttackVector = Field(
        description="Most remote position from which this surface is reachable. "
        "Threats through it inherit this unless they are less remote.",
    )


class Asset(Entity):
    id: AssetId
    name: str
    description: str = ""
    domain: DomainId = Field(description="Domain where the asset resides.")


class Damage(Entity):
    """Damage scenario (21434 clause 15.3)."""

    id: DamageId
    asset: AssetId
    description: str
    impact: Impact
    properties: list[Property] = Field(default_factory=list, description="Compromised security properties.")


class Step(_Base):
    """One attack path step. A plain string in YAML is accepted and means a
    step with no `via`. `via` names the surface this step crosses, which
    lets the full path be checked against the domain graph and gives agents
    a concrete target per hop."""

    text: str
    via: SurfaceId | None = None


class Threat(Entity):
    """Threat scenario with attack path, feasibility, risk and treatment
    (21434 clauses 15.4 to 15.9)."""

    id: ThreatId
    damage: DamageId
    surface: SurfaceId | Literal["none"] = Field(
        description="Entry surface, or 'none' for a design-level threat with "
        "no entry point (no secure boot, plaintext keys). Interfaces from an "
        "attacker foothold on the device (syscalls, IPC) are real surfaces.",
    )
    description: str
    attack_path: list[Step] = Field(default_factory=list, description="Ordered attack steps.")
    vector: AttackVector | None = Field(
        None,
        description="Omit to inherit the surface's vector. Required when surface is 'none'.",
    )
    feasibility: Feasibility
    treatment: Treatment
    rationale: str = Field("", description="Why this treatment; context for reviewers and agents.")
    goals: list[GoalId] = Field(default_factory=list, description="Product goals and external claims.")

    @field_validator("attack_path", mode="before")
    @classmethod
    def _coerce_steps(cls, v):
        return [{"text": s} if isinstance(s, str) else s for s in (v or [])]

    @field_serializer("attack_path")
    def _serialize_steps(self, steps: list[Step], info):
        """Compact form (plain strings for steps without `via`) only when
        dumping with context={"compact": True}, which the YAML writer does.
        Everything else, including JSON for agents, gets uniform objects."""
        compact = bool(info.context and info.context.get("compact"))
        return [s.text if (compact and s.via is None) else s.model_dump(exclude_none=True) for s in steps]

    @model_validator(mode="after")
    def _vector_required_without_surface(self) -> Threat:
        if self.surface == NO_SURFACE and self.vector is None:
            raise ValueError(f"{self.id}: vector is required when surface is 'none'")
        return self


class Goal(Entity):
    """What must hold for a threat's treatment to work. Owned by the product
    (a cybersecurity goal) or by someone else, such as the OEM, integrator
    or operator (a cybersecurity claim)."""

    id: GoalId
    owner: Owner = Field(Owner.product, description="product: CG_*, verifiable. external: CC_*, assumed.")
    name: str
    description: str = ""


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class ThreatModel(_Base):
    schema_version: Literal["0.1"] = Field(SCHEMA_VERSION, description="Format version, owned by the tool.")
    system: str
    revision: int = Field(
        1,
        ge=1,
        description="Model revision, owned by the author. Findings and verdicts reference a revision.",
    )
    description: str = ""
    retired_ids: list[str] = Field(
        default_factory=list,
        description="Deleted entity IDs, permanently reserved. Do not remove or reuse them.",
    )
    risk_matrix: dict[Impact, dict[Feasibility, Annotated[int, Field(ge=1, le=5)]]] | None = Field(
        None,
        description="Override of the default impact x feasibility matrix.",
    )

    domains: list[Domain]
    surfaces: list[Surface]
    assets: list[Asset]
    damages: list[Damage]
    threats: list[Threat]
    goals: list[Goal] = Field(default_factory=list)

    # Validation -----------------------------------------------------------

    @model_validator(mode="after")
    def _check_references(self) -> ThreatModel:
        errors: list[str] = []

        all_ids = [e.id for coll in self._collections().values() for e in coll]
        for dup, n in Counter(all_ids).items():
            if n > 1:
                errors.append(f"duplicate ID {dup} ({n}x)")

        for retired in self.retired_ids:
            if not re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", retired):
                errors.append(f"invalid retired ID {retired!r}")
        if len(set(self.retired_ids)) != len(self.retired_ids):
            errors.append("duplicate retired IDs")
        if reused := set(all_ids) & set(self.retired_ids):
            errors.append(f"retired IDs cannot be reused: {', '.join(sorted(reused))}")

        ids = {kind: {e.id for e in coll} for kind, coll in self._collections().items()}

        def need(owner: str, ref: str, kind: str) -> None:
            if ref not in ids[kind]:
                errors.append(f"{owner}: unknown {kind[:-1]} {ref}")

        parents = {d.id: d.parent for d in self.domains}
        for domain in self.domains:
            if domain.parent is not None:
                need(domain.id, domain.parent, "domains")
        # Walk iteratively, so deep but valid trees do not hit Python's recursion limit.
        checked: set[str] = set()
        for domain in self.domains:
            path: set[str] = set()
            current: str | None = domain.id
            while current is not None and current in parents and current not in checked:
                if current in path:
                    errors.append(f"{domain.id}: domain parent cycle involving {current}")
                    break
                path.add(current)
                current = parents[current]
            checked.update(path)

        for s in self.surfaces:
            for d in s.connects:
                need(s.id, d, "domains")
        for a in self.assets:
            need(a.id, a.domain, "domains")
        for d in self.damages:
            need(d.id, d.asset, "assets")
        for t in self.threats:
            need(t.id, t.damage, "damages")
            if t.surface != NO_SURFACE:
                need(t.id, t.surface, "surfaces")
            for step in t.attack_path:
                if step.via is not None:
                    need(t.id, step.via, "surfaces")
            for g in t.goals:
                need(t.id, g, "goals")

        if self.risk_matrix is not None:
            for i in Impact:
                for f in Feasibility:
                    if self.risk_matrix.get(i, {}).get(f) is None:
                        errors.append(f"risk_matrix: missing {i.value} x {f.value}")

        if errors:
            raise ValueError("; ".join(errors))
        self._index = {e.id: e for coll in self._collections().values() for e in coll}
        return self

    @classmethod
    def entity_classes(cls) -> dict[str, type[Entity]]:
        """Discover collections once from the schema, shared by core and UI."""
        out = {}
        for name, fi in cls.model_fields.items():
            if get_origin(fi.annotation) is list:
                inner = get_args(fi.annotation)[0]
                if isinstance(inner, type) and issubclass(inner, BaseModel) and "id" in inner.model_fields:
                    out[name] = cast(type[Entity], inner)
        return out

    def _collections(self) -> dict[str, list]:
        return {name: getattr(self, name) for name in self.entity_classes()}

    # Lookups and derived values -------------------------------------------

    def get(self, id_: str):
        return self._index[id_]

    def domain_of_threat(self, t: Threat) -> Domain:
        """Domain holding the asset the threat damages."""
        return self.get(self.get(self.get(t.damage).asset).domain)

    def vector_of(self, t: Threat) -> AttackVector:
        if t.vector is not None:
            return t.vector
        return self.get(t.surface).vector

    def risk_of_cell(self, impact: Impact, feasibility: Feasibility) -> int:
        return (self.risk_matrix or DEFAULT_RISK_MATRIX)[impact][feasibility]

    def risk_of(self, t: Threat) -> int:
        return self.risk_of_cell(self.get(t.damage).impact, t.feasibility)

    def path_surfaces(self, t: Threat) -> list[str]:
        """Entry surface followed by every `via` on the path, in order."""
        out = [] if t.surface == NO_SURFACE else [t.surface]
        out += [s.via for s in t.attack_path if s.via is not None]
        return out
