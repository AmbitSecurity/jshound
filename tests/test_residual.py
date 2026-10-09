"""residual measures what the extractor missed, so a false miss in it is worse than no measurement
at all."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.residual import classify   # noqa: E402


def _run(js, records):
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.js"), "w").write(js)
    rp = os.path.join(d, "r.json")
    json.dump(records, open(rp, "w"))
    return classify(d, rp)


def test_an_absolute_template_claims_the_relative_literal_it_came_from():
    """tldraw writes `fetch(new URL("/v1/event", cfg.endpoint))`, so the literal in the bundle is
    `/v1/event` while the record resolves to `https://clerk-telemetry.com/v1/event`, better than
    the literal. A path-shaped test on the record reported it as MISSED."""
    res = _run('fetch(new URL("/v1/event", e));',
               [{"template": "https://clerk-telemetry.com/v1/event", "method": "POST"}])
    assert res["buckets"].get("MISSED-apiish", 0) == 0, res["buckets"]


def test_a_genuinely_unextracted_api_path_is_still_reported():
    res = _run('var x = "/api/v2/never-extracted";', [{"template": "/something/else"}])
    assert res["buckets"].get("MISSED-apiish", 0) == 1, res["buckets"]


def test_a_relative_template_still_claims_its_literal():
    res = _run('fetch("/api/v1/users");', [{"template": "/api/v1/users", "method": "GET"}])
    assert res["buckets"].get("MISSED-apiish", 0) == 0, res["buckets"]
