"""
Draw a threat model as a Graphviz diagram.

  * In-scope domains are boxes containing their assets and child domains.
  * Out-of-scope domains are dashed external entities (boxes when parents).
  * Surfaces connecting two or more domains are edges between them.
  * Surfaces on a single domain are entry points pointing into it.

Every element is labelled by name, with its ID in small type underneath so
the picture can still be read alongside the YAML, findings, and agent output.

Labels are HTML tables with explicit padding, and output is rendered through
Cairo so text is measured and drawn with the same font (SVG text becomes
outlines and looks identical in every viewer).
"""

from __future__ import annotations

import shutil
import subprocess
from html import escape

from .schema import NO_SURFACE, Asset, Damage, Domain, Goal, Scope, Surface, Threat, ThreatModel

FONT = "DejaVu Sans"
INK = "#1f2937"
MUTED = "#4b5563"
IN_FILL = "#eef3fd"
IN_LINE = "#3b5bdb"
OUT_LINE = "#9ca3af"
ENTRY_FILL = "#fff4e6"
ENTRY_LINE = "#d9480f"
THREAT_INK = "#c2410c"
MARK_FILL = "#fff7ed"  # highlighted entity
ASSET_ROWS = 4  # max assets per column inside a domain box
THREATS_PER_LINE = 4
CHAR_W = 6.0  # rough width (pt) of a 10pt bold name character, for equal asset widths


# ---------------------------------------------------------------------------
# Label builders (HTML-like Graphviz labels)
# ---------------------------------------------------------------------------


def _rows(name: str, id_: str, color: str, size: int = 10, width: int = 0, note: str = "") -> str:
    """Name in bold, then the ID (and an optional note) small and muted on its
    own row, so the ID's underscores never collide with the name. `width` is a
    minimum, in points; set on a cell (not the table) so the text stays centred."""
    w = f' WIDTH="{width}"' if width else ""
    small = f"{id_}  ·  {note}" if note else id_
    return (
        f'<TR><TD{w}><FONT POINT-SIZE="{size}" COLOR="{color}"><B>{escape(name)}</B></FONT></TD></TR>'
        f'<TR><TD><FONT POINT-SIZE="7.5" COLOR="{MUTED}">{escape(small)}</FONT></TD></TR>'
    )


def _threat_rows(ids: list[str]) -> str:
    lines = [", ".join(ids[i : i + THREATS_PER_LINE]) for i in range(0, len(ids), THREATS_PER_LINE)]
    return "".join(
        f'<TR><TD><FONT POINT-SIZE="8" COLOR="{THREAT_INK}">{escape(line)}</FONT></TD></TR>' for line in lines
    )


def _box(rows: str, line: str, fill: str, dashed: bool = False, border: int = 1) -> str:
    """Bordered, rounded, padded label used as a node."""
    style = "ROUNDED,DASHED" if dashed else "ROUNDED"
    return (
        f'<<TABLE BORDER="{border}" CELLBORDER="0" CELLSPACING="0" CELLPADDING="6" '
        f'STYLE="{style}" COLOR="{line}" BGCOLOR="{fill}">'
        f'<TR><TD><TABLE BORDER="0" CELLSPACING="2" CELLPADDING="0">{rows}</TABLE></TD></TR>'
        f"</TABLE>>"
    )


def _plain(rows: str, bg: str = "white") -> str:
    """Borderless label with a solid background so lines never run through text."""
    return f'<<TABLE BORDER="0" CELLSPACING="2" CELLPADDING="2" BGCOLOR="{bg}">{rows}</TABLE>>'


# ---------------------------------------------------------------------------
# DOT generation
# ---------------------------------------------------------------------------


def marked(m: ThreatModel, eid: str | None) -> set[str]:
    """IDs to highlight for entity `eid`: a threat lights up its whole path
    (entry surface, every `via`, the target asset, or its domain when it has
    no surface); a damage its asset; a goal every threat it covers; a domain,
    surface or asset itself."""
    if not eid:
        return set()
    try:
        e = m.get(eid)
    except KeyError:
        return set()
    if isinstance(e, Threat):
        out = set(m.path_surfaces(e)) | {m.get(e.damage).asset}
        return out | ({m.domain_of_threat(e).id} if e.surface == NO_SURFACE else set())
    if isinstance(e, Damage):
        return {e.asset}
    if isinstance(e, Goal):
        return {x for t in m.threats if eid in t.goals for x in marked(m, t.id)}
    if isinstance(e, Domain | Surface | Asset):
        return {eid}
    return set()


def to_dot(m: ThreatModel, threats_on_edges: bool = True, highlight: str | None = None) -> str:
    mark = marked(m, highlight)
    assets_by_domain: dict[str, list] = {d.id: [] for d in m.domains}
    for a in m.assets:
        assets_by_domain[a.domain].append(a)

    threats_by_surface: dict[str, list[str]] = {s.id: [] for s in m.surfaces}
    # Threats with no entry surface are shown on the domain of their asset.
    domain_threats: dict[str, list[str]] = {d.id: [] for d in m.domains}
    for t in m.threats:
        if t.surface == NO_SURFACE:
            domain_threats[m.domain_of_threat(t).id].append(t.id)
        else:
            threats_by_surface[t.surface].append(t.id)

    domains = {d.id: d for d in m.domains}
    children: dict[str | None, list[Domain]] = {None: []}
    ancestors: dict[str, list[str]] = {}
    for d in m.domains:
        children.setdefault(d.parent, []).append(d)
        path = [d.id]
        parent = d.parent
        while parent is not None:
            path.append(parent)
            parent = domains[parent].parent
        ancestors[d.id] = path[::-1]

    # Put a surface in the lowest shared container. Entry points stay outside
    # their domain; a parent-child interface belongs *inside* the parent, so
    # neither its label nor its edges need to cross the parent's boundary.
    surface_containers: dict[str, str | None] = {}
    surfaces_by_container: dict[str | None, list[Surface]] = {}
    for s in m.surfaces:
        paths = [ancestors[dom] for dom in s.connects]
        shared = []
        for level in zip(*paths, strict=False):
            if len(set(level)) != 1:
                break
            shared.append(level[0])
        # Repeated references still identify only one domain. Keep its surface
        # outside it, especially for external leaves which have no cluster.
        if len(set(s.connects)) == 1:
            shared = shared[:-1]
        container = shared[-1] if shared else None
        surface_containers[s.id] = container
        surfaces_by_container.setdefault(container, []).append(s)

    def is_cluster(d: Domain) -> bool:
        return d.scope == Scope.in_scope or bool(assets_by_domain[d.id]) or bool(children.get(d.id))

    def grid_rows(n: int) -> int:
        """Rows per column, balanced: 5 assets are 3 + 2, not 4 + 1."""
        return -(-n // -(-n // ASSET_ROWS)) if n else 0

    def port(dom_id: str, container: str | None) -> tuple[str, str | None]:
        """Node that edges into a domain attach to, and the cluster to clip at.
        For a domain with assets this is an asset in the first column, so no
        empty column is added to the box."""
        d = domains[dom_id]
        if not is_cluster(d):
            return dom_id, None
        # Graphviz cannot clip an edge to a cluster containing both endpoints.
        # A parent's own asset/anchor is its endpoint for an internal surface.
        cluster = None if container == dom_id else f"cluster_{dom_id}"
        assets = assets_by_domain[dom_id]
        if not assets:
            return f"{dom_id}__anchor", cluster
        first_col = assets[: grid_rows(len(assets))]
        return first_col[len(first_col) // 2].id, cluster

    def domain_rows(d, line: str) -> str:
        rows = _rows(d.name, d.id, line, size=13)
        if threats_on_edges and domain_threats[d.id]:
            ids = ", ".join(domain_threats[d.id])
            rows += (
                f'<TR><TD><TABLE BORDER="1" STYLE="ROUNDED" COLOR="{THREAT_INK}" '
                f'BGCOLOR="white" CELLBORDER="0" CELLPADDING="3" CELLSPACING="0"><TR><TD>'
                f'<FONT POINT-SIZE="8" COLOR="{THREAT_INK}">no surface: {escape(ids)}</FONT>'
                f"</TD></TR></TABLE></TD></TR>"
            )
        return rows

    def surface_rows(s) -> str:
        rows = _rows(s.name, s.id, INK, size=10, note=s.vector.value)
        if threats_on_edges and threats_by_surface[s.id]:
            rows += _threat_rows(threats_by_surface[s.id])
        return rows

    def surface_node(s: Surface, indent: str) -> str:
        entry = len(s.connects) == 1
        line, fill = (ENTRY_LINE, ENTRY_FILL) if entry else (INK, "white")
        on = s.id in mark
        if on:
            line, fill = THREAT_INK, MARK_FILL
        return f"{indent}{s.id}__node [label={_box(surface_rows(s), line, fill, border=3 if on else 1)}];"

    title = f'<B>{escape(m.system)}</B>   <FONT POINT-SIZE="11" COLOR="{MUTED}">rev {m.revision}</FONT>'
    if mark:
        title += f'   <FONT POINT-SIZE="11" COLOR="{THREAT_INK}">highlighted: {escape(highlight)}</FONT>'
    title = f"<{title}>"
    out = [
        "digraph threat_model {",
        "  rankdir=LR; compound=true; newrank=true; ordering=out;",
        f'  graph [fontname="{FONT}", fontcolor="{INK}", fontsize=16, nodesep=0.45, '
        f"ranksep=0.55, pad=0.4, labelloc=t, label={title}];",
        f'  node  [fontname="{FONT}", shape=plain];',
        f'  edge  [color="{INK}", penwidth=1.3, arrowsize=0.7, dir=none];',
        "",
    ]

    # Domains ------------------------------------------------------------
    def emit_domain(d: Domain, indent: str = "  ") -> None:
        dashed = d.scope == Scope.out_of_scope
        line = OUT_LINE if dashed else IN_LINE
        if d.id in mark:
            line = THREAT_INK
        if is_cluster(d):
            out.extend(
                [
                    f"{indent}subgraph cluster_{d.id} {{",
                    f"{indent}  label={_plain(domain_rows(d, line), bg=IN_FILL)};",
                    f'{indent}  style="rounded,{"dashed" if dashed else "filled"}"; fillcolor="{IN_FILL}"; '
                    f'color="{line}"; penwidth={3 if d.id in mark else 1.6}; margin=16;',
                ]
            )
            assets = assets_by_domain[d.id]
            if not assets:
                out.append(
                    f'{indent}  {d.id}__anchor [shape=box, style=invis, width=0.1, height=0.1, label=""];'
                )
            # Every asset in a domain gets the width of the longest name.
            width = round(max((len(a.name) for a in assets), default=0) * CHAR_W)
            for a in assets:
                on = a.id in mark
                label = _box(
                    _rows(a.name, a.id, INK, width=width),
                    THREAT_INK if on else IN_LINE,
                    MARK_FILL if on else "white",
                    border=3 if on else 1,
                )
                out.append(f"{indent}  {a.id} [label={label}];")
            # Grid of `rows` rows: invisible edges place each asset to the
            # right of the one `rows` earlier. Reversed because LR layout
            # stacks out-edges bottom-up.
            rows = grid_rows(len(assets))
            for i in reversed(range(rows, len(assets))):
                out.append(f"{indent}  {assets[i - rows].id} -> {assets[i].id} [style=invis];")
            if len(assets) > 1:
                col = " ".join(a.id for a in assets[:rows])
                out.append(f"{indent}  {{ rank=same; {col} }}")
        else:
            box = _box(_rows(d.name, d.id, line), line, "white", dashed=True, border=3 if d.id in mark else 1)
            out.append(f"{indent}{d.id} [label={box}];")

    # Use a stack rather than Python recursion: valid forests can be deep.
    pending = [(d, "  ", False) for d in reversed(children[None])]
    while pending:
        d, indent, closing = pending.pop()
        if closing:
            for s in surfaces_by_container.get(d.id, []):
                out.append(surface_node(s, indent + "  "))
            out.append(f"{indent}}}")
        else:
            emit_domain(d, indent)
            if is_cluster(d):
                pending.append((d, indent, True))
                pending.extend((child, indent + "  ", False) for child in reversed(children.get(d.id, [])))
    out.append("")

    # Surfaces: each is a node, joined to every domain it connects -------
    for s in m.surfaces:
        node = f"{s.id}__node"
        entry = len(s.connects) == 1
        on = s.id in mark
        container = surface_containers[s.id]
        if container is None:
            out.append(surface_node(s, "  "))
        lit = [f'color="{THREAT_INK}"', "penwidth=3"] if on else []

        doms = list(s.connects)
        # External (out-of-scope) domains go on the left of the surface.
        outside = [x for x in doms if domains[x].scope == Scope.out_of_scope]
        inside = [x for x in doms if domains[x].scope == Scope.in_scope]
        if entry:
            left, right = [], doms
        elif outside:
            left, right = outside, inside
        else:
            left, right = inside[:1], inside[1:]

        for dom in left:
            n, cl = port(dom, container)
            attrs = ([f"ltail={cl}"] if cl else []) + lit
            out.append(f"  {n} -> {node} [{', '.join(attrs)}];")
        for dom in right:
            n, cl = port(dom, container)
            attrs = [f"lhead={cl}"] if cl else []
            if entry:
                attrs += [*(lit[:1] or [f'color="{ENTRY_LINE}"']), "dir=forward", *lit[1:]]
            else:
                attrs += lit
            out.append(f"  {node} -> {n} [{', '.join(attrs)}];")

    out.append("}")
    return "\n".join(out)


def render(m: ThreatModel, path: str, threats_on_edges: bool = True) -> None:
    """Render to .svg, .png or .pdf (by extension), or write .dot source."""
    dot = to_dot(m, threats_on_edges)
    fmt = path.rsplit(".", 1)[-1].lower()
    if fmt in ("dot", "gv"):
        with open(path, "w") as f:
            f.write(dot + "\n")
        return
    if not shutil.which("dot"):
        raise RuntimeError("Graphviz 'dot' not found; write a .dot file instead or install graphviz")
    args = ["dot", f"-T{fmt}:cairo", "-o", path]
    if fmt == "png":
        args.insert(1, "-Gdpi=150")
    subprocess.run(args, input=dot.encode(), check=True, timeout=15)
