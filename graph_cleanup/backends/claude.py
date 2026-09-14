"""Claude as a review backend, via the Anthropic API (`pip install anthropic`).

Two paths, per `docs/SPEC-GRAPH-CLEANUP.md` §6's "Sonnet first, local second":

* `ClaudeBackend` -- synchronous, one tile per `client.messages.create` call.
  Use this for small runs (a handful of tiles, or `--limit N` in
  `run_review.py`) -- the natural way to check the harness and the prompt
  actually work before spending on the full sample.
* `submit_batch`/`collect_batch` -- the Message Batches API, for the ~300-tile
  gold-set run proper. Cheaper (batches run at a discount) and the right shape
  for hundreds of independent, order-insensitive requests.

The system prompt (`prompt.txt`, identical across every tile in a run) is sent
with `cache_control: ephemeral` so a multi-tile run only pays full price for it
once -- see `shared/prompt-caching.md` in the `claude-api` skill.

**Untested against a live API key** -- this session had none available (no
`ANTHROPIC_API_KEY`, no `ant auth login` profile). Written directly from the
Anthropic SDK's documented request/response shapes, not guessed; before
trusting `submit_batch` for a real paid run, run `ClaudeBackend.answer_tile`
against one or two real tiles first (`run_review.py --backend claude --limit 2`)
and confirm the parsed verdicts look sane.
"""
import base64
import json
import os
import time
from typing import Dict, List, Optional

from .base import BackendError, read_context, read_prompt

DEFAULT_MODEL = "claude-sonnet-5"
# 16000 (not the small answer's actual size) per the SDK's own guidance: hitting
# max_tokens truncates mid-thought and forces a retry, and thinking tokens are
# billed the same regardless of the cap, so there is no cost reason to lowball it.
DEFAULT_MAX_TOKENS = 16000
DEFAULT_EFFORT = "medium"


def _image_block(path: str) -> dict:
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": data}}


def _user_content(tile_dir: str) -> List[dict]:
    context = read_context(tile_dir)
    return [
        _image_block(os.path.join(tile_dir, "chart.png")),
        _image_block(os.path.join(tile_dir, "candidates.png")),
        {"type": "text", "text": "context.json:\n" + json.dumps(context, indent=2)},
    ]


def _system_block(prompt_text: str) -> List[dict]:
    return [{"type": "text", "text": prompt_text,
            "cache_control": {"type": "ephemeral"}}]


def parse_answer(raw_text: str) -> dict:
    """Pull the JSON object out of a response that may have fenced it in
    ```json ... ``` despite the prompt asking for JSON only."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


class ClaudeBackend:
    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
                effort: str = DEFAULT_EFFORT, client=None):
        try:
            import anthropic
        except ImportError as exc:
            raise BackendError(
                "the 'anthropic' package is not installed -- pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

    def answer_tile(self, tile_dir: str) -> str:
        prompt_text = read_prompt(tile_dir)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_system_block(prompt_text),
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": _user_content(tile_dir)}],
            )
        except self._anthropic.APIConnectionError as exc:
            raise BackendError(f"connection error: {exc}") from exc
        except self._anthropic.RateLimitError as exc:
            raise BackendError(f"rate limited: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise BackendError(f"API error {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise BackendError(f"model refused: {getattr(response, 'stop_details', None)}")

        for block in response.content:
            if block.type == "text":
                return block.text
        raise BackendError("response had no text block")


def submit_batch(tile_dirs: Dict[str, str], model: str = DEFAULT_MODEL,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 effort: str = DEFAULT_EFFORT, client=None) -> str:
    """Submit one batch request per tile. `tile_dirs` maps tile_id -> dir_path
    (the tile_id becomes the batch's `custom_id`, so results can be matched
    back up -- results come back in arbitrary order, never by position)."""
    import anthropic

    client = client or anthropic.Anthropic()
    requests = []
    for tile_id, tile_dir in tile_dirs.items():
        prompt_text = read_prompt(tile_dir)
        requests.append({
            "custom_id": tile_id,
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "system": _system_block(prompt_text),
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": effort},
                "messages": [{"role": "user", "content": _user_content(tile_dir)}],
            },
        })
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def poll_batch(batch_id: str, client=None, interval_s: float = 30.0,
               timeout_s: float = 3600.0) -> str:
    """Block until the batch reaches a terminal `processing_status`."""
    import anthropic

    client = client or anthropic.Anthropic()
    start = time.time()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch.processing_status
        if time.time() - start > timeout_s:
            raise BackendError(f"batch {batch_id} did not finish within {timeout_s}s "
                              f"(status: {batch.processing_status})")
        time.sleep(interval_s)


def collect_batch(batch_id: str, client=None) -> Dict[str, str]:
    """`tile_id -> raw response text` for every succeeded result.

    A tile whose result was `errored`/`canceled`/`expired` is omitted, not
    raised -- one bad tile in a 300-tile batch should not lose the other 299;
    the caller (`run_review.py`) reports which tile_ids are missing so they can
    be resubmitted individually.
    """
    import anthropic

    client = client or anthropic.Anthropic()
    out: Dict[str, str] = {}
    for entry in client.messages.batches.results(batch_id):
        if entry.result.type != "succeeded":
            continue
        message = entry.result.message
        for block in message.content:
            if block.type == "text":
                out[entry.custom_id] = block.text
                break
    return out
