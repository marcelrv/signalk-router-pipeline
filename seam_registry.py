"""Round 25 shared global-node registry (STITCHING_DESIGN.md Section 3).

A small pipeline-side-only SQLite database that lets independently-built
adjacent region `.sqlite` files share seam-node identities *by construction*
instead of trying (and failing, per STITCHING_DESIGN.md Sections 2.1/2.2) to
recompute identical geometry independently: a region publishes its own
boundary nodes into this registry after it is built, and any later-built
neighbouring region adopts them verbatim (same `node_id`/`lon`/`lat`) before
it writes its own `.sqlite`.

This registry is NEVER shipped -- it lives only on the machine doing the
pipeline build, is not published to `router-data`, and is not loaded by
routeiq at runtime. The cross-build sharing effect is baked directly into
each region's own `.sqlite` at build time (design fork Section 3.8, option
(a): adopted seam nodes + their connector edges are written straight into
the graph before `export_to_sqlite`, so the existing runtime node-ID merge
does the rest -- no runtime overlay).

Builds are sequential (no concurrency) -- no locking/transaction machinery
beyond sqlite3's own connection-level defaults is needed.

There is deliberately NO freeze / retirement / `source_region`-replacement
lifecycle (open question Section 5, resolved by the user): `source_region`
is kept purely as a provenance column. A rebuild simply re-adopts whatever
is already in the registry (stable ids) and re-publishes its own current
boundary nodes over it (plain upsert) -- no delete/replace step.
"""
import os
import sqlite3
from typing import Dict, Iterable, List


class SeamRegistry:
    """One row per published seam node:
    `(node_id, lon, lat, node_kind_id, node_depth, source_region)`.

    Opens (or creates) idempotently -- safe to instantiate repeatedly against
    the same path across a sequence of region builds.
    """

    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seam_nodes (
                node_id INTEGER PRIMARY KEY,
                lon REAL NOT NULL,
                lat REAL NOT NULL,
                node_kind_id INTEGER NOT NULL DEFAULT 0,
                node_depth REAL,
                source_region TEXT
            );
            -- Composite index on (lon, lat): every query here is a bbox
            -- (BETWEEN on both columns) over a registry that stays small
            -- (boundary nodes only, across however many regions have been
            -- built) -- a single composite index is sufficient at this scale.
            CREATE INDEX IF NOT EXISTS idx_seam_nodes_lon_lat ON seam_nodes(lon, lat);
            """
        )
        self.conn.commit()

    def query_bbox(self, min_lon: float, min_lat: float,
                    max_lon: float, max_lat: float) -> List[sqlite3.Row]:
        """All registry nodes whose coordinate falls inside the given bbox."""
        cur = self.conn.execute(
            "SELECT node_id, lon, lat, node_kind_id, node_depth, source_region "
            "FROM seam_nodes WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
            (min_lon, max_lon, min_lat, max_lat),
        )
        return cur.fetchall()

    def upsert_nodes(self, rows: Iterable[Dict]):
        """Idempotent upsert, keyed by node_id. Each row is a dict with keys
        node_id, lon, lat, node_kind_id, node_depth, source_region."""
        rows = list(rows)
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO seam_nodes (node_id, lon, lat, node_kind_id, node_depth, source_region)
            VALUES (:node_id, :lon, :lat, :node_kind_id, :node_depth, :source_region)
            ON CONFLICT(node_id) DO UPDATE SET
                lon=excluded.lon, lat=excluded.lat,
                node_kind_id=excluded.node_kind_id,
                node_depth=excluded.node_depth,
                source_region=excluded.source_region
            """,
            rows,
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seam_nodes").fetchone()[0]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
