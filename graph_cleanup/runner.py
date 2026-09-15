"""Drive a backend over a batch of prepared tiles: answer, validate, retry
once, record, and turn a `keep`/`drop`/`unsure` verdict into an `Op`
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

Resumable by construction: each tile's raw answer is written to
`<tile_dir>/answer.json` as soon as it is received, so a killed or interrupted
run picks back up by skipping tiles that already have one (`--resume`, the
default). A tile that never produces valid JSON after one retry is marked
`unanswered` and contributes no ops -- **the default is always `keep`**,
because a tile a weak backend could not judge must never silently become a
deletion.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .backends.base import Backend, BackendError, read_context
from .candidates import DEAD_END_STUB, SMALL_COMPONENT
from .ops import DROP_NODE, Op

VALID_VERDICTS = ("keep", "drop", "unsure")


@dataclass
class RunStats:
    total: int = 0
    answered: int = 0
    skipped_existing: int = 0
    retried: int = 0
    unanswered: int = 0
    verdicts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.answered}/{self.total} tiles answered "
                f"({self.skipped_existing} already done, {self.retried} retried, "
                f"{self.unanswered} unanswered); verdicts {self.verdicts}")


def _validate(raw: str, context: dict) -> Dict[str, dict]:
    """Parse and check a backend's answer against the tile's own candidate
    numbers. Raises ValueError on anything wrong -- the caller decides whether
    that's worth a retry."""
    from .backends.claude import parse_answer  # reuses the fence-stripping parser

    parsed = parse_answer(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"answer is not a JSON object: {type(parsed)}")
    expected = {str(c["n"]) for c in context["candidates"]}
    got = set(parsed.keys())
    unknown = got - expected
    if unknown:
        raise ValueError(f"answer references numbers not in this tile: {unknown}")
    missing = expected - got
    if missing:
        raise ValueError(f"answer omits numbers from this tile: {missing}")
    for n, entry in parsed.items():
        if not isinstance(entry, dict) or "verdict" not in entry:
            raise ValueError(f"entry {n!r} is missing 'verdict': {entry!r}")
        if entry["verdict"] not in VALID_VERDICTS:
            raise ValueError(f"entry {n!r} has an invalid verdict: {entry['verdict']!r}")
    return parsed


def run_tile(backend: Backend, tile_dir: str, max_retries: int = 1) -> Optional[Dict[str, dict]]:
    """Answer one tile, validating and retrying malformed JSON. Returns the
    validated verdict dict, or None if it never became valid."""
    context = read_context(tile_dir)
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            raw = backend.answer_tile(tile_dir)
        except BackendError as exc:
            last_error = str(exc)
            continue
        try:
            return _validate(raw, context)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            continue
    if last_error:
        with open(os.path.join(tile_dir, "error.txt"), "w", encoding="utf-8") as fh:
            fh.write(last_error)
    return None


def run_all(backend: Backend, tile_dirs: Sequence[str], resume: bool = True,
           max_retries: int = 1, sleep_between_s: float = 0.0) -> RunStats:
    """Answer every tile in `tile_dirs`, writing `answer.json` into each."""
    stats = RunStats(total=len(tile_dirs))
    for tile_dir in tile_dirs:
        answer_path = os.path.join(tile_dir, "answer.json")
        if resume and os.path.exists(answer_path):
            stats.skipped_existing += 1
            continue
        verdicts = run_tile(backend, tile_dir, max_retries=max_retries)
        if verdicts is None:
            stats.unanswered += 1
            continue
        with open(answer_path, "w", encoding="utf-8") as fh:
            json.dump(verdicts, fh, indent=2)
        stats.answered += 1
        for entry in verdicts.values():
            stats.verdicts[entry["verdict"]] = stats.verdicts.get(entry["verdict"], 0) + 1
        if sleep_between_s:
            time.sleep(sleep_between_s)
    return stats


def answers_to_ops(tile_dirs: Sequence[str], author: str) -> List[Op]:
    """Turn every tile's `answer.json` (once written by `run_all`, or by a
    batch collector) into `Op` records.

    Only `drop` produces an op -- `keep` and `unsure` both mean "leave it",
    which is already the graph's current state and needs no operation. A
    `small_component` drop removes every node in the component (its edges go
    with it); a `dead_end_stub` drop removes the stub's own chain, working from
    the free end inward so each `drop_node` sees a still-degree-1 node.
    """
    ops: List[Op] = []
    for tile_dir in tile_dirs:
        answer_path = os.path.join(tile_dir, "answer.json")
        manifest_path = os.path.join(tile_dir, "manifest.json")
        if not os.path.exists(answer_path) or not os.path.exists(manifest_path):
            continue
        with open(answer_path, encoding="utf-8") as fh:
            verdicts = json.load(fh)
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        tile_id = os.path.basename(tile_dir.rstrip("/"))
        for n, entry in verdicts.items():
            if entry["verdict"] != "drop":
                continue
            cand = manifest.get(n)
            if cand is None:
                continue
            reason = entry.get("why") or f"{cand['kind']} dropped by reviewer"
            ops += _drop_ops(cand, author, reason, tile_id)
    return ops


#  The prompt asks for a verdict + a one-line reason, not a numeric confidence
#  -- there is nothing in a model's answer to read one from. A flat value below
#  1.0 (a deterministic pass's confidence) marks these as needing the human
#  review the tier-5 override workflow (README.md "Community override workflow")
#  calls for, without pretending to a precision the model never gave.
AI_DROP_CONFIDENCE = 0.7


def _drop_ops(cand: dict, author: str, reason: str, tile_id: str) -> List[Op]:
    kind = cand["kind"]
    nodes = cand.get("nodes")
    if not nodes:
        return []
    if kind == SMALL_COMPONENT:
        target_nodes = nodes
    elif kind == DEAD_END_STUB:
        # Drop from the free end inward -- dropping the junction end first
        # would delete a node still holding the rest of the graph together.
        target_nodes = nodes[:-1]
    else:
        return []
    return [Op(op=DROP_NODE, node=n, author=author, reason=reason,
               confidence=AI_DROP_CONFIDENCE, tile=tile_id) for n in target_nodes]
