"""Diagram source and optional Graphviz layout checks."""

import copy
import json
import re
import shutil
import subprocess

import pytest
from test_schema import MINIMAL

from tmodel import ThreatModel
from tmodel.diagram import THREAT_INK, marked, to_dot


def model() -> ThreatModel:
    d = copy.deepcopy(MINIMAL)
    d["threats"][0]["attack_path"] = [{"text": "x", "via": "SURF_JTAG"}]
    d["goals"] = [{"id": "CG_01", "name": "g"}]
    d["threats"][0]["goals"] = ["CG_01"]
    return ThreatModel.model_validate(d)


def test_highlight_marks_a_threats_whole_path():
    m = model()
    t = m.threats[0]
    assert marked(m, t.id) == {t.surface, "SURF_JTAG", "A_01"}
    assert marked(m, "DS_01") == {"A_01"}
    assert marked(m, "CG_01") == marked(m, t.id)  # a goal: every threat it covers
    assert marked(m, "DOM_A") == {"DOM_A"} and marked(m, "NOPE_1") == set() and marked(m, None) == set()


def test_highlight_only_changes_the_marked_elements():
    m = model()
    plain, lit = to_dot(m), to_dot(m, highlight=m.threats[0].id)
    assert to_dot(m, highlight=None) == plain and "highlighted" not in plain
    assert "highlighted: TS_01" in lit and 'BORDER="3"' in lit
    assert to_dot(m, highlight="NOPE_1") == plain  # unknown IDs are ignored


def nested_model(root_asset: bool = False, root_scope: str = "out") -> ThreatModel:
    """Unordered forest, an external parent, an empty intermediate domain."""
    domains = [
        {"id": "DOM_LEAF", "name": "Leaf <&>", "parent": "DOM_CHILD"},
        {"id": "DOM_CHILD", "name": "Child", "parent": "DOM_ROOT"},
        {"id": "DOM_SIBLING", "name": "External sibling", "scope": "out", "parent": "DOM_ROOT"},
        {"id": "DOM_OTHER", "name": "Other root"},
        {"id": "DOM_EXT", "name": "External", "scope": "out"},
        {"id": "DOM_ROOT", "name": "Root", "scope": root_scope},
    ]
    connections = {
        "PARENT": ["DOM_ROOT", "DOM_CHILD"],
        "DEEP": ["DOM_ROOT", "DOM_LEAF"],
        "SIBLING": ["DOM_LEAF", "DOM_SIBLING"],
        "ENTRY": ["DOM_LEAF"],
        "FOREST": ["DOM_LEAF", "DOM_OTHER", "DOM_EXT"],
        "ROOT_ENTRY": ["DOM_ROOT"],
        "PARENT_LAST": ["DOM_LEAF", "DOM_CHILD"],
        "MULTI": ["DOM_ROOT", "DOM_CHILD", "DOM_LEAF"],
    }
    assets = [{"id": f"A_{i}", "name": f"Asset {i}", "domain": "DOM_LEAF"} for i in range(5)]
    if root_asset:
        assets.append({"id": "A_PARENT", "name": "Parent asset", "domain": "DOM_ROOT"})
    return ThreatModel.model_validate(
        {
            "system": "Nested",
            "domains": domains,
            "assets": assets,
            "surfaces": [
                {"id": f"SURF_{key}", "name": key, "connects": doms, "vector": "Local"}
                for key, doms in connections.items()
            ],
            "damages": [],
            "threats": [],
        }
    )


def dot_containers(dot: str) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Read cluster ownership without depending on a Graphviz installation."""
    stack = []
    clusters, nodes = {}, {}
    for line in dot.splitlines():
        line = line.strip()
        if match := re.fullmatch(r"subgraph cluster_(\w+) \{", line):
            domain = match[1]
            assert domain not in clusters
            clusters[domain] = tuple(stack)
            stack.append(domain)
        elif line == "}" and stack:
            stack.pop()
        elif match := re.match(r"(\w+) \[", line):
            node = match[1]
            if node not in {"graph", "node", "edge"}:
                assert node not in nodes
                nodes[node] = tuple(stack)
    return clusters, nodes


def test_flat_domains_keep_assets_external_nodes_and_entry_points():
    m = model()
    dot = to_dot(m)
    clusters, nodes = dot_containers(dot)
    assert clusters == {"DOM_A": ()}
    assert nodes == {"A_01": ("DOM_A",), "DOM_X": (), "SURF_CAN__node": (), "SURF_JTAG__node": ()}
    assert "DOM_X -> SURF_CAN__node [];" in dot
    assert "SURF_CAN__node -> A_01 [lhead=cluster_DOM_A];" in dot
    assert 'SURF_JTAG__node -> A_01 [lhead=cluster_DOM_A, color="#d9480f", dir=forward];' in dot
    explicit_none = m.model_dump()
    for domain in explicit_none["domains"]:
        domain["parent"] = None
    assert to_dot(ThreatModel.model_validate(explicit_none)) == dot


def test_repeated_external_connections_keep_the_surface_label():
    m = model()
    m.surfaces[0].connects = ["DOM_X", "DOM_X"]
    dot = to_dot(m, highlight="SURF_CAN")
    _, nodes = dot_containers(dot)
    assert nodes["SURF_CAN__node"] == ()
    assert "SURF_CAN__node [label=<" in dot
    assert "CAN" in dot and "Local" in dot and "TS_01" in dot
    assert dot.count("SURF_CAN__node [label=") == 1


@pytest.mark.parametrize("root_scope", ["in", "out"])
@pytest.mark.parametrize("root_asset", [False, True])
def test_nested_domains_and_surfaces_use_their_lowest_shared_container(root_asset, root_scope):
    m = nested_model(root_asset, root_scope)
    dot = to_dot(m)
    clusters, nodes = dot_containers(dot)
    assert clusters == {
        "DOM_ROOT": (),
        "DOM_CHILD": ("DOM_ROOT",),
        "DOM_LEAF": ("DOM_ROOT", "DOM_CHILD"),
        "DOM_OTHER": (),
    }
    assert nodes["DOM_SIBLING"] == ("DOM_ROOT",)  # external leaf stays a dashed node
    assert nodes["DOM_CHILD__anchor"] == ("DOM_ROOT", "DOM_CHILD")
    for i in range(5):
        assert nodes[f"A_{i}"] == ("DOM_ROOT", "DOM_CHILD", "DOM_LEAF")
    for key in ("PARENT", "DEEP", "SIBLING", "MULTI"):
        assert nodes[f"SURF_{key}__node"] == ("DOM_ROOT",)
    for key in ("ENTRY", "PARENT_LAST"):
        assert nodes[f"SURF_{key}__node"] == ("DOM_ROOT", "DOM_CHILD")
    for key in ("FOREST", "ROOT_ENTRY"):
        assert nodes[f"SURF_{key}__node"] == ()
    assert "Leaf &lt;&amp;&gt;" in dot
    assert "A_0 -> A_3 [style=invis];" in dot  # the leaf's existing balanced asset grid
    root_endpoint = "A_PARENT" if root_asset else "DOM_ROOT__anchor"
    assert nodes[root_endpoint] == ("DOM_ROOT",)
    assert "ltail=cluster_DOM_ROOT" not in dot  # internal parent endpoints must not be clipped
    assert "SURF_PARENT__node -> DOM_CHILD__anchor [lhead=cluster_DOM_CHILD];" in dot
    assert "SURF_DEEP__node -> A_1 [lhead=cluster_DOM_LEAF];" in dot
    assert "A_1 -> SURF_PARENT_LAST__node [ltail=cluster_DOM_LEAF];" in dot
    assert "SURF_PARENT_LAST__node -> DOM_CHILD__anchor [];" in dot
    assert 'SURF_ENTRY__node -> A_1 [lhead=cluster_DOM_LEAF, color="#d9480f", dir=forward];' in dot
    assert (
        "SURF_ROOT_ENTRY__node -> "
        + root_endpoint
        + ' [lhead=cluster_DOM_ROOT, color="#d9480f", dir=forward];'
        in dot
    )


def test_nested_highlights_and_no_surface_threats_stay_on_their_own_domain():
    m = model()
    m.domains[0].parent = "DOM_X"  # the former external leaf becomes a dashed parent
    m.threats[0].surface = "none"
    m.threats[0].attack_path = []
    dot = to_dot(m, highlight="TS_01")
    assert marked(m, "TS_01") == {"A_01", "DOM_A"}
    assert f'color="{THREAT_INK}"; penwidth=3;' in dot
    assert 'STYLE="ROUNDED,DASHED"' not in dot  # external parent is now a cluster
    assert 'style="rounded,dashed"' in dot
    assert "no surface: TS_01" in dot
    assert "no surface: TS_01" not in to_dot(m, threats_on_edges=False)
    _, nodes = dot_containers(dot)
    assert nodes["A_01"] == ("DOM_X", "DOM_A")
    surface_dot = to_dot(m, highlight="SURF_CAN")
    assert f'DOM_X__anchor -> SURF_CAN__node [color="{THREAT_INK}", penwidth=3];' in surface_dot
    assert f'SURF_CAN__node -> A_01 [lhead=cluster_DOM_A, color="{THREAT_INK}", penwidth=3];' in surface_dot


def test_deep_forest_does_not_rely_on_python_recursion():
    depth = 1100
    m = ThreatModel.model_validate(
        {
            "system": "Deep",
            "domains": [
                {"id": f"DOM_{i}", "name": str(i), "parent": f"DOM_{i - 1}" if i else None}
                for i in reversed(range(depth))
            ],
            "assets": [],
            "surfaces": [
                {"id": "SURF_DEEP", "name": "Deep entry", "connects": [f"DOM_{depth - 1}"], "vector": "Local"}
            ],
            "damages": [],
            "threats": [],
        }
    )
    clusters, nodes = dot_containers(to_dot(m))
    assert len(clusters) == depth
    assert nodes["SURF_DEEP__node"] == tuple(f"DOM_{i}" for i in range(depth - 1))


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is optional")
@pytest.mark.parametrize("root_scope", ["in", "out"])
@pytest.mark.parametrize("root_asset", [False, True])
def test_graphviz_nested_layout_has_no_warnings_or_overlapping_boundaries(root_asset, root_scope):
    dot = to_dot(nested_model(root_asset, root_scope))
    result = subprocess.run(
        ["dot", "-Tjson0"], input=dot, text=True, capture_output=True, timeout=15, check=True
    )
    assert not result.stderr
    objects = {obj["name"]: obj for obj in json.loads(result.stdout)["objects"]}

    def cluster_box(domain):
        return tuple(float(n) for n in objects[f"cluster_{domain}"]["bb"].split(","))

    def node_box(node):
        obj = objects[node]
        x, y = (float(n) for n in obj["pos"].split(","))
        w, h = float(obj["width"]) * 72, float(obj["height"]) * 72
        return x - w / 2, y - h / 2, x + w / 2, y + h / 2

    def contains(outer, inner):
        return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]

    def disjoint(a, b):
        return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]

    clusters, nodes = dot_containers(dot)
    for domain, ancestors in clusters.items():
        for ancestor in ancestors:
            assert contains(cluster_box(ancestor), cluster_box(domain))
    for node, ancestors in nodes.items():
        for domain in clusters:
            if domain in ancestors:
                assert contains(cluster_box(domain), node_box(node))
            else:
                assert disjoint(cluster_box(domain), node_box(node)), (domain, node)
