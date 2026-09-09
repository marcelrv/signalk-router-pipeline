#!/usr/bin/env python3
"""Derive marked-channel axis lines from chart layers (docs/SPEC-CHANNEL-AXES.md).

Sits between ``enc_preprocessor.py`` (or ``clip_pilot_data.py``) and
``nautical_routing_pipeline.py``. Reads the GeoJSON layer directory, writes
``channel_axes_lines.geojson`` (one single-part LineString per derived axis, with a
confidence score), ``channel_axes_rejected.geojson`` (what was tried and why it was
dropped) and ``channel_axes_stats.json``.

Three source tiers, highest authority first:

* tier 1 -- charted axis lines (``inland_waterways_lines.geojson``: wtwaxs / RECTRC /
  NAVLNE). Already ingested by the pipeline as topology; used here only as coverage
  so lower tiers never duplicate them.
* tier 2 -- FAIRWY / DRGARE polygons: touching/overlapping polygons are unioned into
  channel components, each reduced to its Voronoi medial axis (spurs pruned, ends
  extended to the polygon boundary).
* tier 3 -- lateral marks (BOYLAT / BCNLAT, with BOYSAW safe-water marks): marks are
  grouped by the channel name in OBJNAM, ordered by their number (direction of
  buoyage), paired into gates where an opposite-hand mark sits abeam, and turned
  into a *corridor* polygon (buffer of the mark chain, clipped to charted water,
  with a "wall" on the shoal side of every single-sided mark). The corridor's
  medial axis, weighted to hug the mark chain, is the derived channel axis.

Measured basis (2026-09-09, MD NOAA + NL RWS cells): 96 % / 87 % of lateral marks
parse to ``<channel> <number>``; number parity matches CATLAM on 100 % / 98 %; only
48 % of marks have an opposite-hand partner within 400 m (so gates alone are not
enough); 61 % of MD marked channels and most Wadden/Oosterschelde gullies have no
polygon or line source at all -- the marks are the only charted evidence.

Every derived axis is validated (inside charted water, not across land, not
shallower than the marks it was derived from where DEPARE bands exist, minimum
length) and de-duplicated against higher tiers. A failure is written to the
rejected layer with a reason; the pipeline then keeps today's behaviour there.

Everything is computed in a local metric CRS (``estimate_utm_crs`` of the inputs)
and written back as WGS84.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union, voronoi_diagram
from shapely.strtree import STRtree

logger = logging.getLogger("derive_channel_axes")

# ----------------------------------------------------------------------------- inputs

LAYER_FILES = {
    "coastal_water": "coastal_water_polygons.geojson",
    "land": "land_polygons.geojson",
    "depth_areas": "depare_polygons.geojson",
    "fairways": "fairways_polygons.geojson",
    "dredged_areas": "dredged_areas_polygons.geojson",
    "inland_waterways": "inland_waterways_lines.geojson",
    "lateral_marks": "lateral_marks_points.geojson",
    "safe_water_marks": "safe_water_marks_points.geojson",
    "nav_systems": "nav_systems_polygons.geojson",
}
OUTPUT_AXES = "channel_axes_lines.geojson"
OUTPUT_REJECTED = "channel_axes_rejected.geojson"
OUTPUT_STATS = "channel_axes_stats.json"
# Charted land and water polygons overlap by a few metres where cells were digitised
# independently; only a real crossing (more than this much line over land) rejects.
LAND_CROSSING_TOLERANCE_M = 5.0

# S-57 CATLAM: category of lateral mark.
CATLAM_PORT = 1
CATLAM_STARBOARD = 2
CATLAM_PREF_STARBOARD = 3   # preferred channel to starboard (junction mark)
CATLAM_PREF_PORT = 4        # preferred channel to port (junction mark)

# US Coast Guard naming: "<channel> [Lighted] [Ice] <Buoy|Daybeacon|Light|...> <num>[suffix]".
_US_MARK_RE = re.compile(
    r"^(?P<chan>.+?)\s+(?:Lighted\s+)?(?:Ice\s+)?(?:Seasonal\s+)?"
    r"(?:Buoy|Daybeacon|Light|Beacon|Daymark|Dayboard)\s+(?P<num>\d+)(?P<suf>[A-Z]{0,2})$",
    re.IGNORECASE)
# European practice: "<abbreviation> <num>[suffix]" ("O 12", "ZOL 5", "WP 3A", "MAAS-1").
_EU_MARK_RE = re.compile(r"^(?P<chan>[A-Za-z][A-Za-z\-\. ]*?)\s*-?\s*(?P<num>\d+)\s?(?P<suf>[A-Za-z]?)$")
# Bare numbers ("27", "25 C"): the Westerschelde's buoys carry no channel prefix at all.
# They form one key and rely on spatial clustering + gap/turn splitting to separate
# channels, at reduced confidence.
_BARE_MARK_RE = re.compile(r"^(?P<num>\d+)\s?(?P<suf>[A-Za-z]?)$")
BARE_NUMBER_KEY = "(unnamed channel)"
# Trailing words that name the same channel as the bare name ("Wicomico River Entrance
# Light 1W" belongs to the "Wicomico River" chain).
_STRIP_TRAILING = ("entrance", "approach", "junction")


def parse_mark_name(name: Optional[str]) -> Optional[Tuple[str, int, str, str]]:
    """Return ``(channel_key, number, suffix, display_name)`` for a lateral-mark
    OBJNAM, or None when the name carries no channel + number.

    The key is lower-cased with trailing "entrance/approach/junction" removed so the
    marks of one channel land in one group regardless of how the aid was named;
    ``display_name`` is the channel part as charted ("Potomac River Channel").
    """
    if not name or not isinstance(name, str):
        return None
    text = " ".join(name.strip().split())
    m = _US_MARK_RE.match(text) or _EU_MARK_RE.match(text)
    if not m:
        b = _BARE_MARK_RE.match(text)
        if not b:
            return None
        return BARE_NUMBER_KEY, int(b.group("num")), b.group("suf").upper(), BARE_NUMBER_KEY
    display = m.group("chan").strip()
    words = display.lower().split()
    while len(words) > 1 and words[-1] in _STRIP_TRAILING:
        words.pop()
    chan = " ".join(words)
    if not chan:
        return None
    return chan, int(m.group("num")), m.group("suf").upper(), display


# ----------------------------------------------------------------------------- geometry helpers

def _flatten_lines(geom) -> List[LineString]:
    out: List[LineString] = []
    if geom is None or geom.is_empty:
        return out
    if geom.geom_type == "LineString":
        out.append(geom)
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            out.extend(_flatten_lines(g))
    return out


def _flatten_polygons(geom) -> List[Polygon]:
    out: List[Polygon] = []
    if geom is None or geom.is_empty:
        return out
    if geom.geom_type == "Polygon":
        out.append(geom)
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            out.extend(_flatten_polygons(g))
    return out


def _geom_array(geoms: Sequence) -> np.ndarray:
    arr = np.empty(len(geoms), dtype=object)
    for i, g in enumerate(geoms):
        arr[i] = g
    return arr


def _key(x: float, y: float) -> Tuple[float, float]:
    return (round(x, 2), round(y, 2))


def voronoi_graph(poly: Polygon, step_m: float) -> nx.Graph:
    """Voronoi edges of the densified boundary that lie inside ``poly`` as a graph.

    Node = rounded endpoint coordinate, edge attributes ``w`` (length) and ``coords``
    (vertex list from ``u`` to ``v``). The union of these edges approximates the
    polygon's medial axis plus one short spoke per boundary vertex (pruned later).
    """
    boundary = shapely.segmentize(poly.boundary, step_m)
    vd = voronoi_diagram(boundary, edges=True)
    lines = _flatten_lines(vd)
    G = nx.Graph()
    if not lines:
        return G
    arr = _geom_array(lines)
    shapely.prepare(poly)  # in-place; speeds up the vectorised covers() below
    inside = shapely.covers(poly, arr)
    for line, ok in zip(lines, inside):
        if not ok:
            continue
        coords = list(line.coords)
        u, v = _key(*coords[0]), _key(*coords[-1])
        if u == v:
            continue
        w = line.length
        if G.has_edge(u, v) and G[u][v]["w"] <= w:
            continue
        G.add_edge(u, v, w=w, coords=coords)
    return G


def _edge_coords(G: nx.Graph, u, v) -> List[Tuple[float, float]]:
    coords = G[u][v]["coords"]
    return coords if _key(*coords[0]) == u else list(reversed(coords))


def path_to_line(G: nx.Graph, path: Sequence) -> Optional[LineString]:
    pts: List[Tuple[float, float]] = []
    for u, v in zip(path[:-1], path[1:]):
        seg = _edge_coords(G, u, v)
        if pts:
            seg = seg[1:]
        pts.extend(seg)
    return LineString(pts) if len(pts) >= 2 else None


def _nearest_node(G: nx.Graph, pt: Point):
    nodes = list(G.nodes)
    if not nodes:
        return None
    xy = np.array(nodes, dtype=float)
    d = np.hypot(xy[:, 0] - pt.x, xy[:, 1] - pt.y)
    return nodes[int(np.argmin(d))]


def prune_spurs(G: nx.Graph, min_len_m: float) -> nx.Graph:
    """Remove leaf branches shorter than ``min_len_m`` that end at a junction.

    All qualifying branches of one sweep are removed together (a rectangle's two
    corner spurs at the same end must go at once, otherwise the survivor becomes
    part of a leaf-to-leaf path and is kept). Repeats until stable. A branch is the
    walk from a degree-1 node through degree-2 nodes up to the first node of
    degree >= 3; a leaf-to-leaf graph (no junction) is never pruned, so a plain
    corridor keeps its one axis.
    """
    G = G.copy()
    changed = True
    while changed and G.number_of_edges() > 0:
        changed = False
        doomed = set()
        for leaf in [n for n in G.nodes if G.degree(n) == 1]:
            walk = [leaf]
            prev, cur = None, leaf
            length = 0.0
            while True:
                nbrs = [n for n in G.neighbors(cur) if n != prev]
                if not nbrs:
                    break  # other leaf: leaf-to-leaf path
                nxt = nbrs[0]
                length += G[cur][nxt]["w"]
                prev, cur = cur, nxt
                if G.degree(cur) != 2:
                    break
                walk.append(cur)
            if G.degree(cur) >= 3 and length < min_len_m:
                doomed.update(walk)
        if doomed and len(doomed) < G.number_of_nodes():
            G.remove_nodes_from(doomed)
            changed = True
    G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
    return G


def graph_to_polylines(G: nx.Graph) -> List[LineString]:
    """Contract degree-2 chains into polylines between junctions/leaves."""
    lines: List[LineString] = []
    seen = set()
    starts = [n for n in G.nodes if G.degree(n) != 2]
    for s in starts:
        for nbr in G.neighbors(s):
            if (s, nbr) in seen:
                continue
            pts = list(_edge_coords(G, s, nbr))
            seen.add((s, nbr)); seen.add((nbr, s))
            prev, cur = s, nbr
            while G.degree(cur) == 2:
                nxt = [n for n in G.neighbors(cur) if n != prev][0]
                if (cur, nxt) in seen:
                    break
                pts.extend(_edge_coords(G, cur, nxt)[1:])
                seen.add((cur, nxt)); seen.add((nxt, cur))
                prev, cur = cur, nxt
            if len(pts) >= 2:
                lines.append(LineString(pts))
    # pure cycles (every node degree 2) have no start node; ignore them.
    return lines


def longest_path_line(G: nx.Graph) -> Optional[LineString]:
    """Graph diameter path (two Dijkstra sweeps) of the largest component."""
    if G.number_of_edges() == 0:
        return None
    comp = max(nx.connected_components(G), key=len)
    H = G.subgraph(comp)
    src = next(iter(H))
    d = nx.single_source_dijkstra_path_length(H, src, weight="w")
    a = max(d, key=d.get)
    d = nx.single_source_dijkstra_path_length(H, a, weight="w")
    b = max(d, key=d.get)
    return path_to_line(H, nx.dijkstra_path(H, a, b, weight="w"))


def extend_to_boundary(line: LineString, poly: Polygon, reach_m: float,
                       extend_start: bool = True, extend_end: bool = True) -> LineString:
    """Extend the free ends along their last segment direction until the boundary.

    Only leaf ends are extended (a polyline ending at a junction of a branched
    skeleton must keep its shared junction vertex).
    """
    coords = list(line.coords)
    if len(coords) < 2:
        return line

    def _ext(p0, p1):
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L == 0:
            return None
        ray = LineString([p1, (p1[0] + dx / L * reach_m, p1[1] + dy / L * reach_m)])
        hit = ray.intersection(poly.boundary)
        if hit.is_empty:
            return None
        pts = [g for g in _flatten_points(hit)]
        if not pts:
            return None
        best = min(pts, key=lambda q: q.distance(Point(p1)))
        return (best.x, best.y)

    tail = _ext(coords[1], coords[0]) if extend_start else None
    head = _ext(coords[-2], coords[-1]) if extend_end else None
    if tail is not None:
        coords.insert(0, tail)
    if head is not None:
        coords.append(head)
    return LineString(coords)


def _flatten_points(geom) -> List[Point]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Point":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_flatten_points(g))
        return out
    if geom.geom_type == "LineString":
        return [Point(c) for c in geom.coords]
    return []


def polygon_skeleton(poly: Polygon, step_m: float, prune_m: float, reach_m: float) -> List[LineString]:
    """Medial axis of a (channel-shaped) polygon as pruned polylines with ends extended."""
    G0 = voronoi_graph(poly, step_m)
    if G0.number_of_edges() == 0:
        return []
    comp = max(nx.connected_components(G0), key=len)
    G0 = G0.subgraph(comp).copy()
    G = prune_spurs(G0, prune_m)
    lines = graph_to_polylines(G) if G.number_of_edges() else []
    leaves = {n for n in G.nodes if G.degree(n) == 1}
    if not lines:
        lp = longest_path_line(G0)
        lines = [lp] if lp is not None else []
        leaves = {_key(*lp.coords[0]), _key(*lp.coords[-1])} if lp is not None else set()
    out = []
    for l in lines:
        ls = l.simplify(1.0)
        out.append(extend_to_boundary(ls, poly, reach_m,
                                      extend_start=_key(*l.coords[0]) in leaves,
                                      extend_end=_key(*l.coords[-1]) in leaves))
    return out


def mean_width_m(poly: Polygon) -> float:
    """~width of a corridor-shaped polygon (2·area/perimeter → w for a long strip)."""
    if poly.length == 0:
        return 0.0
    return 2.0 * poly.area / poly.length


def aspect_ratio(poly: Polygon) -> float:
    mrr = poly.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        return 1.0
    cs = list(mrr.exterior.coords)
    e = sorted(Point(cs[i]).distance(Point(cs[i + 1])) for i in range(4))
    return e[-1] / e[0] if e[0] > 0 else float("inf")


# ----------------------------------------------------------------------------- marks / chains

@dataclass
class Mark:
    name: str
    key: str
    num: int
    suf: str
    catlam: Optional[int]
    kind: str            # "buoy" | "beacon" | "safe_water"
    cscl: Optional[int]
    pt: Point            # metric
    display: str = ""    # channel part of the name as charted

    @property
    def lateral(self) -> bool:
        return self.catlam in (CATLAM_PORT, CATLAM_STARBOARD)


@dataclass
class Anchor:
    """One estimated channel-centre point.

    ``marks`` are the one or two marks it was derived from; ``is_gate`` when the two
    marks are of opposite hand (their midpoint is on the centre by construction);
    ``half_width`` is the local half channel width estimate in metres.
    """
    pt: Point
    marks: List[Mark]
    is_gate: bool
    half_width: float = 0.0


@dataclass
class Chain:
    key: str
    name: str
    marks: List[Mark]
    anchors: List[Anchor] = field(default_factory=list)


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (list, tuple, np.ndarray)):
        v = v[0] if len(v) else None
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(str(v).strip().strip("[]'\""))
        except ValueError:
            return None


def load_marks(lateral: gpd.GeoDataFrame, safe: gpd.GeoDataFrame) -> Tuple[List[Mark], int, int]:
    """Build Mark objects (metric CRS input). Returns (marks, n_unparsed, n_unnamed)."""
    marks: List[Mark] = []
    unparsed = unnamed = 0
    for gdf, default_kind in ((lateral, None), (safe, "safe_water")):
        if gdf is None or gdf.empty:
            continue
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            pt = geom if geom.geom_type == "Point" else geom.representative_point()
            name = row.get("OBJNAM")
            parsed = parse_mark_name(name)
            if parsed is None:
                if name and isinstance(name, str) and name.strip():
                    unparsed += 1
                else:
                    unnamed += 1
                continue
            key, num, suf, display = parsed
            objl = str(row.get("src_objl") or "").upper()
            kind = default_kind or ("beacon" if objl.startswith("BCN") else "buoy")
            catlam = None if kind == "safe_water" else _to_int(row.get("CATLAM"))
            cscl = _to_int(row.get("src_cscl"))
            marks.append(Mark(str(name).strip(), key, num, suf, catlam, kind, cscl, pt, display))
    return marks, unparsed, unnamed


def dedupe_marks(marks: List[Mark], tol_m: float = 60.0) -> List[Mark]:
    """One mark per (channel, number, suffix) within ``tol_m``: keep the largest-scale
    cell's copy (smallest src_cscl). Overlapping usage bands and seasonal ("Ice")
    replacements chart the same aid more than once."""
    by = collections.defaultdict(list)
    for m in marks:
        by[(m.key, m.num, m.suf)].append(m)
    out: List[Mark] = []
    for group in by.values():
        group.sort(key=lambda m: (m.cscl if m.cscl is not None else 10**9, m.name))
        kept: List[Mark] = []
        for m in group:
            if any(m.pt.distance(k.pt) <= tol_m for k in kept):
                continue
            kept.append(m)
        out.extend(kept)
    return out


def cluster_marks(marks: List[Mark], link_m: float) -> List[List[Mark]]:
    """Single-linkage clusters (same channel name can recur in distant places)."""
    if not marks:
        return []
    xy = np.array([[m.pt.x, m.pt.y] for m in marks])
    tree = STRtree([m.pt for m in marks])
    parent = list(range(len(marks)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, m in enumerate(marks):
        for j in tree.query(m.pt.buffer(link_m)):
            j = int(j)
            if j != i and math.hypot(*(xy[i] - xy[j])) <= link_m:
                parent[find(i)] = find(j)
    groups = collections.defaultdict(list)
    for i, m in enumerate(marks):
        groups[find(i)].append(m)
    return list(groups.values())


def order_marks(marks: List[Mark]) -> List[Mark]:
    return sorted(marks, key=lambda m: (m.num, m.suf))


def _unit(p: Point, q: Point) -> Tuple[float, float]:
    dx, dy = q.x - p.x, q.y - p.y
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L > 0 else (0.0, 0.0)


def center_chain(seq: List[Mark], default_half_width_m: float) -> List[Anchor]:
    """Channel-centre estimate from an ordered mark sequence.

    Consecutive lateral marks of opposite hand straddle the channel: their midpoint
    is a centre point and their across-channel separation gives the local width.
    Consecutive marks of the same hand lie on one edge: their midpoint is pushed
    toward the channel by the locally estimated half width (nearest opposite-hand
    pairs, else the chain median, else ``default_half_width_m``). Non-lateral marks
    (safe-water, preferred-channel) are centre points as they are. Directions come
    from the centre sequence itself, so an alternating-side chain (marks 3-4 km
    apart in a wide river) yields a straight centre line, not a zigzag.
    """
    raw: List[Tuple[Point, List[Mark], bool]] = []
    prev: Optional[Mark] = None
    for m in seq:
        if not m.lateral:
            raw.append((m.pt, [m], False))
            continue
        if prev is not None:
            mid = Point((prev.pt.x + m.pt.x) / 2.0, (prev.pt.y + m.pt.y) / 2.0)
            raw.append((mid, [prev, m], prev.catlam != m.catlam))
        prev = m
    if not raw:
        return []
    n = len(raw)

    def direction(i: int) -> Tuple[float, float]:
        a = raw[max(0, i - 1)][0]
        b = raw[min(n - 1, i + 1)][0]
        d = _unit(a, b)
        if d == (0.0, 0.0) and n > 1:
            d = _unit(raw[0][0], raw[-1][0])
        return d

    # local width from opposite-hand pairs: separation across the chain direction
    widths: List[Optional[float]] = []
    for i, (pt, marks, gate) in enumerate(raw):
        if gate:
            dx, dy = direction(i)
            a, b = marks
            across = abs((b.pt.x - a.pt.x) * dy - (b.pt.y - a.pt.y) * dx)
            widths.append(float(np.clip(across / 2.0, 20.0, 1500.0)))
        else:
            widths.append(None)
    known = [w for w in widths if w is not None]
    chain_default = float(np.median(known)) if known else default_half_width_m
    anchors: List[Anchor] = []
    for i, (pt, marks, gate) in enumerate(raw):
        hw = widths[i]
        if hw is None:
            near = [widths[j] for j in range(max(0, i - 3), min(n, i + 4)) if widths[j] is not None]
            hw = float(np.median(near)) if near else chain_default
        if len(marks) == 2 and not gate:
            sx, sy = shoal_normal(direction(i), marks[0].catlam)
            pt = Point(pt.x - sx * hw, pt.y - sy * hw)
        anchors.append(Anchor(pt, list(marks), gate, hw))
    return anchors


def default_half_width_m(seq: List[Mark], floor_m: float = 50.0, cap_m: float = 500.0) -> float:
    """Fallback half width for a chain without opposite-hand pairs: a quarter of the
    median mark spacing (marks are usually spaced a few channel widths apart)."""
    if len(seq) < 2:
        return floor_m
    d = [seq[i].pt.distance(seq[i + 1].pt) for i in range(len(seq) - 1)]
    return float(np.clip(0.25 * float(np.median(d)), floor_m, cap_m))


def split_anchors(anchors: List[Anchor], max_gap_m: float, max_turn_deg: float) -> List[List[Anchor]]:
    """Split an anchor sequence at long gaps and at sharp turns."""
    chains: List[List[Anchor]] = []
    cur: List[Anchor] = []
    for i, a in enumerate(anchors):
        if cur:
            gap = cur[-1].pt.distance(a.pt)
            if gap > max_gap_m:
                chains.append(cur); cur = []
        if cur and len(cur) >= 2:
            d0 = _unit(cur[-2].pt, cur[-1].pt)
            d1 = _unit(cur[-1].pt, a.pt)
            cos = max(-1.0, min(1.0, d0[0] * d1[0] + d0[1] * d1[1]))
            if math.degrees(math.acos(cos)) > max_turn_deg:
                chains.append(cur); cur = [cur[-1]]
        cur.append(a)
    if cur:
        chains.append(cur)
    return chains


def chain_direction(anchors: List[Anchor], idx: int) -> Tuple[float, float]:
    prev = anchors[max(0, idx - 1)].pt
    nxt = anchors[min(len(anchors) - 1, idx + 1)].pt
    return _unit(prev, nxt)


def shoal_normal(direction: Tuple[float, float], catlam: int) -> Tuple[float, float]:
    """Unit normal pointing from a lateral mark toward the shoal it guards.

    Direction of buoyage = increasing number. A port-hand mark is left of the
    channel (the shoal is on its left), a starboard-hand mark is right of it.
    """
    dx, dy = direction
    left = (-dy, dx)
    right = (dy, -dx)
    return left if catlam == CATLAM_PORT else right


def build_corridor(chain: List[Anchor], water: Polygon | MultiPolygon, r_fn, wall_buffer_m: float
                   ) -> Tuple[Optional[Polygon], LineString, List[float]]:
    """Corridor polygon for one chain: per-segment buffer of the centre polyline
    (radius from the local half width), clipped to charted water, minus a wall on
    the shoal side of every lateral mark so the axis passes each mark on its
    correct side. Returns (corridor, centre line, per-segment radius list).
    """
    pts = [a.pt for a in chain]
    naive = LineString([(p.x, p.y) for p in pts])
    radii: List[float] = []
    parts = []
    for i in range(len(pts) - 1):
        seg = LineString([pts[i], pts[i + 1]])
        r = r_fn(max(chain[i].half_width, chain[i + 1].half_width))
        radii.append(r)
        parts.append(seg.buffer(r))
    corridor = unary_union(parts)
    if water is not None and not water.is_empty:
        corridor = corridor.intersection(water)
    walls = []
    done = set()
    for i, a in enumerate(chain):
        r_local = max(radii[max(0, i - 1)], radii[min(len(radii) - 1, i)]) if radii else 100.0
        d = chain_direction(chain, i)
        for m in a.marks:
            if not m.lateral or id(m) in done:
                continue
            done.add(id(m))
            nx_, ny_ = shoal_normal(d, m.catlam)
            far = (m.pt.x + nx_ * (r_local + 50.0), m.pt.y + ny_ * (r_local + 50.0))
            walls.append(LineString([(m.pt.x, m.pt.y), far]).buffer(wall_buffer_m))
    if walls:
        corridor = corridor.difference(unary_union(walls))
    polys = _flatten_polygons(corridor)
    if not polys:
        return None, naive, radii
    return MultiPolygon(polys) if len(polys) > 1 else polys[0], naive, radii


def chain_pins(chain: List[Anchor], radii: List[float]) -> Tuple[Point, Point, float]:
    """Start/end centre points the derived axis is pinned to, and the mean radius."""
    r_mean = float(np.mean(radii)) if radii else 100.0
    return chain[0].pt, chain[-1].pt, r_mean


def connected_component(corridor, start: Point, end: Point, reach_m: float) -> Optional[Polygon]:
    """The corridor polygon holding ``start`` if it also reaches ``end``, else None."""
    polys = _flatten_polygons(corridor)
    if not polys:
        return None
    comp = None
    for p in polys:
        if p.distance(start) < 1e-6 or p.contains(start):
            comp = p
            break
    if comp is None:
        comp = min(polys, key=lambda p: p.distance(start))
    if comp.distance(end) > reach_m:
        return None
    return comp


def tighten_to_depth(corridor, depth: "DepthSampler", min_drval1: float, start: Point, end: Point,
                     reach_m: float):
    """Restrict the corridor to charted water at least ``min_drval1`` deep, if that
    still connects the chain's ends (the deepest continuous water inside the
    buoyed corridor is where the channel is). Returns (corridor, tightened?)."""
    deep = depth.water_at_least(corridor, min_drval1)
    if deep is None:
        return corridor, False
    tight = corridor.intersection(deep)
    polys = _flatten_polygons(tight)
    if not polys:
        return corridor, False
    tight = MultiPolygon(polys) if len(polys) > 1 else polys[0]
    if connected_component(tight, start, end, reach_m) is None:
        return corridor, False
    return tight, True


def corridor_centerline(corridor, chain: List[Anchor], naive: LineString, radii: List[float],
                        hug: float = 1.0) -> Tuple[Optional[LineString], Optional[str]]:
    """Medial axis of the corridor between the chain's first and last anchor.

    Edge weights are length × (1 + hug·distance-to-chain/r) so the path stays near
    the marks where the water allows it, while walls and land keep it on the
    correct side. Returns (line, reason-if-failed).
    """
    start, end, r_mean = chain_pins(chain, radii)
    comp = connected_component(corridor, start, end, r_mean)
    if comp is None:
        return None, "corridor_disconnected"
    step = float(np.clip(r_mean / 8.0, 8.0, 60.0))
    comp = comp.simplify(step / 4.0, preserve_topology=True)
    if comp.geom_type != "Polygon":
        polys = _flatten_polygons(comp)
        if not polys:
            return None, "corridor_empty"
        comp = max(polys, key=lambda p: p.area)
    G = voronoi_graph(comp, step)
    if G.number_of_edges() == 0:
        return None, "voronoi_empty"
    # The medial axis is the largest connected piece; boundary spokes that lost their
    # inner end to the polygon test form tiny fragments that must not capture a pin.
    G = G.subgraph(max(nx.connected_components(G), key=len)).copy()
    if hug > 0:
        mids = [Point((c[0][0] + c[-1][0]) / 2.0, (c[0][1] + c[-1][1]) / 2.0)
                for _, _, c in G.edges(data="coords")]
        dist = shapely.distance(naive, _geom_array(mids))
        for (u, v), dd in zip(G.edges(), dist):
            G[u][v]["w"] = G[u][v]["w"] * (1.0 + hug * float(dd) / r_mean)
    s = _nearest_node(G, start)
    t = _nearest_node(G, end)
    try:
        path = nx.dijkstra_path(G, s, t, weight="w")
    except nx.NetworkXNoPath:
        return None, "corridor_disconnected"
    line = path_to_line(G, path)
    if line is None or line.length == 0:
        return None, "voronoi_empty"
    return line.simplify(2.0), None


# ----------------------------------------------------------------------------- validation

class DepthSampler:
    def __init__(self, depare_m: gpd.GeoDataFrame):
        self.ok = depare_m is not None and not depare_m.empty and "DRVAL1" in depare_m.columns
        if self.ok:
            self.geoms = list(depare_m.geometry.values)
            self.vals = pd.to_numeric(depare_m["DRVAL1"], errors="coerce").to_numpy()
            self.tree = STRtree(self.geoms)

    def water_at_least(self, geom, min_drval1: float):
        """Union of DEPARE polygons near ``geom`` whose DRVAL1 >= ``min_drval1``."""
        if not self.ok:
            return None
        parts = []
        for i in self.tree.query(geom):
            i = int(i)
            v = self.vals[i]
            if not np.isnan(v) and v >= min_drval1:
                parts.append(shapely.make_valid(self.geoms[i]))
        return unary_union(parts) if parts else None

    def at(self, pt: Point) -> Optional[float]:
        if not self.ok:
            return None
        best = None
        for i in self.tree.query(pt):
            i = int(i)
            v = self.vals[i]
            if np.isnan(v) or not self.geoms[i].contains(pt):
                continue
            best = v if best is None else max(best, v)  # deepest containing candidate
        return best

    def along(self, line: LineString, step_m: float) -> Tuple[Optional[float], float]:
        """(median DRVAL1 along the line, share of samples with no DEPARE)."""
        if not self.ok or line.length == 0:
            return None, 1.0
        n = int(line.length // step_m) + 2
        vals = [self.at(line.interpolate(min(i * step_m, line.length))) for i in range(n)]
        have = [v for v in vals if v is not None]
        miss = 1.0 - len(have) / len(vals)
        return (float(np.median(have)) if have else None), miss


class LayerIndex:
    """Bbox-indexed access to big polygon layers (coastal_water, land)."""

    def __init__(self, gdf_m: Optional[gpd.GeoDataFrame]):
        self.geoms = [] if gdf_m is None or gdf_m.empty else list(gdf_m.geometry.values)
        self.tree = STRtree(self.geoms) if self.geoms else None

    def union_near(self, geom, margin_m: float = 0.0):
        if self.tree is None:
            return None
        q = geom.buffer(margin_m) if margin_m else geom
        idx = self.tree.query(q)
        if len(idx) == 0:
            return None
        parts = [self.geoms[int(i)] for i in idx]
        return unary_union([shapely.make_valid(p) for p in parts])

    def crossing_length(self, line: LineString) -> float:
        """Length of ``line`` inside any polygon of this layer."""
        if self.tree is None:
            return 0.0
        total = 0.0
        for i in self.tree.query(line):
            g = self.geoms[int(i)]
            if g.intersects(line):
                total += line.intersection(g).length
        return total


def clip_to_water(line: LineString, water, outside_tolerance: float = 0.05,
                  min_part_m: float = 20.0) -> List[LineString]:
    """Parts of ``line`` inside ``water``.

    A line almost entirely inside (sliver gaps between adjacent chart polygons) is
    returned whole; otherwise the merged inside parts. A dredged creek that a
    coarser cell covers with land keeps its water-only fragments rather than
    losing the whole axis.
    """
    if water is None:
        return [line]
    outside = line.difference(water)
    if outside.is_empty or outside.length <= outside_tolerance * line.length:
        return [line]
    inter = shapely.line_merge(line.intersection(water))
    return [p for p in _flatten_lines(inter) if p.length >= min_part_m]


def snap_end_to_lines(part: LineString, lines: List[LineString], max_m: float, land: "LayerIndex"
                      ) -> LineString:
    """Join a cut end of a de-duplicated axis part to the nearest vertex of the
    higher-tier line it was cut against (within ``max_m``, not across land), so the
    two share an exact node in the routing graph."""
    coords = list(part.coords)
    if len(coords) < 2 or not lines:
        return part
    for end in (0, -1):
        pt = Point(coords[end])
        best = None
        for l in lines:
            if l.distance(pt) > max_m:
                continue
            for c in l.coords:
                d = pt.distance(Point(c))
                if d <= max_m and (best is None or d < best[0]):
                    best = (d, c)
        if best is None or best[0] < 1.0:
            continue
        seg = LineString([coords[end], best[1]])
        if land.crossing_length(seg) > LAND_CROSSING_TOLERANCE_M:
            continue
        if end == 0:
            coords.insert(0, best[1])
        else:
            coords.append(best[1])
    return LineString(coords)


def subtract_coverage(line: LineString, coverage_lines: List[LineString], buffer_m: float,
                      min_len_m: float) -> Tuple[List[LineString], float]:
    """Remove the parts of ``line`` within ``buffer_m`` of any coverage line."""
    if not coverage_lines:
        return [line], 0.0
    cov = unary_union([l.buffer(buffer_m) for l in coverage_lines])
    rest = line.difference(cov)
    parts = [p for p in _flatten_lines(rest) if p.length >= min_len_m]
    removed = line.length - sum(p.length for p in parts)
    return parts, removed


# ----------------------------------------------------------------------------- driver

@dataclass
class Params:
    max_mark_gap_m: float = 8000.0
    max_turn_deg: float = 120.0
    min_marks: int = 3
    min_length_m: float = 200.0
    corridor_fraction: float = 1.5      # corridor radius = fraction × local half width + 50 m
    corridor_min_r_m: float = 100.0
    corridor_max_r_m: float = 1500.0
    cluster_link_m: float = 8000.0
    dedup_min_m: float = 50.0
    dedup_max_m: float = 250.0
    polygon_min_aspect: float = 3.0
    sample_step_m: float = 100.0
    depth_slack_m: float = 0.5
    hug: float = 1.0
    min_confidence: float = 0.0
    depth_tighten: bool = True
    simplify_m: float = 5.0             # Douglas-Peucker on every output axis (the pipeline re-densifies)


class ChannelAxisDeriver:
    def __init__(self, input_dir: str, output_dir: Optional[str] = None, params: Optional[Params] = None):
        self.input_dir = input_dir
        self.output_dir = output_dir or input_dir
        self.p = params or Params()
        self.gdfs: dict = {}
        self.crs_metric = None
        self.axes: List[dict] = []
        self.rejected: List[dict] = []
        self.stats: dict = collections.OrderedDict()
        self.marks: List[Mark] = []
        self._tier2_lines: List[LineString] = []
        self._tier2_tree_cache: Optional[STRtree] = None
        self._last_reason: Optional[str] = None

    # -- loading -------------------------------------------------------------
    def load(self):
        t0 = time.time()
        for key, fname in LAYER_FILES.items():
            path = os.path.join(self.input_dir, fname)
            if os.path.exists(path):
                gdf = gpd.read_file(path)
                if gdf.crs is None:
                    gdf = gdf.set_crs("EPSG:4326")
                elif gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs("EPSG:4326")
                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
                self.gdfs[key] = gdf
                logger.info("Loaded %s: %d features", fname, len(gdf))
            else:
                self.gdfs[key] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                logger.warning("Missing %s (treated as empty)", fname)
        ref = [self.gdfs[k] for k in ("lateral_marks", "fairways", "dredged_areas", "coastal_water")
               if not self.gdfs[k].empty]
        if not ref:
            raise SystemExit("No marks, fairways, dredged areas or coastal water found -- nothing to derive.")
        self.crs_metric = ref[0].estimate_utm_crs()
        for key in list(self.gdfs):
            gdf = self.gdfs[key]
            self.gdfs[key] = gdf.to_crs(self.crs_metric) if not gdf.empty else gdf
        self.water = LayerIndex(self.gdfs["coastal_water"])
        self.land = LayerIndex(self.gdfs["land"])
        self.depth = DepthSampler(self.gdfs["depth_areas"])
        self.tier1_lines: List[LineString] = []
        for g in self.gdfs["inland_waterways"].geometry.values:
            self.tier1_lines.extend(_flatten_lines(g))
        self.tier1_tree = STRtree(self.tier1_lines) if self.tier1_lines else None
        self.stats["input"] = {k: int(len(v)) for k, v in self.gdfs.items()}
        self.stats["crs_metric"] = str(self.crs_metric)
        logger.info("Loaded layers in %.1fs (metric CRS %s)", time.time() - t0, self.crs_metric)

    def navigable_water(self, geom, margin_m: float):
        """Charted water near ``geom`` with charted land removed. Land and water
        polygons from cells of different scale overlap by up to hundreds of metres;
        the pipeline's raster does the same subtraction (land mask wins)."""
        water = self.water.union_near(geom, margin_m)
        if water is None:
            return None
        land = self.land.union_near(geom, margin_m)
        if land is not None:
            water = water.difference(land)
        polys = _flatten_polygons(water)
        if not polys:
            return None
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]

    def _coverage(self, tree: Optional[STRtree], lines: List[LineString], geom, buffer_m: float) -> List[LineString]:
        if tree is None:
            return []
        return [lines[int(i)] for i in tree.query(geom.buffer(buffer_m))]

    # -- tier 2 --------------------------------------------------------------
    def derive_polygon_axes(self):
        t0 = time.time()
        p = self.p
        polys = []
        for key in ("fairways", "dredged_areas"):
            gdf = self.gdfs[key]
            if gdf.empty:
                continue
            for _, row in gdf.iterrows():
                g = shapely.make_valid(row.geometry)
                for poly in _flatten_polygons(g):
                    if poly.area > 0:
                        polys.append((poly, row))
        if not polys:
            self.stats["tier2"] = {"components": 0}
            return
        union = unary_union([g for g, _ in polys])
        components = _flatten_polygons(union)
        member_tree = STRtree([g for g, _ in polys])
        marks_tree = STRtree([m.pt for m in self.marks]) if self.marks else None
        n_ok = n_blob = n_rej = 0
        km = 0.0
        for ci, comp in enumerate(components):
            members = [polys[int(i)][1] for i in member_tree.query(comp) if polys[int(i)][0].intersects(comp)]
            names = [str(r.get("OBJNAM")) for r in members if r.get("OBJNAM")]
            name = collections.Counter(names).most_common(1)[0][0] if names else ""
            drvals = pd.to_numeric(pd.Series([r.get("DRVAL1") for r in members]), errors="coerce").dropna()
            drval1 = float(drvals.min()) if len(drvals) else None
            objls = sorted({str(r.get("src_objl")) for r in members if r.get("src_objl")})
            cscls = pd.to_numeric(pd.Series([r.get("src_cscl") for r in members]), errors="coerce").dropna()
            cscl = int(cscls.min()) if len(cscls) else None
            has_marks = bool(marks_tree is not None and any(
                comp.contains(self.marks[int(i)].pt) for i in marks_tree.query(comp)))
            width = mean_width_m(comp)
            if aspect_ratio(comp) < p.polygon_min_aspect and not has_marks:
                n_blob += 1
                self._reject(comp.representative_point().buffer(1).exterior, 2, name, "blob_polygon",
                             {"aspect": round(aspect_ratio(comp), 2)})
                continue
            step = float(np.clip(width / 6.0, 5.0, 30.0))
            comp_s = comp.simplify(step / 4.0, preserve_topology=True)
            comp_s = max(_flatten_polygons(comp_s), key=lambda q: q.area) if comp_s.geom_type != "Polygon" else comp_s
            try:
                lines = polygon_skeleton(comp_s, step, prune_m=1.5 * width, reach_m=3.0 * width + 50.0)
            except Exception as exc:  # noqa: BLE001 -- per-component isolation
                n_rej += 1
                self._reject(comp.exterior, 2, name, "skeleton_error", {"error": repr(exc)[:200]})
                continue
            if not lines:
                n_rej += 1
                self._reject(comp.exterior, 2, name, "voronoi_empty", {})
                continue
            water = self.navigable_water(comp, 50.0)
            confidence = 0.9 if not (aspect_ratio(comp) < p.polygon_min_aspect) else 0.8
            for li, line in enumerate(lines):
                res = self._finish_axis(line, tier=2, kind="polygon_centerline", name=name,
                                        confidence=confidence, water=water, width_m=width,
                                        extra={"drval1": drval1, "src_objl": ",".join(objls),
                                               "src_cscl": cscl, "component": ci, "part": li,
                                               "n_marks": None, "n_gates": None},
                                        depth_ref=None, coverage_tiers=(1,), part_min_len_m=20.0)
                if res:
                    n_ok += res[0]; km += res[1]
                else:
                    n_rej += 1
        self.stats["tier2"] = {"components": len(components), "axes": n_ok, "km": round(km, 1),
                               "blob_skipped": n_blob, "rejected": n_rej, "seconds": round(time.time() - t0, 1)}
        logger.info("tier 2: %d components -> %d axes (%.1f km), %d blobs skipped, %d rejected [%.1fs]",
                    len(components), n_ok, km, n_blob, n_rej, time.time() - t0)

    # -- tier 3 --------------------------------------------------------------
    def derive_mark_axes(self):
        t0 = time.time()
        p = self.p
        marks = self.marks
        if not marks:
            self.stats["tier3"] = {"chains": 0}
            return
        # Region-wide numbering convention (IALA B: odd = port; IALA A: even = port).
        odd_port = sum(1 for m in marks if m.lateral and (m.num % 2 == 1) == (m.catlam == CATLAM_PORT))
        lateral_n = sum(1 for m in marks if m.lateral)
        convention_odd_port = odd_port >= lateral_n / 2.0 if lateral_n else True
        self.stats["parity_convention"] = "odd=port (IALA B)" if convention_odd_port else "even=port (IALA A)"
        by_key = collections.defaultdict(list)
        for m in marks:
            by_key[m.key].append(m)
        n_chains = n_ok = n_rej = 0
        km = 0.0
        reasons = collections.Counter()
        for key, group in by_key.items():
            for cluster in cluster_marks(group, p.cluster_link_m):
                seq = order_marks(cluster)
                anchors = center_chain(seq, default_half_width_m(seq))
                for chain in split_anchors(anchors, p.max_mark_gap_m, p.max_turn_deg):
                    n_chains += 1
                    chain_marks = list({id(m): m for a in chain for m in a.marks}.values())
                    name = collections.Counter(m.display or m.key for m in chain_marks).most_common(1)[0][0]
                    if len(chain_marks) < p.min_marks or len(chain) < 2:
                        reasons["too_few_marks"] += 1
                        if len(chain_marks) >= 2:  # singletons are noise in the QA layer
                            self._reject(LineString([(a.pt.x, a.pt.y) for a in chain]) if len(chain) > 1
                                         else chain[0].pt.buffer(1).exterior, 3, name, "too_few_marks",
                                         {"n_marks": len(chain_marks)})
                        continue
                    ok, reason = self._derive_one_chain(chain, chain_marks, name, convention_odd_port)
                    if ok:
                        n_ok += ok[0]; km += ok[1]
                    else:
                        n_rej += 1
                        reasons[reason] += 1
        self.stats["tier3"] = {"chains": n_chains, "axes": n_ok, "km": round(km, 1), "rejected": n_rej,
                               "reasons": dict(reasons), "seconds": round(time.time() - t0, 1)}
        logger.info("tier 3: %d chains -> %d axes (%.1f km), %d rejected %s [%.1fs]",
                    n_chains, n_ok, km, n_rej, dict(reasons), time.time() - t0)

    def _derive_one_chain(self, chain: List[Anchor], chain_marks: List[Mark], name: str,
                          convention_odd_port: bool):
        p = self.p

        def r_fn(half_width):
            return float(np.clip(p.corridor_fraction * half_width + 50.0, p.corridor_min_r_m, p.corridor_max_r_m))

        naive_line = LineString([(a.pt.x, a.pt.y) for a in chain])
        r_max = max(r_fn(a.half_width) for a in chain)
        water = self.navigable_water(naive_line, r_max + 100.0)
        if water is None:
            self._reject(naive_line, 3, name, "no_water", {"n_marks": len(chain_marks)})
            return None, "no_water"
        r_mean_guess = float(np.mean([r_fn(a.half_width) for a in chain]))
        wall_buffer = float(np.clip(r_mean_guess / 20.0, 5.0, 30.0))
        mark_depths = [self.depth.at(m.pt) for m in chain_marks]
        mark_depths = [d for d in mark_depths if d is not None]
        depth_ref = float(np.median(mark_depths)) if mark_depths else None
        tightened = False
        try:
            corridor, naive, radii = build_corridor(chain, water, r_fn, wall_buffer)
            if corridor is None:
                self._reject(naive_line, 3, name, "corridor_empty", {"n_marks": len(chain_marks)})
                return None, "corridor_empty"
            if p.depth_tighten and depth_ref is not None:
                start, end, r_mean = chain_pins(chain, radii)
                corridor, tightened = tighten_to_depth(corridor, self.depth, depth_ref - p.depth_slack_m,
                                                       start, end, r_mean)
            line, reason = corridor_centerline(corridor, chain, naive, radii, hug=p.hug)
        except Exception as exc:  # noqa: BLE001 -- per-chain isolation
            self._reject(naive_line, 3, name, "centerline_error", {"error": repr(exc)[:200]})
            return None, "centerline_error"
        if line is None:
            self._reject(naive_line, 3, name, reason, {"n_marks": len(chain_marks)})
            return None, reason
        n_gates = sum(1 for a in chain if a.is_gate)
        n_pairs = sum(1 for a in chain if len(a.marks) == 2)
        parity_bad = sum(1 for m in chain_marks if m.lateral and
                         ((m.num % 2 == 1) == (m.catlam == CATLAM_PORT)) != convention_odd_port)
        confidence = 0.6
        if n_pairs and n_gates >= 0.5 * n_pairs:
            confidence += 0.1
        if len(chain_marks) >= 8:
            confidence += 0.1
        if parity_bad:
            confidence -= 0.1
        if chain_marks and all(m.key == BARE_NUMBER_KEY for m in chain_marks):
            confidence -= 0.1   # grouped by proximity only, not by a charted channel name
        width = 2.0 * float(np.median([a.half_width for a in chain]))
        res = self._finish_axis(line, tier=3, kind="mark_chain", name=name, confidence=confidence,
                                water=water, width_m=width,
                                extra={"n_marks": len(chain_marks), "n_gates": n_gates,
                                       "drval1": None,
                                       "src_objl": ",".join(sorted({"BCNLAT" if m.kind == "beacon" else
                                                                    "BOYSAW" if m.kind == "safe_water" else "BOYLAT"
                                                                    for m in chain_marks})),
                                       "src_cscl": min((m.cscl for m in chain_marks if m.cscl), default=None),
                                       "parity_mismatch": parity_bad, "depth_tightened": tightened},
                                depth_ref=depth_ref, coverage_tiers=(1, 2))
        if res:
            return res, None
        return None, self._last_reason

    # -- shared finishing: validate, dedup, record ---------------------------
    def _finish_axis(self, line: LineString, tier: int, kind: str, name: str, confidence: float,
                     water, width_m: float, extra: dict, depth_ref: Optional[float],
                     coverage_tiers: Tuple[int, ...], part_min_len_m: Optional[float] = None):
        """Validate, de-duplicate against higher tiers and record one derived axis.

        Returns (n_parts, km) or None (reason in ``self._last_reason``).
        """
        p = self.p
        self._last_reason = None
        # A branched polygon skeleton is a set of polylines meeting at junctions; a
        # short link between two junctions must survive or the network breaks, so
        # tier 2 applies the length floor to the whole component, not per part.
        part_min = p.min_length_m if part_min_len_m is None else part_min_len_m
        water_parts = clip_to_water(line, water)
        if not water_parts or sum(w.length for w in water_parts) < 0.2 * line.length:
            self._reject(line, tier, name, "outside_water", extra)
            self._last_reason = "outside_water"
            return None
        dedup_m = float(np.clip(0.5 * width_m, p.dedup_min_m, p.dedup_max_m))
        direction = self._bearing(line)
        n = 0
        km = 0.0
        kept_parts: List[LineString] = []
        reasons = collections.Counter()
        for wp in water_parts:
            land_m = self.land.crossing_length(wp)
            if land_m > LAND_CROSSING_TOLERANCE_M:
                self._reject(wp, tier, name, "crosses_land", {**extra, "land_m": round(land_m, 1)})
                reasons["crosses_land"] += 1
                continue
            depth_med, miss = self.depth.along(wp, p.sample_step_m)
            depth_checked = depth_med is not None and miss <= 0.5
            conf = confidence
            if tier == 3:
                if not depth_checked:
                    conf -= 0.1
                elif depth_ref is not None and depth_med < depth_ref - p.depth_slack_m:
                    self._reject(wp, tier, name, "shallower_than_marks",
                                 {**extra, "depth_axis": depth_med, "depth_marks": depth_ref})
                    reasons["shallower_than_marks"] += 1
                    continue
            conf = float(np.clip(conf, 0.0, 1.0))
            cov: List[LineString] = []
            if 1 in coverage_tiers:
                cov.extend(self._coverage(self.tier1_tree, self.tier1_lines, wp, dedup_m))
            if 2 in coverage_tiers:
                cov.extend(self._coverage(self._tier2_tree(), self._tier2_lines, wp, dedup_m))
            parts, removed = subtract_coverage(wp, cov, dedup_m, part_min)
            if not parts:
                self._reject(wp, tier, name, "duplicate_of_higher_tier", {**extra, "dedup_m": round(dedup_m)})
                reasons["duplicate_of_higher_tier"] += 1
                continue
            for part in parts:
                if removed > 0:
                    part = snap_end_to_lines(part, cov, 2.0 * dedup_m, self.land)
                if p.simplify_m > 0:
                    # A Voronoi path carries a vertex every few metres; the pipeline sets
                    # its own vertex spacing (--inland-densify-max-segment-m), so leave
                    # only the shape. Endpoints (and thus snapped junctions) survive.
                    part = part.simplify(p.simplify_m, preserve_topology=True)
                if part.length < part_min:
                    continue
                props = collections.OrderedDict(
                    axis_kind=kind, tier=tier, channel_name=name, confidence=round(conf, 2),
                    length_m=round(part.length, 1), direction_deg=round(direction, 1),
                    corridor_width_m=round(width_m, 1),
                    depth_median_m=None if depth_med is None else round(depth_med, 2),
                    depth_checked=bool(depth_checked), dedup_removed_m=round(removed, 1),
                )
                for k, v in extra.items():
                    props[k] = None if v is None or (isinstance(v, float) and math.isnan(v)) else v
                self.axes.append({"geometry": part, **props})
                kept_parts.append(part)
                n += 1
                km += part.length / 1000.0
        if n == 0:
            reason = reasons.most_common(1)[0][0] if reasons else "too_short"
            if reason == "too_short":
                self._reject(line, tier, name, "too_short", extra)
            self._last_reason = reason
            return None
        if tier == 2:
            self._tier2_lines.extend(kept_parts)
            self._tier2_tree_cache = None
        return n, km

    def _tier2_tree(self) -> Optional[STRtree]:
        if not self._tier2_lines:
            return None
        if self._tier2_tree_cache is None:
            self._tier2_tree_cache = STRtree(self._tier2_lines)
        return self._tier2_tree_cache

    @staticmethod
    def _bearing(line: LineString) -> float:
        (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
        return (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360.0) % 360.0

    def _reject(self, geom, tier: int, name: str, reason: str, extra: dict):
        props = {"tier": tier, "channel_name": name, "reason": reason}
        for k, v in (extra or {}).items():
            props[k] = None if v is None or (isinstance(v, float) and math.isnan(v)) else v
        self.rejected.append({"geometry": geom, **props})

    # -- run -----------------------------------------------------------------
    def run(self):
        t0 = time.time()
        self.load()
        raw_marks, unparsed, unnamed = load_marks(self.gdfs["lateral_marks"], self.gdfs["safe_water_marks"])
        self.marks = dedupe_marks(raw_marks)
        self.stats["marks"] = {"raw": len(raw_marks) + unparsed + unnamed, "parsed": len(raw_marks),
                               "unparsed_name": unparsed, "unnamed": unnamed, "deduped": len(self.marks)}
        logger.info("marks: %d parsed (%d unparsed, %d unnamed) -> %d after dedupe",
                    len(raw_marks), unparsed, unnamed, len(self.marks))
        self._tier2_lines = []
        self._tier2_tree_cache = None
        self.derive_polygon_axes()
        self.derive_mark_axes()
        self.write()
        self.stats["seconds_total"] = round(time.time() - t0, 1)
        with open(os.path.join(self.output_dir, OUTPUT_STATS), "w") as fh:
            json.dump(self.stats, fh, indent=2, default=str)
        logger.info("done in %.1fs: %d axes, %d rejected -> %s", time.time() - t0, len(self.axes),
                    len(self.rejected), self.output_dir)

    def _write_layer(self, records: List[dict], path: str):
        if not records:
            _write_empty_collection(path)
            return
        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=self.crs_metric).to_crs("EPSG:4326")
        gdf.to_file(path, driver="GeoJSON")

    def write(self):
        os.makedirs(self.output_dir, exist_ok=True)
        keep = [a for a in self.axes if a["confidence"] >= self.p.min_confidence]
        self._write_layer(keep, os.path.join(self.output_dir, OUTPUT_AXES))
        self._write_layer(self.rejected, os.path.join(self.output_dir, OUTPUT_REJECTED))
        self.stats["output"] = {
            "axes": int(len(keep)), "axes_km": round(sum(a["length_m"] for a in keep) / 1000.0, 1),
            "below_min_confidence": int(len(self.axes) - len(keep)),
            "rejected": int(len(self.rejected)),
            "rejected_by_reason": dict(collections.Counter(r["reason"] for r in self.rejected)),
            "by_kind": dict(collections.Counter(a["axis_kind"] for a in keep)),
        }


def _write_empty_collection(path: str):
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": []}, fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input-dir", required=True, help="Directory of the pipeline GeoJSON layers.")
    ap.add_argument("--output-dir", default=None, help="Where to write outputs (default: input dir).")
    d = Params()
    ap.add_argument("--max-mark-gap-m", type=float, default=d.max_mark_gap_m,
                    help="Split a mark chain at gaps longer than this (default %(default)s).")
    ap.add_argument("--max-turn-deg", type=float, default=d.max_turn_deg,
                    help="Split a mark chain at turns sharper than this (default %(default)s).")
    ap.add_argument("--min-marks", type=int, default=d.min_marks)
    ap.add_argument("--min-length-m", type=float, default=d.min_length_m)
    ap.add_argument("--corridor-fraction", type=float, default=d.corridor_fraction,
                    help="Corridor radius = fraction × local half channel width + 50 m, clamped (default %(default)s).")
    ap.add_argument("--corridor-min-r-m", type=float, default=d.corridor_min_r_m)
    ap.add_argument("--corridor-max-r-m", type=float, default=d.corridor_max_r_m)
    ap.add_argument("--cluster-link-m", type=float, default=d.cluster_link_m,
                    help="Same-named marks farther apart than this are different channels.")
    ap.add_argument("--dedup-min-m", type=float, default=d.dedup_min_m,
                    help="Lower-tier axis parts within max(this, half the channel width, capped by --dedup-max-m) "
                         "of a higher-tier axis are dropped.")
    ap.add_argument("--dedup-max-m", type=float, default=d.dedup_max_m)
    ap.add_argument("--polygon-min-aspect", type=float, default=d.polygon_min_aspect,
                    help="Fairway polygons rounder than this (long/short edge) are skipped unless marks run through them.")
    ap.add_argument("--hug", type=float, default=d.hug,
                    help="How strongly a mark-chain axis is pulled toward the marks (0 = pure medial axis).")
    ap.add_argument("--min-confidence", type=float, default=d.min_confidence,
                    help="Drop derived axes below this confidence from the output layer.")
    ap.add_argument("--simplify-m", type=float, default=d.simplify_m,
                    help="Douglas-Peucker tolerance applied to every output axis (default %(default)s; 0 = off).")
    ap.add_argument("--no-depth-tighten", action="store_true",
                    help="Do not restrict a mark-chain corridor to water at least as deep as the marks stand in.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    params = Params(max_mark_gap_m=args.max_mark_gap_m, max_turn_deg=args.max_turn_deg,
                    min_marks=args.min_marks, min_length_m=args.min_length_m,
                    corridor_fraction=args.corridor_fraction, corridor_min_r_m=args.corridor_min_r_m,
                    corridor_max_r_m=args.corridor_max_r_m, cluster_link_m=args.cluster_link_m,
                    dedup_min_m=args.dedup_min_m, dedup_max_m=args.dedup_max_m,
                    polygon_min_aspect=args.polygon_min_aspect,
                    hug=args.hug, min_confidence=args.min_confidence,
                    depth_tighten=not args.no_depth_tighten, simplify_m=args.simplify_m)
    for name in ("max_mark_gap_m", "min_length_m", "corridor_min_r_m", "corridor_max_r_m",
                 "cluster_link_m", "dedup_min_m", "dedup_max_m"):
        v = getattr(params, name)
        if not math.isfinite(v) or v <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be a finite positive number (got {v!r}).")
    if params.corridor_min_r_m > params.corridor_max_r_m:
        raise SystemExit("--corridor-min-r-m must not exceed --corridor-max-r-m.")
    if params.dedup_min_m > params.dedup_max_m:
        raise SystemExit("--dedup-min-m must not exceed --dedup-max-m.")
    if not math.isfinite(params.simplify_m) or params.simplify_m < 0:
        raise SystemExit("--simplify-m must be a finite non-negative number.")
    if not (0.0 <= params.min_confidence <= 1.0):
        raise SystemExit("--min-confidence must be within 0..1.")
    ChannelAxisDeriver(args.input_dir, args.output_dir, params).run()


if __name__ == "__main__":
    main()
