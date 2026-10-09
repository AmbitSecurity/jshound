"""Hash routing or path routing, and what is allowed to count as evidence.

A hash route never reaches the server, so it answers 404 to every request; a path route returns the
app shell. Getting this backwards turns a route table from a map into a work list. It used to be a
regex over the chunk source, which over 11 corpora holding 557 route records produced exactly two
labels — and both were wrong.
"""
import collections
import json
import os
import subprocess
import sys
import _lib  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "routing-evidence")


def _routing(case):
    d = os.path.join(FIX, case)
    assert os.path.isdir(d), d
    r = _lib.analyze(d)
    assert r.returncode == 0, r.stderr[-1200:]
    routes = [x for x in json.loads(r.stdout) if x.get("_kind") == "route"]
    assert routes, f"{case}: no routes extracted, so the assertion would be vacuous"
    return collections.Counter(x.get("routing") or "ABSENT" for x in routes)


def test_an_app_that_imports_and_calls_a_hash_router_is_hash_routed():
    """The true positive. None of the 21 real corpora produce one, so it is a fixture or nothing."""
    assert _routing("app-calls-hash") == {"hash": 1}


def test_a_library_defining_and_calling_both_factories_proves_nothing():
    """react-router ships `HashRouter` AND `BrowserRouter`, and each calls its own history factory
    inside its own body — so every bundle containing it contains both calls. Slack's does. The tell
    is that the callee binds to a function declared right there: the library calling itself."""
    assert _routing("library-defines-both") == {"ABSENT": 1}


def test_a_name_in_a_string_or_an_export_map_proves_nothing():
    """Grafana's shape, and the source of the one wrong label the regex ever produced:
    `createHashRouter` appears inside react-router's own error message, and `HashRouter` appears in
    its export map as `HashRouter:()=>At`. A library exporting a router is not an app using one."""
    assert _routing("name-in-string-and-export-map") == {"ABSENT": 1}


def test_the_verdict_covers_routes_defined_in_other_files():
    """The router is constructed in one chunk and the route table can live in another, so a per-chunk
    answer labels some of an app's routes and not the rest. Settled once over the corpus, like the
    module index — the same mistake cost cal.com 42 tRPC endpoints before it was found by measurement."""
    assert _routing("router-in-another-file") == {"hash": 2}


def test_routes_with_no_router_anywhere_are_not_labelled():
    """Stated, never inferred: absent is the honest output when the evidence is not there."""
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(os.path.join(FIX, "router-in-another-file", "routes.js"), d)
        r = _lib.analyze(d)
        routes = [x for x in json.loads(r.stdout) if x.get("_kind") == "route"]
        assert routes and all(x.get("routing") is None for x in routes)
