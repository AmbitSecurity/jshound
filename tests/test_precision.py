"""precision finds endpoints the extractor INVENTED. residual cannot: it measures literals that went
unclaimed, and a fabrication is a claim with no literal behind it — an extractor can score a perfect
zero on residual while reporting URLs that do not exist."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.precision import check   # noqa: E402


def _run(js, records):
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.js"), "w").write(js)
    rp = os.path.join(d, "r.json")
    json.dump(records, open(rp, "w"))
    return check(d, rp)


SRC = ('const api = createApi({});'
       'function D(){ const [f] = api.endpoints.upsertRuleGroupForNamespace.useMutation(); }'
       'fetch("/api/v1/users"); fetch("/t/events"); var L = "MM/DD/YYYY";')


def test_a_path_assembled_from_identifiers_is_flagged():
    """Grafana reported /api/trpc/hK.endpoints.<name> while "/api/trpc" appears nowhere in it."""
    r = _run(SRC, [{"template": "/api/trpc/hK.endpoints.upsertRuleGroupForNamespace", "sink": "trpc"}])
    assert r["findings"]["not-in-corpus"] == 1, r


def test_a_date_format_is_flagged():
    r = _run(SRC, [{"template": "MM/DD/YYYY", "sink": "L.get"}])
    assert r["findings"]["date-format"] == 1, r


def test_real_paths_are_not_flagged():
    """Including a one-character segment: /t/events is a legitimate analytics path."""
    r = _run(SRC, [{"template": "/api/v1/users", "sink": "fetch"},
                   {"template": "/t/events", "sink": "sendBeacon"}])
    assert r["questionable"] == 0, r


def test_a_path_built_from_a_base_still_passes():
    """A client that joins a base and a suffix never writes the join down, so requiring the whole
    path to appear would flag real endpoints — that version flagged 44% of tldraw's."""
    r = _run('const base="https://api.x.test"; fetch(base + "/v2/orders");',
             [{"template": "https://api.x.test/v2/orders", "sink": "fetch"}])
    assert r["questionable"] == 0, r


def test_a_path_holding_a_raw_space_is_flagged():
    """The class `not-in-corpus` is blind to. Grafana defines its mixed-datasource sentinel in one
    object literal, `{type: "mixed", uid: "-- Mixed --"}`, and the extractor's property index filled
    every `${x.uid}` slot with it — 20 addresses like `/datasources/uid/-- Mixed --/health`. EVERY
    fragment of that path is written down somewhere, so the fragment test passes it, and the checker
    reported 4 questionable while 20 fabrications sat in the output. A raw space cannot be in a URL."""
    r = _run('const L={type:"mixed",uid:"-- Mixed --"};fetch("/api/datasources/uid/"+x.uid+"/health");',
             [{"template": "/api/datasources/uid/-- Mixed --/health", "sink": "rtk-query"}])
    assert r["findings"]["unencoded-space"] == 1, r
    # every fragment IS in the corpus, which is exactly why the other check cannot see this
    assert r["findings"]["not-in-corpus"] == 0, r


def test_a_slot_name_cannot_trip_the_space_check():
    """Tested on the skeleton, so a slot is never the thing that flags a path."""
    r = _run('fetch("/api/datasources/uid/"+u+"/health");',
             [{"template": "/api/datasources/uid/{-- Mixed --}/health", "sink": "x"}])
    assert r["findings"]["unencoded-space"] == 0, r


def test_a_real_path_with_an_encoded_space_is_not_flagged():
    """`%20` is how a space appears in a URL, and it is not whitespace."""
    r = _run('fetch("/api/search?q=hello%20world");',
             [{"template": "/api/search?q=hello%20world", "sink": "fetch"}])
    assert r["findings"]["unencoded-space"] == 0, r
