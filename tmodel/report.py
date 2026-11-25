"""
Presentation views of a threat model: the threat register and the risk heat
map. Everything here is derived; nothing is stored.

Like diagram.py, this module knows the domain. The web layer only renders
what it returns, and the Excel/Word exports will reuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import (
    NO_SURFACE,
    AttackVector,
    Damage,
    Feasibility,
    Goal,
    Impact,
    Owner,
    Threat,
    ThreatModel,
    Treatment,
)

# Display order: worst first.
IMPACTS = list(reversed(Impact))  # Severe .. Negligible
FEASIBILITIES = list(Feasibility)  # Very Low .. High


@dataclass
class Hop:
    text: str
    via: str | None = None  # surface name
    via_id: str | None = None


@dataclass
class Entry:
    """One threat with everything needed to read it without the rest of the model."""

    threat: Threat
    risk: int
    impact: Impact
    feasibility: Feasibility
    vector: AttackVector
    vector_inherited: bool
    surface: str | None  # name; None for a design-level threat
    domain: str  # name of the domain holding the target asset
    asset: str
    damage: str
    steps: list[Hop]
    treatment: Treatment
    goals: list[Goal]


@dataclass
class Cell:
    impact: Impact
    feasibility: Feasibility
    risk: int
    threats: list[str] = field(default_factory=list)


def entry(m: ThreatModel, t: Threat) -> Entry:
    damage = m.get(t.damage)
    asset = m.get(damage.asset)
    return Entry(
        threat=t,
        risk=m.risk_of(t),
        impact=damage.impact,
        feasibility=t.feasibility,
        vector=m.vector_of(t),
        vector_inherited=t.vector is None,
        surface=None if t.surface == NO_SURFACE else m.get(t.surface).name,
        domain=m.get(asset.domain).name,
        asset=asset.name,
        damage=damage.description,
        steps=[Hop(s.text, m.get(s.via).name if s.via else None, s.via) for s in t.attack_path],
        treatment=t.treatment,
        goals=[m.get(g) for g in t.goals],
    )


def register(
    m: ThreatModel, impact: Impact | None = None, feasibility: Feasibility | None = None
) -> list[Entry]:
    """Threats worst first (risk, then impact, then feasibility), optionally
    narrowed to one heat map cell."""
    out = [entry(m, t) for t in m.threats]
    if impact is not None:
        out = [e for e in out if e.impact == impact]
    if feasibility is not None:
        out = [e for e in out if e.feasibility == feasibility]
    return sorted(
        out,
        key=lambda e: (-e.risk, IMPACTS.index(e.impact), -FEASIBILITIES.index(e.feasibility), e.threat.id),
    )


def heatmap(m: ThreatModel) -> list[list[Cell]]:
    """Impact rows (Severe first) by feasibility columns (Very Low first),
    each cell carrying its risk and the threats that land in it."""
    grid = {(i, f): Cell(i, f, m.risk_of_cell(i, f)) for i in IMPACTS for f in FEASIBILITIES}
    for t in m.threats:
        grid[(m.get(t.damage).impact, t.feasibility)].threats.append(t.id)
    return [[grid[(i, f)] for f in FEASIBILITIES] for i in IMPACTS]


@dataclass
class DamageGroup:
    damage: Damage
    domain: str
    asset: str
    threats: list[Entry]


@dataclass
class GoalMapping:
    goal: Goal
    threats: list[Entry]


def damage_groups(m: ThreatModel) -> list[DamageGroup]:
    """Full-model review agenda, including damage scenarios with no threats.

    Highest impact first, then highest derived risk within that impact; IDs
    break ties so the agenda stays stable between reviews.
    """
    entries = register(m)
    groups = []
    for d in m.damages:
        asset = m.get(d.asset)
        groups.append(
            DamageGroup(
                d, m.get(asset.domain).name, asset.name, [e for e in entries if e.threat.damage == d.id]
            )
        )
    return sorted(
        groups,
        key=lambda g: (
            IMPACTS.index(g.damage.impact),
            -max((e.risk for e in g.threats), default=0),
            g.damage.id,
        ),
    )


def goal_mappings(m: ThreatModel) -> list[GoalMapping]:
    """Reverse the threat → goal links, retaining unmapped goals and claims.

    Mapping is traceability, not evidence that a goal is implemented or tested.
    """
    entries = register(m)
    return [
        GoalMapping(g, [e for e in entries if g.id in e.threat.goals])
        for g in sorted(m.goals, key=lambda g: (g.owner != Owner.product, g.id))
    ]


REGISTER_COLUMNS = [
    "Risk", "ID", "Threat", "Impact", "Feasibility", "Vector", "Entry surface", "Domain", "Asset",
    "Damage", "Attack path", "Treatment", "Goals", "Claims", "Rationale",
]  # fmt: skip


def register_table(m: ThreatModel) -> tuple[list[str], list[list[str]]]:
    """The register as rows of text, worst first, every reference resolved to
    names, for spreadsheets (CSV now; Excel later)."""
    rows = []
    for e in register(m):
        goals = [f"{g.id} {g.name}" for g in e.goals if g.owner == Owner.product]
        claims = [f"{g.id} {g.name}" for g in e.goals if g.owner == Owner.external]
        steps = [f"{i}. {s.text}" + (f" (via {s.via})" if s.via else "") for i, s in enumerate(e.steps, 1)]
        rows.append(
            [
                str(e.risk),
                e.threat.id,
                e.threat.description,
                e.impact.value,
                e.feasibility.value,
                e.vector.value + (" (from surface)" if e.vector_inherited else ""),
                e.surface or "none (design-level)",
                e.domain,
                e.asset,
                e.damage,
                "\n".join(steps),
                e.treatment.value,
                "\n".join(goals),
                "\n".join(claims),
                e.threat.rationale,
            ]
        )
    return REGISTER_COLUMNS, rows
