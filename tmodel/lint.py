"""
Quality checks that don't make a model invalid but should be reviewed.
Structural errors (bad IDs, broken references) are raised by the schema.

Levels:
  warn  something is probably wrong or incomplete
  info  a deliberate choice worth a second look (fires every run by design)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .schema import NO_SURFACE, OWNER_PREFIX, Owner, Scope, ThreatModel, Treatment


@dataclass(frozen=True)
class Finding:
    entity: str
    rule: str
    level: Literal["warn", "info"]
    message: str

    def __str__(self) -> str:
        return f"{self.level:<4} {self.entity}: {self.message}  [{self.rule}]"


def lint(m: ThreatModel) -> list[Finding]:
    out: list[Finding] = []

    def warn(entity: str, rule: str, msg: str) -> None:
        out.append(Finding(entity, rule, "warn", msg))

    def info(entity: str, rule: str, msg: str) -> None:
        out.append(Finding(entity, rule, "info", msg))

    # Damages ----------------------------------------------------------------
    for d in m.damages:
        if not d.properties:
            warn(d.id, "damage-no-properties", "no compromised properties (C/I/A) marked")
        if not any(t.damage == d.id for t in m.threats):
            warn(d.id, "damage-no-threat", "no threat scenario leads to this damage")

    # Assets -----------------------------------------------------------------
    for a in m.assets:
        if m.get(a.domain).scope == Scope.out_of_scope:
            warn(a.id, "asset-out-of-scope", f"asset resides in out-of-scope domain {a.domain}")
        if not any(d.asset == a.id for d in m.damages):
            warn(a.id, "asset-no-damage", "no damage scenario for this asset")

    # Threats ----------------------------------------------------------------
    for t in m.threats:
        owners = {m.get(g).owner for g in t.goals}
        if t.treatment in (Treatment.reduce, Treatment.avoid) and Owner.product not in owners:
            warn(t.id, "treatment-no-goal", f"treatment is {t.treatment.value} but no product goal is linked")
        if t.treatment in (Treatment.share, Treatment.retain) and Owner.external not in owners:
            warn(
                t.id,
                "treatment-no-claim",
                f"treatment is {t.treatment.value} but no external goal (claim) is linked",
            )
        if not t.attack_path:
            warn(t.id, "threat-no-path", "no attack path")

        target = m.domain_of_threat(t)
        if t.surface == NO_SURFACE:
            info(t.id, "threat-no-surface", f"design-level threat on {target.id}, no entry surface")
            continue

        entry = m.get(t.surface)
        if t.vector is not None and t.vector.rank > entry.vector.rank:
            warn(
                t.id,
                "vector-exceeds-surface",
                f"vector {t.vector.value} is more remote than {entry.id} allows ({entry.vector.value})",
            )
        elif t.vector is not None and t.vector == entry.vector:
            info(t.id, "vector-redundant", f"vector equals {entry.id}'s; can be omitted")

        # Reachability: every surface on the path must touch the target
        # domain, directly or through earlier hops.
        reached: set[str] = set()
        for sid in m.path_surfaces(t):
            s = m.get(sid)
            if reached and not (reached & set(s.connects)):
                warn(
                    t.id,
                    "path-disconnected",
                    f"{sid} does not touch any domain reached so far ({', '.join(sorted(reached))})",
                )
            reached |= set(s.connects)
        if target.id not in reached:
            warn(
                t.id,
                "path-unreachable",
                f"path through {', '.join(m.path_surfaces(t))} never reaches {target.id}; "
                "add `via:` steps for the boundaries crossed, or a missing surface",
            )

    # Goals, surfaces --------------------------------------------------------
    used_goals = {g for t in m.threats for g in t.goals}
    used_surfaces = {s for t in m.threats for s in m.path_surfaces(t)}
    for g in m.goals:
        if g.id not in used_goals:
            warn(g.id, "goal-unused", "goal is not linked to any threat")
        # Not a validation error: changing the owner must not force an ID change.
        if not g.id.startswith(OWNER_PREFIX[g.owner] + "_"):
            warn(
                g.id,
                "goal-prefix-owner",
                f"owner is {g.owner.value}; IDs for that owner conventionally start {OWNER_PREFIX[g.owner]}_",
            )
    for s in m.surfaces:
        if s.id not in used_surfaces:
            warn(s.id, "surface-unused", "no threat enters or passes through this surface")

    return out
