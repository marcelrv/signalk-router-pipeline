"""The gates a cleaned graph must pass before it is allowed to ship.

These are the five gates `docs/SPEC-GRAPH-DENSITY.md` 9 established as the
verification discipline for any change to graph density, plus one this work
needs on its own account (route shape). They exist because counts lie: builds
#11/#12 in `data/BUILD_LOG.md` looked healthy on every headline number while 21
named POIs -- Krammersluizen, the Middelburg harbours, bridges -- had quietly
fallen into an isolated 8-node island.

Two of them matter more than the rest for cleanup specifically:

* **Connectivity is measured by edge length, never by node count.** Removing
  nodes is the entire point here, so a node-count connectivity ratio would move
  on every successful run and tell you nothing. 6.1 records the node-count form
  "sent two investigations chasing a 2.61pp 'regression' that does not exist".
* **POI-pair reachability must not lose a single pair.** This is the gate that
  would have caught #11/#12.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .graph import RoutingGraph
from . import trace


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class GateReport:
    gates: List[Gate] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def __str__(self) -> str:
        return "\n".join(str(g) for g in self.gates)

    def failures(self) -> List[Gate]:
        return [g for g in self.gates if not g.passed]


@dataclass
class Baseline:
    """Everything measured on the graph *before* cleanup, to compare against.

    POIs are held as coordinates, not as node ids. Simplification legitimately
    removes the exact node a POI happened to snap to -- splicing a chain leaves
    the *line* in place and the POI re-snaps a few metres along it. What must not
    change is whether the POI still lands on the graph near where it is, and
    whether it can still reach everywhere it used to. Gating on node identity
    instead would fail every successful run.
    """

    nodes: int
    edges: int
    crosses_land: int
    largest_component_fraction: float
    pois: List[Tuple[float, float, str]]
    snap_dist_m: Dict[str, float]
    reachable_pairs: Set[Tuple[str, str]]
    probe_costs: Dict[Tuple[int, int], float]

    @classmethod
    def measure(cls, g: RoutingGraph, db_path: str,
                probe_pairs: Optional[Sequence[Tuple[int, int]]] = None) -> "Baseline":
        pois = trace.load_pois(db_path)
        snap, pairs = _snap_and_pairs(g, pois)
        probes: Dict[Tuple[int, int], float] = {}
        for s, t in (probe_pairs or []):
            _, cost = trace.shortest_path(g, s, t)
            probes[(s, t)] = cost
        return cls(
            nodes=len(g.nodes),
            edges=len(g.edges),
            crosses_land=sum(1 for e in g.edges.values() if e.crosses_land),
            largest_component_fraction=trace.largest_component_length_fraction(g),
            pois=pois,
            snap_dist_m=snap,
            reachable_pairs=pairs,
            probe_costs=probes,
        )


def _snap_and_pairs(g: RoutingGraph, pois: Sequence[Tuple[float, float, str]]
                    ) -> Tuple[Dict[str, float], Set[Tuple[str, str]]]:
    """Snap every POI to the current graph; return its snap distance and which
    POI pairs can reach each other. Keyed by a stable POI key, not by node id."""
    from .graph import geodesic_m

    idx = trace.NodeIndex(g)
    comp_of: Dict[int, int] = {}
    for i, comp in enumerate(trace.components(g)):
        for n in comp:
            comp_of[n] = i

    snap: Dict[str, float] = {}
    comp: Dict[str, int] = {}
    for lat, lon, name in pois:
        key = _poi_key(lat, lon, name)
        nid = idx.nearest(lat, lon)
        if nid is None:
            snap[key] = float("inf")
            continue
        n = g.nodes[nid]
        snap[key] = geodesic_m(lat, lon, n.lat, n.lon)
        comp[key] = comp_of.get(nid, -1)

    keys = sorted(comp)
    pairs: Set[Tuple[str, str]] = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if comp[a] == comp[b]:
                pairs.add((a, b))
    return snap, pairs


def _poi_key(lat: float, lon: float, name: str) -> str:
    return f"{name}@{lat:.5f},{lon:.5f}"


def check(g: RoutingGraph, baseline: Baseline,
          max_component_loss_pp: float = 0.5,
          max_route_cost_increase: float = 0.05,
          max_hub_degree: int = 30,
          max_snap_drift_m: float = 50.0) -> GateReport:
    """Run every gate against a cleaned graph. See the module docstring."""
    rep = GateReport()

    # 1. crosses_land stays 0 (or at least never grows).
    now_land = sum(1 for e in g.edges.values() if e.crosses_land)
    rep.gates.append(Gate(
        "crosses_land", now_land <= baseline.crosses_land,
        f"{baseline.crosses_land} -> {now_land}"))

    # 2. Connectivity by edge length, not node count.
    frac = trace.largest_component_length_fraction(g)
    loss_pp = (baseline.largest_component_fraction - frac) * 100.0
    rep.gates.append(Gate(
        "largest_component_by_length", loss_pp <= max_component_loss_pp,
        f"{baseline.largest_component_fraction:.4f} -> {frac:.4f} "
        f"({loss_pp:+.2f}pp, limit {max_component_loss_pp}pp)"))

    # 3. POI-pair reachability: zero loss, re-snapping each POI to the cleaned
    #    graph. The #11/#12 gate.
    now_snap, now_pairs = _snap_and_pairs(g, baseline.pois)
    lost = baseline.reachable_pairs - now_pairs
    detail = (f"{len(baseline.reachable_pairs)} pairs -> {len(now_pairs)}, "
              f"{len(lost)} lost")
    if lost:
        names = sorted({a for a, _ in lost} | {b for _, b in lost})
        detail += f"; affects {len(names)} POIs e.g. {names[:3]}"
    rep.gates.append(Gate("poi_pair_reachability", not lost, detail))

    # 3b. A POI that now snaps much further away is routeiq's `coverage_gap`
    #     symptom in the making: the route grows a long straight connecting leg
    #     that is excluded from every constraint check.
    drifted = {k: (baseline.snap_dist_m.get(k, 0.0), d)
               for k, d in now_snap.items()
               if d - baseline.snap_dist_m.get(k, 0.0) > max_snap_drift_m}
    worst = max(((d - b), k) for k, (b, d) in drifted.items()) if drifted else (0.0, None)
    rep.gates.append(Gate(
        "poi_snap_drift", not drifted,
        f"{len(drifted)} POIs snap >{max_snap_drift_m:g}m further than before"
        + (f", worst {worst[1]} +{worst[0]:.0f}m" if drifted else "")))

    # 4. Counts moved in the expected direction and only downward.
    grew = len(g.nodes) > baseline.nodes or len(g.edges) > baseline.edges
    dn = 100.0 * (baseline.nodes - len(g.nodes)) / baseline.nodes if baseline.nodes else 0
    de = 100.0 * (baseline.edges - len(g.edges)) / baseline.edges if baseline.edges else 0
    rep.gates.append(Gate(
        "counts", not grew,
        f"nodes {baseline.nodes} -> {len(g.nodes)} (-{dn:.1f}%), "
        f"edges {baseline.edges} -> {len(g.edges)} (-{de:.1f}%)"))

    # 5. No new hubs. Splicing joins neighbours, so it can raise a degree.
    hubs = [n for n in g.adj if g.degree(n) > max_hub_degree]
    max_deg = max((g.degree(n) for n in g.adj), default=0)
    rep.gates.append(Gate(
        "hubs", not hubs,
        f"{len(hubs)} nodes with out-degree > {max_hub_degree}, max {max_deg}"))

    # 6. Route shape. A cleanup that *shortens* a route has usually deleted a
    #    constraint; one that lengthens it has deleted something real. Both are
    #    worth stopping on, so this checks the magnitude of the change.
    if baseline.probe_costs:
        worst_name, worst, usable = None, 0.0, 0
        for (s, t), before in baseline.probe_costs.items():
            if before in (0.0, trace.INF):
                continue  # unroutable before cleanup; nothing to compare against
            usable += 1
            _, after = trace.shortest_path(g, s, t)
            if after == trace.INF:
                worst_name, worst = (s, t), float("inf")
                break
            delta = abs(after - before) / before
            if delta > worst:
                worst_name, worst = (s, t), delta
        if not usable:
            rep.gates.append(Gate(
                "route_shape", True,
                f"no usable probe: all {len(baseline.probe_costs)} were already "
                f"unroutable before cleanup"))
        else:
            rep.gates.append(Gate(
                "route_shape", worst <= max_route_cost_increase,
                f"worst of {usable} probe(s) {worst_name} changed {worst:.1%} "
                f"(limit {max_route_cost_increase:.0%})"))

    return rep
