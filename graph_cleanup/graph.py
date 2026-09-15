"""In-memory view of a built routing `.sqlite`, for post-build cleanup
(`docs/SPEC-GRAPH-CLEANUP.md`).

Cleanup deliberately runs on the *built database* rather than as another
generation-time knob on `nautical_routing_pipeline.py`:

* a rebuild is 15+ minutes, and every cleanup decision is about the graph, not
  about the source charts -- re-deriving the charts to re-test a simplification
  tolerance is pure waste;
* node IDs are coordinate-derived (`_coord_to_id`,
  `nautical_routing_pipeline.py`), so a decision about a node keeps referring to
  the same place after the region is rebuilt. That is what makes an `ops.jsonl`
  a durable artifact instead of throwaway post-processing.

Shape of the data this loads (measured on build #39, `data/BUILD_LOG.md`):
62,904 nodes / 164,468 edge rows. Edges are stored **bidirectionally** -- one
row per direction with byte-identical attributes -- so this module keeps a
single undirected record per pair, keyed `(min(u, v), max(u, v))`, and re-emits
both directions on save. Self-loops (2 of them in #39) are dropped at load:
they cost a row each and can never be on a route.
"""
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from pyproj import Geod

_GEOD = Geod(ellps="WGS84")

EdgeKey = Tuple[int, int]

NODE_COLUMNS = (
    "id", "lat", "lon", "node_depth", "region_id",
    "node_kind_id", "source_tier", "source_id",
)

# Order matters: `EdgeRec.values()` and the INSERT in `save()` share it.
EDGE_COLUMNS = (
    "distance", "min_depth", "drval1", "max_air_draft", "min_width",
    "cost_factor", "distance_to_land", "edge_type_id", "traffic_mode",
    "crosses_land", "crosses_obstacle", "edge_kind_id", "source_tier",
    "source_id", "width_profile", "requires_lock", "lock_id",
)

NODE_KIND_NAVMESH = 1


def edge_key(u: int, v: int) -> EdgeKey:
    """Canonical undirected key for an edge."""
    return (u, v) if u <= v else (v, u)


def geodesic_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metres between two WGS84 points, same method the pipeline uses so a
    spliced edge's `distance` stays comparable with the ones it replaced."""
    _, _, dist = _GEOD.inv(lon1, lat1, lon2, lat2)
    return abs(dist)


@dataclass
class NodeRec:
    id: int
    lat: float
    lon: float
    node_depth: float
    region_id: Optional[int]
    node_kind_id: int
    source_tier: int
    source_id: Optional[int]


@dataclass
class EdgeRec:
    """One undirected edge. Attribute names mirror the `edges` table."""
    distance: float
    min_depth: Optional[float]
    drval1: Optional[float]
    max_air_draft: Optional[float]
    min_width: Optional[float]
    cost_factor: Optional[float]
    distance_to_land: Optional[float]
    edge_type_id: int
    traffic_mode: int
    crosses_land: int
    crosses_obstacle: int
    edge_kind_id: int
    source_tier: int
    source_id: Optional[int]
    width_profile: Optional[str]
    requires_lock: int
    lock_id: Optional[int]

    def values(self) -> Tuple:
        return tuple(getattr(self, c) for c in EDGE_COLUMNS)

    @property
    def weight(self) -> float:
        """Routing cost, the same product routeiq minimises."""
        return (self.distance or 0.0) * (self.cost_factor or 1.0)


@dataclass
class RoutingGraph:
    """Undirected in-memory graph plus enough provenance to write it back."""

    nodes: Dict[int, NodeRec] = field(default_factory=dict)
    edges: Dict[EdgeKey, EdgeRec] = field(default_factory=dict)
    adj: Dict[int, Set[int]] = field(default_factory=dict)
    source_db: Optional[str] = None
    dropped_self_loops: int = 0
    protected: Set[int] = field(default_factory=set)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, db_path: str) -> "RoutingGraph":
        conn = sqlite3.connect(db_path)
        try:
            g = cls(source_db=db_path)
            g.protected = _load_protected_nodes(conn)
            for row in conn.execute(f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes"):
                rec = NodeRec(*row)
                g.nodes[rec.id] = rec
                g.adj[rec.id] = set()
            sql = f"SELECT source, target, {', '.join(EDGE_COLUMNS)} FROM edges"
            for row in conn.execute(sql):
                u, v = int(row[0]), int(row[1])
                if u == v:
                    g.dropped_self_loops += 1
                    continue
                key = edge_key(u, v)
                if key in g.edges:
                    continue  # reverse direction, attributes are identical
                g.edges[key] = EdgeRec(*row[2:])
                g.adj.setdefault(u, set()).add(v)
                g.adj.setdefault(v, set()).add(u)
            return g
        finally:
            conn.close()

    # ---------------------------------------------------------------- queries

    def degree(self, node: int) -> int:
        return len(self.adj.get(node, ()))

    def neighbours(self, node: int) -> Set[int]:
        return self.adj.get(node, set())

    def edge(self, u: int, v: int) -> Optional[EdgeRec]:
        return self.edges.get(edge_key(u, v))

    def edge_length_m(self, u: int, v: int) -> float:
        a, b = self.nodes[u], self.nodes[v]
        return geodesic_m(a.lat, a.lon, b.lat, b.lon)

    def total_edge_length_m(self) -> float:
        return sum(e.distance or 0.0 for e in self.edges.values())

    # --------------------------------------------------------------- mutation

    def remove_edge(self, u: int, v: int) -> bool:
        key = edge_key(u, v)
        if key not in self.edges:
            return False
        del self.edges[key]
        self.adj[key[0]].discard(key[1])
        self.adj[key[1]].discard(key[0])
        return True

    def remove_node(self, node: int) -> bool:
        """Remove the node and every edge incident on it.

        Refuses a protected node (navmesh seam / POI-snapped) directly, so this
        invariant holds for every caller -- including `ops.apply()`'s replay
        path, which calls this without going through `simplify.py`'s own
        protected-aware op generation.
        """
        if node not in self.nodes or node in self.protected:
            return False
        for nb in list(self.adj.get(node, ())):
            self.remove_edge(node, nb)
        self.adj.pop(node, None)
        del self.nodes[node]
        return True

    def splice_out(self, node: int) -> bool:
        """Remove a degree-2 node and join its two neighbours with one edge that
        inherits the *worst* attribute of the pair it replaces, so simplifying a
        chain can never make it look safer or cheaper than it was.

        Refuses when the neighbours are already directly connected -- splicing
        there would silently discard the longer way round and change the graph's
        shape rather than just its resolution. Also refuses a protected node
        (navmesh seam / POI-snapped) directly -- see `remove_node`.
        """
        if node in self.protected or self.degree(node) != 2:
            return False
        a, b = tuple(self.adj[node])
        if edge_key(a, b) in self.edges:
            return False
        merged = self._merge_edges(self.edge(a, node), self.edge(node, b), a, b)
        self.remove_node(node)
        self.edges[edge_key(a, b)] = merged
        self.adj.setdefault(a, set()).add(b)
        self.adj.setdefault(b, set()).add(a)
        return True

    def move_node(self, node: int, lat: float, lon: float) -> bool:
        """Reposition a node and re-measure every edge that touches it.

        Note this does *not* re-key the node: the coordinate-derived ID now
        disagrees with the coordinate. That is deliberate -- re-keying would
        break every other op in the same file that names this node, and the ID's
        only contract is stability, not invertibility. Refuses a protected node
        (navmesh seam / POI-snapped) directly -- see `remove_node`.
        """
        if node not in self.nodes or node in self.protected:
            return False
        rec = self.nodes[node]
        rec.lat, rec.lon = lat, lon
        for nb in self.adj.get(node, ()):
            self.edges[edge_key(node, nb)].distance = self.edge_length_m(node, nb)
        return True

    def _merge_edges(self, e1: EdgeRec, e2: EdgeRec, a: int, b: int) -> EdgeRec:
        def worst_min(x, y):
            vals = [v for v in (x, y) if v is not None]
            return min(vals) if vals else None

        # The straight chord a-b is shorter than the path it replaces whenever
        # the spliced-out node sits off the direct line (a curved chain --
        # common here, see docs/SPEC-GRAPH-DENSITY.md's own turn-angle
        # measurements). Taking only the higher of the two cost factors then
        # isn't enough to keep the promise below: scale the cost factor up so
        # the merged weight is never less than what it replaces, on top of
        # (never instead of) the existing worst-of-the-two floor.
        distance = self.edge_length_m(a, b)
        combined_weight = e1.weight + e2.weight
        cost_factor = max(e1.cost_factor or 1.0, e2.cost_factor or 1.0)
        if distance > 0:
            cost_factor = max(cost_factor, combined_weight / distance)
        return EdgeRec(
            distance=distance,
            min_depth=worst_min(e1.min_depth, e2.min_depth),
            drval1=worst_min(e1.drval1, e2.drval1),
            max_air_draft=worst_min(e1.max_air_draft, e2.max_air_draft),
            min_width=worst_min(e1.min_width, e2.min_width),
            # Highest cost factor: the merged edge must not look cheaper than
            # the most expensive stretch it stands in for -- by weight, not
            # just by this per-metre factor (see the chord-shortening note
            # above).
            cost_factor=cost_factor,
            distance_to_land=worst_min(e1.distance_to_land, e2.distance_to_land),
            edge_type_id=e1.edge_type_id,
            traffic_mode=e1.traffic_mode if e1.traffic_mode == e2.traffic_mode else 0,
            crosses_land=max(e1.crosses_land, e2.crosses_land),
            crosses_obstacle=max(e1.crosses_obstacle, e2.crosses_obstacle),
            edge_kind_id=e1.edge_kind_id,
            # Worst (highest) tier wins -- a merged edge is only as trustworthy
            # as its least trustworthy half.
            source_tier=max(e1.source_tier, e2.source_tier),
            source_id=e1.source_id if e1.source_id == e2.source_id else None,
            width_profile=_merge_width_profile(e1.width_profile, e2.width_profile),
            requires_lock=max(e1.requires_lock, e2.requires_lock),
            lock_id=e1.lock_id if e1.lock_id is not None else e2.lock_id,
        )

    # ----------------------------------------------------------------- saving

    def save(self, out_path: str) -> None:
        """Copy the source database and rewrite `nodes` and `edges` from memory.

        Copying keeps `metadata`, `data_sources`, `navmesh_regions`, `pois` and
        the enum tables byte-identical, so a cleaned database stays a valid
        member of the published format without this module having to know the
        whole schema.
        """
        if not self.source_db:
            raise ValueError("RoutingGraph has no source database to copy from")
        if out_path != self.source_db:
            shutil.copyfile(self.source_db, out_path)
        conn = sqlite3.connect(out_path)
        try:
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM nodes")
            conn.executemany(
                f"INSERT INTO nodes ({', '.join(NODE_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(NODE_COLUMNS))})",
                [tuple(getattr(n, c) for c in NODE_COLUMNS) for n in self.nodes.values()],
            )
            cols = ("source", "target") + EDGE_COLUMNS
            rows: List[Tuple] = []
            for (u, v), e in self.edges.items():
                vals = e.values()
                rows.append((u, v) + vals)
                rows.append((v, u) + vals)
            conn.executemany(
                f"INSERT INTO edges ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                rows,
            )
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()


def _load_protected_nodes(conn: sqlite3.Connection) -> Set[int]:
    """Nodes that post-build cleanup must never delete or move.

    `navmesh_regions.boundary_node_ids` is the subset of a region's perimeter
    vertices that sit on the seam with a bordering skeleton piece -- they are how
    routeiq's funnel search enters and leaves the region
    (`nautical_routing_pipeline.build_navmesh_region`). Deleting one silently
    disconnects that region from the rest of its water body, and no headline
    count would show it.

    On build #39 this protects 3,757 of the 3,807 navmesh nodes, which is why
    the navmesh waste documented in `SPEC-GRAPH-DENSITY.md` 10 cannot be fixed
    from here at all: its `vertices`/`triangles` arrays are a triangulation, not
    a node list, so thinning it means re-triangulating. That fix belongs at
    generation time, via 10.6's `NAVMESH_BOUNDARY_SIMPLIFY_M` parameter.
    """
    import json

    out: Set[int] = set()
    try:
        rows = conn.execute("SELECT boundary_node_ids FROM navmesh_regions").fetchall()
    except sqlite3.OperationalError:
        return out
    for (blob,) in rows:
        if not blob:
            continue
        try:
            out.update(int(x) for x in json.loads(blob))
        except (ValueError, TypeError):
            continue
    return out


def _merge_width_profile(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Concatenate two `{"min_m": float, "samples_m": [...]}` blobs.

    Returns None if either side is missing -- a half-known width profile would
    read as a complete one downstream, and `min_width` already carries the
    number that constrains routing.
    """
    import json

    if not a or not b:
        return None
    try:
        pa, pb = json.loads(a), json.loads(b)
        samples = list(pa.get("samples_m") or []) + list(pb.get("samples_m") or [])
        if not samples:
            return None
        return json.dumps({"min_m": round(min(samples), 1), "samples_m": samples})
    except (ValueError, TypeError, AttributeError):
        return None


def iter_chains(g: RoutingGraph) -> Iterable[List[int]]:
    """Every maximal run of degree-2 nodes, as `[junction, interior..., junction]`.

    Junction here means "not degree 2" -- so dead ends (degree 1) and real
    junctions (degree 3+) both terminate a chain. Isolated degree-2 rings have
    no such endpoint and are skipped: they carry no through-traffic and are
    handled as whole components elsewhere.
    """
    seen: Set[EdgeKey] = set()
    for j in [n for n in g.adj if g.degree(n) != 2]:
        for nb in list(g.adj[j]):
            if edge_key(j, nb) in seen:
                continue
            path = [j, nb]
            seen.add(edge_key(j, nb))
            prev, cur = j, nb
            while g.degree(cur) == 2:
                nxt = next(x for x in g.adj[cur] if x != prev)
                if edge_key(cur, nxt) in seen:
                    break
                seen.add(edge_key(cur, nxt))
                path.append(nxt)
                prev, cur = cur, nxt
            if len(path) > 2:
                yield path
