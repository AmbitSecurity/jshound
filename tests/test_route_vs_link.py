"""A link out is not a route in.

The sibling test that promotes a path literal to a client-side route is "the object declaring this
URL also names the view that renders it". A link table sitting beside a component satisfies it, so a
testimonial wall of tweets read as routes, 51 of the 53 reported for supabase.com/dashboard were
`https://x.com/<user>/status/<id>`. This app's router cannot render x.com.

The URL stays in the corpus as a path literal, because it IS in the bundle; what it loses is the
claim to be attack surface, which is what `-f urls` and `-f table` list.
"""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "route-vs-link")


def records():
    r = _lib.analyze(FIX, "--host", "app.test")
    assert r.returncode == 0, r.stderr[-900:]
    return json.loads(r.stdout)


def test_an_absolute_url_is_never_a_client_side_route():
    routes = [x for x in records() if x.get("_kind") == "route"]
    for r in routes:
        t = str(r.get("template") or "")
        assert "x.com" not in t and "twitter.com" not in t, f"link out reported as a route: {t}"


def test_the_tweets_are_demoted_not_deleted():
    """Demoted to a path literal, not dropped: the URL IS in the bundle and a reader may still want
    it. Run WITHOUT `--host`, because with one these are attributed to x.com and filtered out
    entirely, two separate mechanisms, and this asserts the demotion rather than the filter."""
    r = _lib.analyze(FIX)
    assert r.returncode == 0, r.stderr[-900:]
    recs = json.loads(r.stdout)
    lits = [str(x.get("template") or "") for x in recs if x.get("_kind") == "path-literal"]
    assert any("x.com/someone/status" in t for t in lits), lits
    assert not [x for x in recs if x.get("_kind") == "route" and "x.com" in str(x.get("template"))]


def test_naming_a_host_drops_another_origins_links_altogether():
    """The second mechanism: origin attribution. A literal served from x.com is not this host's."""
    assert not [x for x in records() if "x.com" in str(x.get("template") or "")]


def test_a_real_client_side_route_still_resolves():
    routes = {str(x.get("template") or "") for x in records() if x.get("_kind") == "route"}
    assert any("/account/settings" in t for t in routes), routes


def test_no_link_out_reaches_the_url_or_table_export():
    for fmt in ("urls", "table"):
        r = _lib.analyze(FIX, "--host", "app.test", "-f", fmt)
        assert r.returncode == 0, r.stderr[-600:]
        assert "x.com" not in r.stdout and "twitter.com" not in r.stdout, (fmt, r.stdout)
