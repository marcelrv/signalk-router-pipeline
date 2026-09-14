"""Render a routing graph over its charted context, for visual inspection.

Exists because every "resolved" claim in `data/BUILD_LOG.md` since build #34 has
been graph-metrics-only: `SPEC-GRAPH-DENSITY.md` §9.4 records that *"the
original screenshot has not actually been re-rendered to confirm visually...
treat the original location as resolved by graph metrics, not as visually
confirmed."* This module is what closes that gap, and what a Pass B/C tile is
eventually built from.

Two things get drawn:

* **context** -- land, charted water, buoys, fairways, from the region's clipped
  GeoJSON layers (`clip_pilot_data.py`'s output). Optional: a bbox with no
  `input_dir` renders the graph alone, useful for quick node/edge inspection.
* **graph** -- nodes and edges from a `RoutingGraph`, coloured by
  `edge.source_id`/`edge_kind_id` so a skeleton edge, a channel axis, and a
  navmesh boundary are visually distinct at a glance.

Kept independent of `graph_cleanup`'s other modules beyond `RoutingGraph` itself
-- a renderer that requires an applied cleanup to run would be useless for the
before half of a before/after pair.
"""
import json
import math
import os
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .graph import RoutingGraph

BBox = Tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)

# Layer name -> (facecolor, edgecolor, zorder, label). Ordered back-to-front.
#
# **Polygon layers only.** `caution_areas_polygons` and `obstructions_points`
# both carry a mix of Polygon and Point geometry (measured: 8 polygon / 5 point
# and 88 point / 3 polygon respectively, in the Coltons Point bbox alone) --
# geopandas.plot() draws a mixed-geometry GeoDataFrame's Point subset via
# `ax.scatter`, and a `facecolor="none"` meant for the polygon fill does not
# reliably apply there: the points silently fall back to matplotlib's default
# colour cycle instead of disappearing or taking the requested colour. The
# first version of this renderer shipped that bug -- unstyled orange/blue dots
# that looked like real chart symbology over open water. Point subsets of
# every layer are split out and drawn explicitly by `_plot_point_layers`
# instead, obstructions especially: they are exactly the hazard a reviewer
# must see as what it is, not as an accidental scatter colour.
CONTEXT_LAYERS = (
    ("coastal_water_polygons", "#dceefb", "none", 0, None),
    ("depare_polygons", "#c3e2f4", "none", 1, None),
    ("land_polygons", "#e8e2d0", "#b3a888", 2, "land"),
    ("dredged_areas_polygons", "#a9d4ee", "#6fa8cf", 3, "dredged area"),
    ("fairways_polygons", "none", "#7fa8c9", 3, "fairway"),
    ("restricted_areas_polygons", "none", "#c98f8f", 3, "restricted"),
    ("caution_areas_polygons", "none", "#c9b88f", 3, "caution"),
    ("mariculture_polygons", "#d9c9e8", "#a98fc9", 3, "mariculture"),
)

# Point layers, drawn explicitly with their own marker/colour -- never through
# the generic polygon loop above. (facecolor, edgecolor, marker, size, zorder, label)
# 'x' is an unfilled marker (matplotlib draws it as two strokes, not a filled
# path), so its edgecolor is ignored and only facecolor controls its colour --
# edgecolor is still passed through to `scatter` for the filled markers.
POINT_LAYERS = (
    ("obstructions_points", "#c0392b", "#c0392b", "x", 14, 7, "obstruction"),
    ("safe_water_marks_points", "#c0392b", "white", "o", 20, 6, "safe water mark"),
)

# edge_kind_id (nautical_routing_pipeline: 0 centerline, 1 navmesh_boundary,
# 2 lane, 3 macro) and source_id (see docs/BUILD_LOG data_sources table) both
# carry meaning; source_id is the more informative one where present.
SOURCE_STYLE: Dict[Optional[int], Tuple[str, float, float, str]] = {
    2: ("#7a7a7a", 0.6, 0.5, "coastal_water (skeleton)"),   # grey, thin
    15: ("#c04fc0", 1.4, 0.9, "channel_axes"),               # magenta, bold
    14: ("#2e8b57", 1.0, 0.8, "inland_waterways"),           # green
    4: ("#b8860b", 1.2, 0.9, "bridges"),                     # amber
    5: ("#b8860b", 1.2, 0.9, "locks"),
    None: ("#999999", 0.7, 0.4, "stitch connector"),
}
NAVMESH_BOUNDARY_STYLE = ("#bbbbbb", 0.4, 0.35, "navmesh boundary")

CATLAM_PORT, CATLAM_STARBOARD = 1, 2


@dataclass
class RenderConfig:
    width_px: int = 1536
    height_px: int = 1536
    dpi: int = 150
    show_context: bool = True
    show_land: bool = True
    show_marks: bool = True
    show_legend: bool = True
    title: Optional[str] = None
    node_size: float = 1.5


def _load_navmesh_regions(db_path: str, bbox: BBox):
    """Navmesh regions whose boundary overlaps `bbox`, as shapely polygons.

    **Why this exists.** `edge_kind_id == 1` (`EDGE_KIND_NAVMESH_BOUNDARY`) is
    only ever the *perimeter* ring of a navmesh region
    (`nautical_routing_pipeline.build_navmesh_region`) -- the region's interior
    is a constrained Delaunay triangulation stored separately in
    `navmesh_regions.vertices`/`triangles` and walked at query time with the
    funnel algorithm, never flattened into `edges` rows. A renderer that only
    draws `nodes`/`edges` therefore shows a wide open-water region as an empty
    ring with nothing inside -- which reads as an isolated, disconnected
    artifact when it is in fact a normally functioning region. Confirmed by
    reading `build_navmesh_region` before drawing any conclusion from what this
    looked like on screen.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        min_lon, min_lat, max_lon, max_lat = bbox
        out = []
        for (vertices, boundary_geom) in conn.execute(
                "SELECT vertices, boundary_geometry FROM navmesh_regions"):
            try:
                verts = json.loads(vertices)
            except (ValueError, TypeError):
                continue
            if not verts:
                continue
            # vertices are stored [lat, lon] (confirmed against
            # `nautical_routing_pipeline.py`'s navmesh_region_rows construction,
            # not assumed -- a swapped axis would silently mis-place every
            # region drawn from here).
            lats = [v[0] for v in verts]
            lons = [v[1] for v in verts]
            if max(lats) < min_lat or min(lats) > max_lat or \
               max(lons) < min_lon or min(lons) > max_lon:
                continue
            try:
                geom = json.loads(boundary_geom)
            except (ValueError, TypeError):
                continue
            out.append(geom)
        return out
    finally:
        conn.close()


# Process-local cache of whole, unfiltered layers, keyed by absolute path.
# **Why this exists:** a bbox-filtered `gpd.read_file(path, bbox=...)` still
# scans the whole file to find what's in the box -- measured at ~3-4s per call
# against the real MD `depare_polygons.geojson` (84 MB), regardless of how
# small the tile is. Loading that same file whole, once, took 2.9s. A run
# preparing many tiles from the same region (which every real use of this
# module is) was therefore paying the full-file cost again for every tile, of
# every layer -- the dominant cost in build #40's Coltons Point pilot
# (`docs/SPEC-GRAPH-CLEANUP.md` §6: ~14s/tile, almost entirely this). Caching
# the whole GeoDataFrame once and slicing it per tile with `.cx[...]` (an
# in-memory spatial-index lookup, not a re-read) turns an O(tiles x layers)
# disk-scan cost into O(layers) -- what would have been ~70 minutes of
# rendering for a 300-tile statewide run drops to roughly the time to load
# each layer once, a few seconds total.
_LAYER_CACHE: Dict[str, "object"] = {}


def clear_layer_cache() -> None:
    """Drop every cached layer. Call this between regions (different
    `input_dir`) if memory matters more than re-render speed, or in tests that
    must not see another test's cached data."""
    _LAYER_CACHE.clear()


def _load_layer(input_dir: str, name: str, bbox: BBox, geom_types: Optional[Sequence[str]] = None):
    """Load one clipped layer, restricted to `bbox` and optionally to the
    given geometry types -- see the `CONTEXT_LAYERS`/`POINT_LAYERS` docstring
    for why that split matters. Silent-fails to None on a missing or unreadable
    file: a tile should still render with whatever context is available."""
    import geopandas as gpd

    path = os.path.abspath(os.path.join(input_dir, f"{name}.geojson"))
    if path not in _LAYER_CACHE:
        if not os.path.exists(path):
            _LAYER_CACHE[path] = None
        else:
            try:
                _LAYER_CACHE[path] = gpd.read_file(path)
            except Exception:
                _LAYER_CACHE[path] = None

    full = _LAYER_CACHE[path]
    if full is None or len(full) == 0:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox
    gdf = full.cx[min_lon:max_lon, min_lat:max_lat]
    if geom_types is not None:
        gdf = gdf[gdf.geometry.geom_type.isin(geom_types)]
    return gdf if len(gdf) else None


def _scale_bar_length_m(bbox: BBox) -> float:
    """A round number that's roughly a fifth of the bbox width."""
    width_m = (bbox[2] - bbox[0]) * 111_320 * math.cos(math.radians((bbox[1] + bbox[3]) / 2))
    target = width_m / 5.0
    for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000):
        if step >= target:
            return float(step)
    return 50000.0


def render_tile(bbox: BBox, out_path: str,
                 graph: Optional[RoutingGraph] = None,
                 input_dir: Optional[str] = None,
                 config: Optional[RenderConfig] = None,
                 numbered_nodes: Optional[Dict[int, int]] = None) -> str:
    """Render one PNG: chart context (if `input_dir` given) with the graph
    (if `graph` given) drawn over it. At least one of the two must be given.

    `numbered_nodes` maps a node id to a display number, for a Pass B/C
    candidates image -- drawn as a boxed label at the node's position.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle

    if graph is None and input_dir is None:
        raise ValueError("render_tile needs a graph, an input_dir, or both")

    cfg = config or RenderConfig()
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = (min_lat + max_lat) / 2.0
    aspect = math.cos(math.radians(lat_mid))  # so 1 deg lon == aspect deg lat, visually

    fig_w = cfg.width_px / cfg.dpi
    fig_h = cfg.height_px / cfg.dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=cfg.dpi)
    ax.set_xlim(min_lon, max_lon)
    ax.set_ylim(min_lat, max_lat)
    ax.set_aspect(1.0 / aspect)
    ax.set_facecolor("#eef6fb")

    legend_handles = []

    if graph is not None and graph.source_db and cfg.show_context:
        from matplotlib.patches import Polygon as MplPolygon

        # zorder 3.5: above every CONTEXT_LAYERS polygon (max zorder 3, e.g.
        # depare_polygons at 1 painting opaque over anything underneath), below
        # marks/edges/nodes -- otherwise the tint is invisibly painted over.
        regions = _load_navmesh_regions(graph.source_db, bbox)
        drew_region = False
        for geom in regions:
            coords = geom.get("coordinates") or []
            rings = coords if geom.get("type") == "MultiPolygon" else [coords]
            for poly_rings in rings:
                if not poly_rings:
                    continue
                ax.add_patch(MplPolygon(poly_rings[0], closed=True,
                                        facecolor="#fff4c2", edgecolor="#c9a63a",
                                        linewidth=0.5, linestyle=(0, (1, 1)),
                                        alpha=0.35, zorder=3.5))
                drew_region = True
        if drew_region and cfg.show_legend:
            legend_handles.append(Rectangle((0, 0), 1, 1, facecolor="#fff4c2",
                                            edgecolor="#c9a63a", alpha=0.35,
                                            label="navmesh region (funnel-routed, "
                                                  "interior not shown as edges)"))

    if cfg.show_context and input_dir:
        for name, face, edge, z, label in CONTEXT_LAYERS:
            if name == "land_polygons" and not cfg.show_land:
                continue
            gdf = _load_layer(input_dir, name, bbox, geom_types=("Polygon", "MultiPolygon"))
            if gdf is None:
                continue
            gdf.plot(ax=ax, facecolor=face, edgecolor=edge, linewidth=0.5, zorder=z)
            if label and cfg.show_legend:
                legend_handles.append(Rectangle((0, 0), 1, 1, facecolor=face,
                                                edgecolor=edge if edge != "none" else face,
                                                label=label))

        for name, face, edge, marker, size, z, label in POINT_LAYERS:
            gdf = _load_layer(input_dir, name, bbox, geom_types=("Point",))
            if gdf is None:
                continue
            # 'x' is an unfilled (stroke-only) marker -- matplotlib warns and
            # ignores edgecolor for those, so only pass it for filled markers.
            kw = dict(edgecolor=edge, linewidth=0.4) if marker != "x" else dict(linewidth=1.2)
            ax.scatter(gdf.geometry.x, gdf.geometry.y, marker=marker, s=size,
                      color=face, zorder=z, **kw)
            if cfg.show_legend:
                legend_handles.append(Line2D([], [], marker=marker, linestyle="",
                                             color=face, markeredgecolor=edge,
                                             label=label))

        if cfg.show_marks:
            marks = _load_layer(input_dir, "lateral_marks_points", bbox)
            if marks is not None:
                for _, row in marks.iterrows():
                    catlam = row.get("CATLAM")
                    color = "#2e8b57" if catlam == CATLAM_PORT else (
                        "#c0392b" if catlam == CATLAM_STARBOARD else "#555555")
                    pt = row.geometry
                    ax.scatter([pt.x], [pt.y], marker="^", s=22, color=color,
                              edgecolor="black", linewidth=0.3, zorder=6)
                    name = row.get("OBJNAM")
                    if name:
                        ax.annotate(str(name), (pt.x, pt.y), fontsize=3.2,
                                   color="#333333", zorder=6,
                                   xytext=(2, 2), textcoords="offset points")
                if cfg.show_legend:
                    legend_handles.append(Line2D([], [], marker="^", linestyle="",
                                                 color="#2e8b57", label="port mark"))
                    legend_handles.append(Line2D([], [], marker="^", linestyle="",
                                                 color="#c0392b", label="starboard mark"))

    if graph is not None:
        seen_styles = set()
        # Edges first, nodes on top.
        for (u, v), e in graph.edges.items():
            a, b = graph.nodes.get(u), graph.nodes.get(v)
            if a is None or b is None:
                continue
            if not _in_bbox(a.lat, a.lon, bbox) and not _in_bbox(b.lat, b.lon, bbox):
                continue
            if e.edge_kind_id == 1:  # navmesh_boundary
                color, lw, alpha, label = NAVMESH_BOUNDARY_STYLE
            else:
                color, lw, alpha, label = SOURCE_STYLE.get(e.source_id, SOURCE_STYLE[None])
            ax.plot([a.lon, b.lon], [a.lat, b.lat], color=color, linewidth=lw,
                    alpha=alpha, zorder=8, solid_capstyle="round")
            seen_styles.add(label)

        xs, ys = [], []
        for n in graph.nodes.values():
            if _in_bbox(n.lat, n.lon, bbox):
                xs.append(n.lon)
                ys.append(n.lat)
        if xs:
            ax.scatter(xs, ys, s=cfg.node_size, color="#333333", zorder=9,
                      linewidths=0)

        if cfg.show_legend:
            for color, lw, alpha, label in list(SOURCE_STYLE.values()) + [NAVMESH_BOUNDARY_STYLE]:
                if label in seen_styles:
                    legend_handles.append(Line2D([0], [0], color=color, linewidth=max(lw, 1.2),
                                                 alpha=alpha, label=label))

        if numbered_nodes:
            for nid, num in numbered_nodes.items():
                n = graph.nodes.get(nid)
                if n is None or not _in_bbox(n.lat, n.lon, bbox):
                    continue
                ax.annotate(str(num), (n.lon, n.lat), fontsize=6, color="white",
                           weight="bold", zorder=10, ha="center", va="center",
                           bbox=dict(boxstyle="circle,pad=0.15", facecolor="#c0392b",
                                    edgecolor="white", linewidth=0.4))

    _draw_graticule(ax, bbox)
    _draw_scale_bar(ax, bbox, aspect)
    if cfg.title:
        ax.set_title(cfg.title, fontsize=8)
    if cfg.show_legend and legend_handles:
        ax.legend(handles=legend_handles, loc="upper left", fontsize=4.5,
                 framealpha=0.85, borderpad=0.4, handlelength=1.5)

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    fig.tight_layout(pad=0.3)
    fig.savefig(out_path, dpi=cfg.dpi)
    plt.close(fig)
    return out_path


def _in_bbox(lat: float, lon: float, bbox: BBox) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def _draw_graticule(ax, bbox: BBox, target_lines: int = 4) -> None:
    min_lon, min_lat, max_lon, max_lat = bbox
    for step in (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5):
        if (max_lat - min_lat) / step <= target_lines:
            break
    lat0 = math.ceil(min_lat / step) * step
    lat = lat0
    while lat < max_lat:
        ax.axhline(lat, color="#ffffff", linewidth=0.3, alpha=0.5, zorder=1)
        ax.annotate(f"{lat:.3f}N", (min_lon, lat), fontsize=3.5, color="#666666",
                   zorder=11, xytext=(2, 1), textcoords="offset points")
        lat += step
    lon0 = math.ceil(min_lon / step) * step
    lon = lon0
    while lon < max_lon:
        ax.axvline(lon, color="#ffffff", linewidth=0.3, alpha=0.5, zorder=1)
        ax.annotate(f"{lon:.3f}E", (lon, min_lat), fontsize=3.5, color="#666666",
                   zorder=11, xytext=(2, 1), textcoords="offset points")
        lon += step


def _draw_scale_bar(ax, bbox: BBox, aspect: float) -> None:
    length_m = _scale_bar_length_m(bbox)
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = (min_lat + max_lat) / 2.0
    deg_lon = length_m / (111_320 * math.cos(math.radians(lat_mid)))
    x0 = min_lon + (max_lon - min_lon) * 0.03
    y0 = min_lat + (max_lat - min_lat) * 0.04
    ax.plot([x0, x0 + deg_lon], [y0, y0], color="black", linewidth=2, zorder=12,
            solid_capstyle="butt")
    label = f"{length_m/1000:g} km" if length_m >= 1000 else f"{length_m:g} m"
    ax.annotate(label, (x0, y0), fontsize=5, color="black", zorder=12,
               xytext=(0, 3), textcoords="offset points")


def before_after(bbox: BBox, before_db: str, after_db: str, out_dir: str,
                  input_dir: Optional[str] = None,
                  labels: Tuple[str, str] = ("before", "after")) -> Tuple[str, str]:
    """Render the same bbox from two databases, for a side-by-side comparison."""
    before_path = os.path.join(out_dir, f"{labels[0]}.png")
    after_path = os.path.join(out_dir, f"{labels[1]}.png")
    g_before = RoutingGraph.load(before_db)
    render_tile(bbox, before_path, graph=g_before, input_dir=input_dir,
               config=RenderConfig(title=f"{labels[0]}: {os.path.basename(before_db)} "
                                        f"({len(g_before.nodes)}n/{len(g_before.edges)}e)"))
    g_after = RoutingGraph.load(after_db)
    render_tile(bbox, after_path, graph=g_after, input_dir=input_dir,
               config=RenderConfig(title=f"{labels[1]}: {os.path.basename(after_db)} "
                                        f"({len(g_after.nodes)}n/{len(g_after.edges)}e)"))
    return before_path, after_path


def diff_removed(g_before: RoutingGraph, g_after: RoutingGraph):
    """What `render_diff` draws in red: edges in `g_before` gone from
    `g_after`, and nodes gone from `g_after` that aren't already implied by a
    removed edge (an isolated dropped node with no incident edge -- rare, but
    a `drop_node` on an already-degree-0 node produces exactly this).

    Split out from `render_diff` so the diff itself -- which edges/nodes count
    as removed -- has a return value a test can assert on directly, instead of
    only being checkable by decoding pixels out of the rendered PNG.
    """
    removed_edges = {k: e for k, e in g_before.edges.items() if k not in g_after.edges}
    kept_edge_endpoints = {n for (u, v) in g_after.edges for n in (u, v)}
    removed_nodes = {nid: n for nid, n in g_before.nodes.items()
                     if nid not in g_after.nodes and nid not in kept_edge_endpoints}
    return removed_edges, removed_nodes


def render_diff(bbox: BBox, out_path: str, before_db: str, after_db: str,
                input_dir: Optional[str] = None,
                config: Optional[RenderConfig] = None) -> str:
    """One picture, not two: everything the cleanup removed drawn in red over
    what survived, on the real chart. Built because a side-by-side pair asks the
    reader to spot a difference across two images; a single overlay puts the
    difference itself on the page -- what a cleanup run actually did is exactly
    the red ink, nothing else to compare by eye.

    Kept edges/nodes (present in both databases, matched by id -- valid because
    node ids are coordinate-derived, `nautical_routing_pipeline._coord_to_id`)
    draw in the ordinary `SOURCE_STYLE` colours. Removed edges draw as a bold
    dashed red line **beneath** the kept graph; every node gone from `after`
    that isn't an endpoint of a *surviving* edge gets a red dot too (see
    `diff_removed`) -- deliberately including edge endpoints, not just
    standalone drops, so a multi-node dropped stub shows a dot at every vertex
    along its dashed line, not only at its far end.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    cfg = config or RenderConfig()
    g_before = RoutingGraph.load(before_db)
    g_after = RoutingGraph.load(after_db)
    removed_edges, removed_nodes = diff_removed(g_before, g_after)

    # Render the surviving graph exactly as render_tile would, then draw the
    # removed layer directly onto the same axes before saving.
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = (min_lat + max_lat) / 2.0
    aspect = math.cos(math.radians(lat_mid))
    fig_w, fig_h = cfg.width_px / cfg.dpi, cfg.height_px / cfg.dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=cfg.dpi)
    ax.set_xlim(min_lon, max_lon)
    ax.set_ylim(min_lat, max_lat)
    ax.set_aspect(1.0 / aspect)
    ax.set_facecolor("#eef6fb")

    legend_handles = []
    if cfg.show_context and input_dir:
        for name, face, edge, z, label in CONTEXT_LAYERS:
            if name == "land_polygons" and not cfg.show_land:
                continue
            gdf = _load_layer(input_dir, name, bbox, geom_types=("Polygon", "MultiPolygon"))
            if gdf is not None:
                gdf.plot(ax=ax, facecolor=face, edgecolor=edge, linewidth=0.5, zorder=z)

    n_removed_drawn = 0
    for (u, v), e in removed_edges.items():
        a, b = g_before.nodes.get(u), g_before.nodes.get(v)
        if a is None or b is None:
            continue
        if not _in_bbox(a.lat, a.lon, bbox) and not _in_bbox(b.lat, b.lon, bbox):
            continue
        ax.plot([a.lon, b.lon], [a.lat, b.lat], color="#d81e1e", linewidth=1.8,
                alpha=0.9, zorder=7, linestyle=(0, (3, 2)), solid_capstyle="round")
        n_removed_drawn += 1
    rx, ry = [], []
    for n in removed_nodes.values():
        if _in_bbox(n.lat, n.lon, bbox):
            rx.append(n.lon)
            ry.append(n.lat)
    if rx:
        ax.scatter(rx, ry, s=10, color="#d81e1e", zorder=7.5, linewidths=0)

    seen_styles = set()
    for (u, v), e in g_after.edges.items():
        a, b = g_after.nodes.get(u), g_after.nodes.get(v)
        if a is None or b is None:
            continue
        if not _in_bbox(a.lat, a.lon, bbox) and not _in_bbox(b.lat, b.lon, bbox):
            continue
        if e.edge_kind_id == 1:
            color, lw, alpha, label = NAVMESH_BOUNDARY_STYLE
        else:
            color, lw, alpha, label = SOURCE_STYLE.get(e.source_id, SOURCE_STYLE[None])
        ax.plot([a.lon, b.lon], [a.lat, b.lat], color=color, linewidth=lw,
                alpha=alpha, zorder=8, solid_capstyle="round")
        seen_styles.add(label)
    xs, ys = [], []
    for n in g_after.nodes.values():
        if _in_bbox(n.lat, n.lon, bbox):
            xs.append(n.lon)
            ys.append(n.lat)
    if xs:
        ax.scatter(xs, ys, s=cfg.node_size, color="#333333", zorder=9, linewidths=0)

    if cfg.show_legend:
        if n_removed_drawn or rx:
            legend_handles.append(Line2D([0], [0], color="#d81e1e", linewidth=1.8,
                                         linestyle=(0, (3, 2)),
                                         label=f"removed ({len(removed_edges)} edges, "
                                               f"{len(removed_nodes)} nodes)"))
        for color, lw, alpha, label in list(SOURCE_STYLE.values()) + [NAVMESH_BOUNDARY_STYLE]:
            if label in seen_styles:
                legend_handles.append(Line2D([0], [0], color=color, linewidth=max(lw, 1.2),
                                             alpha=alpha, label=f"kept: {label}"))

    _draw_graticule(ax, bbox)
    _draw_scale_bar(ax, bbox, aspect)
    if cfg.title:
        ax.set_title(cfg.title, fontsize=8)
    elif cfg.title is None:
        dn = len(g_before.nodes) - len(g_after.nodes)
        de = len(g_before.edges) - len(g_after.edges)
        ax.set_title(f"removed by cleanup: -{dn} nodes / -{de} edges "
                     f"({os.path.basename(before_db)} -> {os.path.basename(after_db)})",
                     fontsize=8)
    if cfg.show_legend and legend_handles:
        ax.legend(handles=legend_handles, loc="upper left", fontsize=4.5,
                 framealpha=0.85, borderpad=0.4, handlelength=1.5)

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    fig.tight_layout(pad=0.3)
    fig.savefig(out_path, dpi=cfg.dpi)
    plt.close(fig)
    return out_path
