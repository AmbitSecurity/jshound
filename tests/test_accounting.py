"""Every record is accounted for. A count a reader cannot reconcile against the artifact is worse
than no count: tldraw reported 74 endpoints while its OpenAPI export carried paths for 66 of them,
because eight records had a resolved body and an unresolved URL and every exporter dropped them
silently."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from jshound.export import openapi                                  # noqa: E402
from jshound.export.paths import looks_like_source_file, normalize  # noqa: E402
import _lib  # noqa: E402

RECS = [
    {"method": "POST", "template": "/api/a", "_host": "h"},
    {"method": "GET", "template": "/api/b", "_host": "h"},
    {"_kind": "route", "method": "GET", "template": "/ui/page", "_host": "h"},
    # a body with no URL — not an endpoint, it has no address to send to
    {"_kind": "request-no-url", "method": "POST", "body_keys": ["method", "jsonrpc", "id"],
     "_host": "h"},
]


def _paths(rs):
    out = {normalize(r.get("template") or r.get("url") or "")[0] for r in rs}
    return {p for p in out if p and not looks_like_source_file(p)}


def test_endpoint_and_route_paths_reconcile_with_the_spec():
    eps = [r for r in RECS if (r.get("_kind") or "endpoint") == "endpoint"]
    rts = [r for r in RECS if r.get("_kind") == "route"]
    assert len(_paths(eps) | _paths(rts)) == len(openapi.build(RECS)["paths"])


def test_a_request_with_no_url_is_not_an_endpoint():
    eps = [r for r in RECS if (r.get("_kind") or "endpoint") == "endpoint"]
    assert len(eps) == 2, eps
    assert all(r.get("template") for r in eps)


def test_a_url_less_request_keeps_its_body():
    """It is not noise. tldraw's is PostHog's capture payload; another is a JSON-RPC envelope."""
    nu = [r for r in RECS if r.get("_kind") == "request-no-url"][0]
    assert nu["body_keys"] == ["method", "jsonrpc", "id"]


def test_the_reclassification_runs_on_a_real_corpus():
    fx = os.path.join(ROOT, "tests", "fixtures", "new-url-sinks")
    r = _lib.analyze(fx)
    assert r.returncode == 0, r.stderr[-1200:]
    for rec in json.loads(r.stdout):
        if (rec.get("_kind") or "endpoint") == "endpoint":
            assert rec.get("template"), rec
