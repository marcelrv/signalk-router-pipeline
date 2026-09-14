"""The interface every review backend implements."""
import json
import os
from typing import Protocol


class BackendError(Exception):
    """A backend could not produce a usable answer for a tile.

    Deliberately distinct from a schema-validation failure (handled by the
    runner's retry) -- this is for the backend's own failures: a network
    error, an API refusal, a missing credential.
    """


class Backend(Protocol):
    def answer_tile(self, tile_dir: str) -> str:
        """Return the raw text response for the tile at `tile_dir`.

        Implementations read `chart.png`, `candidates.png`, `context.json` and
        `prompt.txt` from `tile_dir` themselves -- the runner only orchestrates
        the queue and validates the returned JSON, it never reaches into a
        tile's images.
        """
        ...


def read_context(tile_dir: str) -> dict:
    with open(os.path.join(tile_dir, "context.json"), encoding="utf-8") as fh:
        return json.load(fh)


def read_prompt(tile_dir: str) -> str:
    with open(os.path.join(tile_dir, "prompt.txt"), encoding="utf-8") as fh:
        return fh.read()
