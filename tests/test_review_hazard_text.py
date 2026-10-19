"""scripts/review_hazard_text.py: list / propose (mocked backend) / accept / reject, date validation."""
import importlib.util
import json
import os
import sqlite3

import pytest
from shapely.geometry import Point

import hazards

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("review_hazard_text", os.path.join(ROOT, "scripts", "review_hazard_text.py"))
rht = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rht)

WINTER = "Winter Resting/Foraging area from November 1 until May 15"


def raw(text, ref="L1", cls="RESARE", name="Bollen"):
    return hazards._make_record(cls, {"RESTRN": [7], "INFORM": text, "LNAM": ref, "OBJNAM": name}, "X.000", 1,
                                Point(3.5, 51.5))


@pytest.fixture
def records():
    return [raw(WINTER, "L1"), raw("Passage prohibited. Very dangerous tidal streams", "L2"),
            raw("Closed during the breeding season", "L3", name="Vogels")]


@pytest.fixture
def cands(records):
    return rht.from_records(records)


def good(**kw):
    a = {"period_start": "--1101", "period_end": "--0515", "valid_from": None, "valid_until": None,
         "note": "from November 1 until May 15", "confident": True}
    a.update(kw)
    return a


class FakeBackend:
    name = "llm:fake"

    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, cand):
        self.seen.append(cand["id"])
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_candidates_are_only_text_review_records(records, cands):
    assert [c["id"] for c in cands] == [str(hazards.notice_id(records[0])), str(hazards.notice_id(records[2]))]
    assert cands[0]["class"] == "RESARE" and cands[0]["tier"] == "prohibited"
    assert cands[0]["text_sha1"] == hazards.text_sha1(records[0])


def test_list_prints_id_class_tier_name_text(cands):
    lines = []
    rht.cmd_list(cands, {cands[1]["id"]: {"status": "proposed"}}, out=lines.append)
    assert len(lines) == 3
    f = lines[0].split("\t")
    assert f[0] == cands[0]["id"] and f[1:3] == ["RESARE", "prohibited"] and f[3] == "[-]" and WINTER in f[5]
    assert "[proposed]" in lines[1] and lines[2].startswith("2 notice")


def test_list_from_db(tmp_path):
    p = str(tmp_path / "x.sqlite")
    c = sqlite3.connect(p)
    c.executescript(hazards.NOTICES_DDL)
    c.execute("INSERT INTO notices (id, source_class, severity, name, text, attributes, geometry, text_review) "
              "VALUES (1,'RESARE',3,'N',?, '{}', '{}', 1)", (WINTER,))
    c.execute("INSERT INTO notices (id, source_class, severity, name, text, attributes, geometry, text_review) "
              "VALUES (2,'RESARE',3,'M','x', '{}', '{}', 0)")
    c.commit()
    out = rht.from_db(p)
    assert [(x["id"], x["tier"]) for x in out] == [("1", "prohibited")]
    assert out[0]["text_sha1"] == hazards.text_sha1({"name": "N", "text": WINTER})


def test_list_from_db_requires_schema3_columns(tmp_path):
    p = str(tmp_path / "old.sqlite")
    sqlite3.connect(p).executescript("CREATE TABLE notices (id INTEGER, text TEXT);")
    with pytest.raises(SystemExit):
        rht.from_db(p)


def test_propose_writes_proposed_never_accepted(cands):
    ov, lines = {}, []
    b = FakeBackend([good(), good(period_start=None, period_end=None, note="no dates", confident=False)])
    assert rht.cmd_propose(cands, ov, b, out=lines.append) == (2, 0)
    e = ov[cands[0]["id"]]
    assert e["status"] == "proposed" and e["proposed_by"] == "llm:fake" and e["reviewed_by"] is None
    assert (e["period_start"], e["period_end"], e["text_sha1"]) == ("--1101", "--0515", cands[0]["text_sha1"])
    assert e["confident"] is True and ov[cands[1]["id"]]["confident"] is False
    assert all(x["status"] == "proposed" for x in ov.values())


def test_propose_skips_existing_entries_and_bad_answers(cands):
    ov = {cands[0]["id"]: {"status": "rejected", "text_sha1": cands[0]["text_sha1"]}}
    b = FakeBackend([{"period_start": "--1301", **{k: None for k in ("period_end", "valid_from", "valid_until")},
                      "note": "x", "confident": True}])
    assert rht.cmd_propose(cands, ov, b, out=lambda *_: None) == (0, 1)
    assert b.seen == [cands[1]["id"]] and cands[1]["id"] not in ov and ov[cands[0]["id"]]["status"] == "rejected"


@pytest.mark.parametrize("ans", [{"period_start": "--1101"}, "text", {**good(), "confident": "yes"},
                                 {**good(), "valid_from": "2022-01-01"}, ValueError("not json")])
def test_propose_rejects_malformed_answers(cands, ans):
    ov = {}
    assert rht.cmd_propose(cands[:1], ov, FakeBackend([ans]), out=lambda *_: None) == (0, 1) and ov == {}


def test_propose_without_backend_fails_clearly(cands):
    ov = {}
    with pytest.raises(rht.NoBackend):
        rht.cmd_propose(cands, ov, FakeBackend([rht.NoBackend("cannot reach")]), out=lambda *_: None)
    assert ov == {}


def test_llm_backend_sends_text_and_parses_reply():
    seen = {}

    def transport(url, payload, headers, timeout):
        seen.update(url=url, payload=payload)
        return {"choices": [{"message": {"content": "```json\n" + json.dumps(good()) + "\n```"}}]}
    b = rht.LlmBackend("http://h:1/v1", "m", transport=transport)
    ans = b({"id": "1", "class": "RESARE", "name": "N", "text": WINTER})
    assert rht.parse_answer(ans)["period_start"] == "--1101"
    assert seen["url"].endswith("/chat/completions") and WINTER in seen["payload"]["messages"][1]["content"]
    assert b.name == "llm:m"


def test_llm_backend_unreachable_is_nobackend():
    from graph_cleanup.backends.local_openai import LocalConnectionError

    def transport(url, payload, headers, timeout):
        raise LocalConnectionError("refused")
    with pytest.raises(rht.NoBackend, match="cannot reach the LLM backend"):
        rht.LlmBackend("http://h:1/v1", "m", transport=transport)({"id": "1", "class": "R", "name": None, "text": "t"})


def test_manual_proposal_validates_dates(cands):
    ov = {}
    e = rht.cmd_manual(cands, ov, cands[0]["id"], {"period_start": "--1101", "period_end": "--0515", "note": "n"}, "marcel")
    assert e["status"] == "proposed" and e["proposed_by"] == "marcel" and e["text_sha1"] == cands[0]["text_sha1"]
    with pytest.raises(ValueError):
        rht.cmd_manual(cands, ov, cands[0]["id"], {"period_start": "1101-"}, "m")
    with pytest.raises(SystemExit):
        rht.cmd_manual(cands, ov, "999", {"period_start": "--1101"}, "m")        # unknown id, no sha1
    rht.cmd_manual([], ov, "999", {"period_end": "--0515"}, "m", text_sha1="a" * 40)
    assert ov["999"]["text_sha1"] == "a" * 40


def test_accept_and_reject_set_status_reviewer_and_time(cands):
    ov = {}
    rht.cmd_propose(cands, ov, FakeBackend([good(), good()]), out=lambda *_: None)
    a, b = cands[0]["id"], cands[1]["id"]
    rht.cmd_review(ov, [a], "accepted", "marcel")
    rht.cmd_review(ov, [b], "rejected", "marcel")
    assert ov[a]["status"] == "accepted" and ov[a]["reviewed_by"] == "marcel" and ov[a]["reviewed_at"].endswith("Z")
    assert ov[b]["status"] == "rejected"
    with pytest.raises(SystemExit):
        rht.cmd_review(ov, ["404"], "accepted", "m")
    with pytest.raises(SystemExit):
        rht.cmd_review(ov, [a], "accepted", "")


def test_accept_revalidates_dates_and_is_atomic(cands):
    ov = {cands[0]["id"]: {"period_start": "--1101", "text_sha1": "a" * 40, "status": "proposed"},
          cands[1]["id"]: {"period_start": "--9999", "text_sha1": "a" * 40, "status": "proposed"}}
    with pytest.raises(SystemExit):
        rht.cmd_review(ov, [cands[0]["id"], cands[1]["id"]], "accepted", "m")
    assert ov[cands[0]["id"]]["status"] == "proposed"      # nothing applied


def test_accepted_entry_is_applied_by_the_builders_and_proposed_is_not(records, cands):
    ov = {}
    rht.cmd_propose(cands, ov, FakeBackend([good(), good()]), out=lambda *_: None)
    rht.cmd_review(ov, [cands[0]["id"]], "accepted", "marcel")
    r0, r2 = raw(WINTER, "L1"), raw("Closed during the breeding season", "L3", name="Vogels")
    assert hazards.apply_overrides([r0, r2], ov) == 1
    assert hazards.is_time_limited(r0) and not hazards.is_time_limited(r2) and r2["text_review"] == 1


def test_main_end_to_end_with_files(tmp_path, monkeypatch, capsys, records):
    ovp = str(tmp_path / "ov.json")
    monkeypatch.setattr(rht, "from_cells", lambda d, b: rht.from_records(records))
    base = ["--overrides", ovp]
    src = ["--cells-dir", "x", "--bbox", "3,51,4,52"]
    assert rht.main(base + ["list"] + src) == 0
    assert "2 notice(s)" in capsys.readouterr().out
    assert rht.main(base + ["propose"] + src, backend=FakeBackend([good(), good(confident=False)])) == 0
    ov = json.load(open(ovp))
    assert len(ov) == 2 and {e["status"] for e in ov.values()} == {"proposed"}
    nid = next(iter(ov))
    assert rht.main(base + ["accept", nid, "--by", "marcel"]) == 0
    assert json.load(open(ovp))[nid]["status"] == "accepted"
    with pytest.raises(SystemExit):                       # --by is required
        rht.main(base + ["reject", nid])
    assert rht.main(base + ["reject", nid, "--by", "marcel"]) == 0
    assert json.load(open(ovp))[nid]["status"] == "rejected"


def test_main_manual_accepts_dash_dash_dates(tmp_path, records, monkeypatch):
    ovp = str(tmp_path / "ov.json")
    monkeypatch.setattr(rht, "from_cells", lambda d, b: rht.from_records(records))
    nid = str(hazards.notice_id(records[0]))
    rc = rht.main(["--overrides", ovp, "propose", "--cells-dir", "x", "--bbox", "3,51,4,52", "--manual", nid,
                   "--period-start", "--1101", "--period-end", "--0515", "--note", "by hand", "--by", "marcel"])
    assert rc == 0
    e = json.load(open(ovp))[nid]
    assert (e["period_start"], e["period_end"], e["status"], e["proposed_by"]) == ("--1101", "--0515", "proposed", "marcel")
    with pytest.raises(SystemExit):
        rht.main(["--overrides", ovp, "propose", "--manual", nid, "--period-start=--1101", "--period-end=--13",
                  "--text-sha1", "a" * 40])


def test_main_propose_without_reachable_backend(tmp_path, monkeypatch, capsys, records):
    from graph_cleanup.backends.local_openai import LocalConnectionError
    ovp = str(tmp_path / "ov.json")
    monkeypatch.setattr(rht, "from_cells", lambda d, b: rht.from_records(records))

    def dead(url, payload, headers, timeout):
        raise LocalConnectionError("refused")
    monkeypatch.setattr("graph_cleanup.backends.local_openai.urllib_transport", dead)
    rc = rht.main(["--overrides", ovp, "propose", "--cells-dir", "x", "--bbox", "3,51,4,52",
                   "--base-url", "http://127.0.0.1:9/v1"])
    assert rc == 2 and "cannot reach the LLM backend" in capsys.readouterr().err
    assert not os.path.exists(ovp)


def test_stale_entry_is_reproposed_matching_ones_are_not(cands):
    ov = {cands[0]["id"]: {"status": "accepted", "text_sha1": "0" * 40, "period_start": "--0101"},
          cands[1]["id"]: {"status": "rejected", "text_sha1": cands[1]["text_sha1"]}}
    b = FakeBackend([good()])
    assert rht.cmd_propose(cands, ov, b, out=lambda *_: None) == (1, 0)
    assert b.seen == [cands[0]["id"]]
    assert ov[cands[0]["id"]]["status"] == "proposed" and ov[cands[0]["id"]]["text_sha1"] == cands[0]["text_sha1"]
    assert ov[cands[1]["id"]]["status"] == "rejected"


def test_accept_warns_when_only_one_period_end_is_set(capsys):
    ov = {"1": {"period_start": "--1101", "period_end": None, "text_sha1": "a" * 40, "status": "proposed"}}
    rht.cmd_review(ov, ["1"], "accepted", "m")
    assert "only one of period_start/period_end" in capsys.readouterr().err


def test_dash_dash_dd_form_in_argv(tmp_path, records, monkeypatch):
    ovp = str(tmp_path / "ov.json")
    nid = str(hazards.notice_id(records[0]))
    assert rht.main(["--overrides", ovp, "propose", "--manual", nid, "--period-start", "---15", "--text-sha1", "a" * 40]) == 0
    assert json.load(open(ovp))[nid]["period_start"] == "---15"
