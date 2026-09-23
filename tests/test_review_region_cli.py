"""End-to-end test of `review_region.py`'s CLI, against a tiny synthetic
database and the offline mock backend -- proves the whole prepare/answer/ops
pipeline wires together correctly, without touching real chart data or a
network."""
import json
import os
import sqlite3

import pytest

import review_region


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
            node_depth REAL, region_id INTEGER, node_kind_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
        CREATE TABLE edges (source INTEGER, target INTEGER, distance REAL,
            min_depth REAL, drval1 REAL, max_air_draft REAL, min_width REAL,
            cost_factor REAL, distance_to_land REAL, edge_type_id INTEGER,
            traffic_mode INTEGER, crosses_land INTEGER, crosses_obstacle INTEGER,
            edge_kind_id INTEGER, source_tier INTEGER, source_id INTEGER,
            width_profile TEXT, requires_lock INTEGER, lock_id INTEGER);
        CREATE TABLE pois (id INTEGER PRIMARY KEY, name TEXT, type_id INTEGER,
            properties TEXT, lat REAL, lon REAL, region_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
        CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
            boundary_geometry TEXT, vertices TEXT, triangles TEXT,
            triangle_adjacency TEXT, boundary_node_ids TEXT);
    """)
    # A ring (no dead ends of its own) plus one real dead-end stub off it.
    ring = [(1, 52.00, 4.00), (2, 52.00, 4.01), (3, 52.00, 4.02),
           (4, 52.00, 4.03), (5, 52.01, 4.02), (6, 52.01, 4.01)]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)", ring)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)",
                     [(10, 52.005, 4.021), (11, 52.010, 4.022)])
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
    ids = [n for n, _, _ in ring]
    pairs = list(zip(ids, ids[1:] + ids[:1])) + [(3, 10), (10, 11)]
    for a, b in pairs:
        for s, t in ((a, b), (b, a)):
            conn.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
    conn.execute("INSERT INTO pois VALUES (1,'Ring Marina',0,'{}',52.0,4.02,1,1,2)")
    conn.commit()
    conn.close()


def _make_geojson(input_dir):
    input_dir.mkdir()
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [3.9, 51.9], [4.2, 51.9], [4.2, 52.2], [3.9, 52.2], [3.9, 51.9]]]}}]}
    (input_dir / "land_polygons.geojson").write_text(json.dumps(fc))


def test_prepare_only_writes_tiles_and_stops(tmp_path):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--prepare-only"])
    assert rc == 0
    tile_dirs = review_region._find_tile_dirs(str(out_dir))
    assert len(tile_dirs) == 1
    assert not os.path.exists(os.path.join(tile_dirs[0], "answer.json"))


def test_mock_backend_end_to_end_writes_ops(tmp_path):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"
    ops_out = tmp_path / "ai.ops.jsonl"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--backend", "mock",
                             "--ops-out", str(ops_out)])
    assert rc == 0
    tile_dirs = review_region._find_tile_dirs(str(out_dir))
    assert all(os.path.exists(os.path.join(d, "answer.json")) for d in tile_dirs)
    assert ops_out.exists()


def test_reusing_out_dir_ignores_a_stale_tile_from_an_earlier_run(tmp_path):
    """A stale tile directory left in --out-dir by an earlier, differently-
    scoped run (a different --bbox or --sample-tiles) must not be answered or
    included in --ops-out -- only this run's own tiles. `_find_tile_dirs`
    globs the whole directory and would previously pick up anything with a
    context.json, mixing an out-of-scope answer into a fresh run's output."""
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    # A stale tile directory, as if left over from an earlier run with a
    # different scope: its own context.json/manifest.json/answer.json,
    # naming a node this run's own database doesn't even have -- so if it
    # were picked up, it would show up as an extra drop op.
    stale_dir = out_dir / "t0_stale_9999"
    stale_dir.mkdir(parents=True)
    (stale_dir / "context.json").write_text(json.dumps({
        "tile_id": "t0_stale_9999", "bbox": [0, 0, 1, 1],
        "candidates": [{"n": 1, "kind": "dead_end_stub", "candidate_id": "stub:999999"}],
    }))
    (stale_dir / "manifest.json").write_text(json.dumps({
        # Two nodes: dead_end_stub drops nodes[:-1] (everything but the
        # junction end), so this must have more than one node to produce a
        # real op -- a single-node list would drop nothing regardless.
        "1": {"candidate_id": "stub:999999", "kind": "dead_end_stub",
              "nodes": [999999, 999998]},
    }))
    (stale_dir / "answer.json").write_text(json.dumps({
        "1": {"verdict": "drop", "why": "stale, out-of-scope answer"},
    }))

    ops_out = tmp_path / "ai.ops.jsonl"
    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--backend", "mock",
                             "--ops-out", str(ops_out)])
    assert rc == 0

    from graph_cleanup import ops as ops_mod
    written_ops = list(ops_mod.read_ops(str(ops_out)))
    assert all(op.node != 999999 for op in written_ops), \
        "the stale tile's answer must not leak into this run's ops"

    # The stale directory itself is untouched by this run.
    assert json.loads((stale_dir / "answer.json").read_text())["1"]["why"] == \
        "stale, out-of-scope answer"


def test_bbox_filters_candidates(tmp_path, capsys):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--prepare-only",
                             "--bbox", "170,80,171,81"])  # far from any candidate
    assert rc == 0
    out = capsys.readouterr().out
    assert "restricts to 0/" in out
    assert "nothing to review" in out


def test_local_backend_end_to_end_with_faked_http(tmp_path, monkeypatch, capsys):
    """`--backend local` wires through the CLI: request goes to the given URL
    with the given model/temperature, a `drop` becomes an op, and the audit
    file exists. (The unusable-reply path is exercised by
    `test_local_backend_garbage_reply_is_degraded_no_ops_and_retried_on_resume`.)"""
    from graph_cleanup.backends import local_openai

    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"
    ops_out = tmp_path / "ai.ops.jsonl"
    seen = []

    def fake_post(url, payload, headers, timeout):
        seen.append((url, payload))
        ctx_text = payload["messages"][1]["content"][-1]["text"]
        ctx = json.loads(ctx_text[len("context.json:\n"):])
        ans = {str(c["n"]): {"verdict": "drop", "why": "fake"} for c in ctx["candidates"]}
        return {"choices": [{"message": {"content": json.dumps(ans)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7}}

    monkeypatch.setattr(local_openai, "urllib_transport", fake_post)
    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--backend", "local",
                             "--local-url", "http://fake:1/v1", "--model", "m1",
                             "--temperature", "0.1", "--ops-out", str(ops_out)])
    assert rc == 0
    assert seen and seen[0][0] == "http://fake:1/v1/chat/completions"
    assert seen[0][1]["model"] == "m1" and seen[0][1]["temperature"] == 0.1
    out = capsys.readouterr().out
    assert "backend: local" in out and "local backend:" in out
    tile_dirs = review_region._find_tile_dirs(str(out_dir))
    assert all(os.path.exists(os.path.join(d, "local_audit.jsonl")) for d in tile_dirs)
    from graph_cleanup import ops as ops_mod
    written = list(ops_mod.read_ops(str(ops_out)))
    assert written and all(op.author == "ai:m1" for op in written)


def _ctx(payload):
    text = payload["messages"][1]["content"][-1]["text"]
    return json.loads(text[len("context.json:\n"):])


def _cli(tmp_path, *extra):
    return review_region.main(["--db", str(tmp_path / "g.sqlite"),
                               "--input-dir", str(tmp_path / "geo"),
                               "--out-dir", str(tmp_path / "tiles"),
                               "--backend", "local", "--local-url", "http://fake:1/v1",
                               *extra])


def _setup(tmp_path):
    _make_db(tmp_path / "g.sqlite")
    _make_geojson(tmp_path / "geo")


def test_local_backend_garbage_reply_is_degraded_no_ops_and_retried_on_resume(
        tmp_path, monkeypatch):
    """An unusable reply becomes an all-`unsure` answer (no op), the answer is
    marked degraded, and a plain re-run (--resume is the default) asks again
    instead of trusting it; a good reply then produces the drop op."""
    from graph_cleanup import ops as ops_mod
    from graph_cleanup.backends import local_openai

    _setup(tmp_path)
    ops_out = tmp_path / "ai.ops.jsonl"
    mode = {"reply": "I refuse to answer in JSON."}
    calls = []

    def fake_post(url, payload, headers, timeout):
        calls.append(1)
        reply = mode["reply"]
        if reply is None:
            ans = {str(c["n"]): {"verdict": "drop", "why": "fake"}
                   for c in _ctx(payload)["candidates"]}
            reply = json.dumps(ans)
        return {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}

    monkeypatch.setattr(local_openai, "urllib_transport", fake_post)
    assert _cli(tmp_path, "--ops-out", str(ops_out)) == 0
    assert list(ops_mod.read_ops(str(ops_out))) == []
    (tile_dir,) = review_region._find_tile_dirs(str(tmp_path / "tiles"))
    answer = json.loads(open(os.path.join(tile_dir, "answer.json")).read())
    assert {v["verdict"] for v in answer.values()} == {"unsure"}
    assert json.loads(open(os.path.join(tile_dir, "answer.meta.json")).read())["degraded"]
    n_first = len(calls)

    mode["reply"] = None                          # server recovers; same command again
    assert _cli(tmp_path, "--ops-out", str(ops_out), "--limit", "1") == 0
    assert len(calls) > n_first                   # retried, not skipped
    assert list(ops_mod.read_ops(str(ops_out)))   # now the drop is real

    n_second = len(calls)
    assert _cli(tmp_path, "--ops-out", str(ops_out)) == 0
    assert len(calls) == n_second                 # a good answer is resumed


def test_backend_circuit_breaker_flag_aborts_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    from graph_cleanup.backends import local_openai

    _setup(tmp_path)
    monkeypatch.setattr(local_openai, "urllib_transport",
                        lambda *a: (_ for _ in ()).throw(
                            local_openai.LocalHTTPError(400, "bad")))
    ops_out = tmp_path / "ai.ops.jsonl"
    rc = _cli(tmp_path, "--max-consecutive-backend-errors", "1", "--ops-out", str(ops_out))
    assert rc == 2
    assert "consecutive" in capsys.readouterr().err
    assert not ops_out.exists()
    # 0 disables the breaker: the run finishes (unanswered) and exits normally
    assert _cli(tmp_path, "--max-consecutive-backend-errors", "0") == 0


def test_answers_to_ops_refuses_an_answer_recorded_for_another_build(tmp_path, capsys):
    _setup(tmp_path)
    assert review_region.main(["--db", str(tmp_path / "g.sqlite"),
                               "--input-dir", str(tmp_path / "geo"),
                               "--out-dir", str(tmp_path / "tiles"),
                               "--backend", "mock"]) == 0
    (tile_dir,) = review_region._find_tile_dirs(str(tmp_path / "tiles"))
    meta_path = os.path.join(tile_dir, "answer.meta.json")
    meta = json.load(open(meta_path))
    meta["manifest_sha256"] = "0" * 64                       # recorded for another build
    json.dump(meta, open(meta_path, "w"))
    from graph_cleanup import runner
    with pytest.raises(runner.StaleAnswerError):
        runner.answers_to_ops([tile_dir], "t")


def test_stale_answer_makes_the_cli_exit_2_and_write_no_ops(tmp_path, monkeypatch, capsys):
    from graph_cleanup import runner
    _setup(tmp_path)
    args = ["--db", str(tmp_path / "g.sqlite"), "--input-dir", str(tmp_path / "geo"),
            "--out-dir", str(tmp_path / "tiles"), "--backend", "mock"]
    assert review_region.main(args) == 0
    (tile_dir,) = review_region._find_tile_dirs(str(tmp_path / "tiles"))
    meta_path = os.path.join(tile_dir, "answer.meta.json")
    meta = json.load(open(meta_path))
    meta["manifest_sha256"] = "0" * 64
    json.dump(meta, open(meta_path, "w"))
    # run_all would just re-answer a stale tile; stub it to reach answers_to_ops
    monkeypatch.setattr(review_region.runner, "run_all", lambda *a, **k: runner.RunStats())
    ops_out = tmp_path / "ai.ops.jsonl"
    assert review_region.main(args + ["--ops-out", str(ops_out)]) == 2
    assert "different manifest" in capsys.readouterr().err
    assert not ops_out.exists()


@pytest.mark.parametrize("url", ["192.168.10.111:8000/v1", "ftp://h/v1", "http://h:abc/v1"])
def test_bad_local_url_is_rejected_before_any_work(tmp_path, url, capsys):
    with pytest.raises(SystemExit) as ei:
        review_region.main(["--db", str(tmp_path / "missing.sqlite"),
                            "--input-dir", str(tmp_path), "--out-dir", str(tmp_path / "o"),
                            "--backend", "local", "--local-url", url])
    assert ei.value.code == 2
    assert "--local-url" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()          # nothing was loaded or rendered


def test_consecutive_degraded_flag_aborts_the_cli(tmp_path, monkeypatch, capsys):
    from graph_cleanup.backends import local_openai
    _setup(tmp_path)
    monkeypatch.setattr(local_openai, "urllib_transport", lambda *a: {
        "choices": [{"message": {"content": "garbage"}, "finish_reason": "stop"}]})
    assert _cli(tmp_path, "--max-consecutive-degraded", "1") == 2
    assert "unusable" in capsys.readouterr().err
    assert _cli(tmp_path, "--max-consecutive-degraded", "0") == 0
