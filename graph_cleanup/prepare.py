"""Write review tiles to disk as self-contained work items
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

    tiles/<region>/tile_00842/
      chart.png        rendered chart context, no graph overlay
      candidates.png    graph overlay + numbered candidate markers
      context.json      tile bbox + one entry per number with its measured facts
      prompt.txt         the fixed reviewer prompt, verbatim

Deliberately flat files, not a live query interface: this is what makes the
review batchable (resumable, parallel, independently re-runnable per item) and
comparable across backends -- a local model run next to Claude Sonnet's answers
sees exactly the same bytes. See `docs/SPEC-GRAPH-CLEANUP.md` §6 for why that
shape was chosen over an MCP/agentic loop for the production path.
"""
import json
import os
from dataclasses import dataclass
from typing import List, Optional

from .candidates import Candidate
from .graph import RoutingGraph
from .render import RenderConfig, render_tile
from .runner import archive_results, atomic_write_text
from .tiles import Tile

PRUNE_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "prune.md")


@dataclass
class PreparedTile:
    tile_id: str
    dir_path: str
    n_candidates: int


def _fact_summary(facts: dict) -> dict:
    """Drop internal/None fields context.json doesn't need to carry."""
    return {k: v for k, v in facts.items() if v is not None and k != "protected"}


def write_tile(tile: Tile, g: RoutingGraph, out_dir: str,
               input_dir: Optional[str] = None,
               prompt_path: str = PRUNE_PROMPT_PATH) -> PreparedTile:
    if not input_dir:
        # chart.png is specifically the chart WITHOUT the graph overlay -- with
        # no clipped GeoJSON to draw it from there is nothing to write, and
        # letting render_tile raise its generic "needs a graph or an input_dir"
        # error here would be confusing at the call site that actually forgot it.
        raise ValueError("write_tile needs input_dir (clipped GeoJSON layers) "
                         "to render chart.png")
    os.makedirs(out_dir, exist_ok=True)
    numbered = tile.numbered()

    render_tile(tile.bbox, os.path.join(out_dir, "chart.png"),
               graph=None, input_dir=input_dir,
               config=RenderConfig(title=None, show_legend=True))
    render_tile(tile.bbox, os.path.join(out_dir, "candidates.png"),
               graph=g, input_dir=input_dir,
               config=RenderConfig(title=None, show_legend=True),
               numbered_nodes={c.anchor: n for n, c in numbered.items()})

    context = {
        "tile_id": tile.id,
        "bbox": {"min_lon": tile.bbox[0], "min_lat": tile.bbox[1],
                "max_lon": tile.bbox[2], "max_lat": tile.bbox[3]},
        "candidates": [
            {"n": n, "kind": c.kind, "candidate_id": c.id,
             **_fact_summary(c.facts)}
            for n, c in numbered.items()
        ],
    }
    atomic_write_text(os.path.join(out_dir, "context.json"), json.dumps(context, indent=2))

    # manifest.json is NOT sent to the reviewer (a 30-node component's raw id
    # list is noise to a model and tells it nothing) -- it exists purely so
    # runner.answers_to_ops() can turn a verdict back into the exact nodes it
    # was about, without re-deriving candidates from the graph after the fact.
    manifest = {str(n): {"candidate_id": c.id, "kind": c.kind, "nodes": c.nodes}
               for n, c in numbered.items()}
    manifest_path = os.path.join(out_dir, "manifest.json")
    new_manifest = json.dumps(manifest)
    # Same tile id, different candidates (another graph build, other
    # thresholds): the old answer's numbers would map to the wrong nodes, so
    # it must not survive. An identical manifest keeps its answer (--resume).
    old_manifest = None
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            old_manifest = fh.read()
    if old_manifest != new_manifest:
        archive_results(out_dir)  # to history/, together with the old manifest
    atomic_write_text(manifest_path, new_manifest)

    with open(prompt_path, encoding="utf-8") as fh:
        prompt_text = fh.read()
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as fh:
        fh.write(prompt_text)

    return PreparedTile(tile_id=tile.id, dir_path=out_dir, n_candidates=len(numbered))


def write_all(tiles: List[Tile], g: RoutingGraph, out_root: str,
              input_dir: Optional[str] = None,
              prompt_path: str = PRUNE_PROMPT_PATH) -> List[PreparedTile]:
    out = []
    for tile in tiles:
        tile_dir = os.path.join(out_root, tile.id)
        out.append(write_tile(tile, g, tile_dir, input_dir=input_dir,
                              prompt_path=prompt_path))
    return out
