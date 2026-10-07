"""Request headers across the spellings that reach a sink. Headers are where auth lives, so an
endpoint reporting none when it sends an Authorization header is worse than reporting nothing."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "headers")


def _by_path():
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", FIX],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-1200:]
    out = {}
    for x in json.loads(r.stdout):
        if (x.get("_kind") or "endpoint") == "endpoint":
            out[str(x.get("template"))] = x.get("req_headers") or {}
    return out


def test_inline_object_headers_keep_their_values():
    h = _by_path()["/v1/a"]
    assert h["authorization"].startswith("Bearer "), h
    assert h["x-org-id"] == "42"


def test_a_new_headers_binding_assembled_with_set():
    """`const h = new Headers(); h.set("X-Api-Key", k); fetch(u, {headers: h})`. `new Headers({…})`
    already worked; this spelling reported no headers while the names sat two statements above."""
    h = _by_path()["/v1/b"]
    assert "x-api-key" in h and "x-trace" in h, h


def test_client_config_headers():
    assert "x-tenant" in _by_path()["/v1/c"]


def test_xhr_set_request_header():
    assert "X-CSRF-Token" in _by_path()["/v1/d"]


def test_req_headers_may_be_a_list_of_names():
    """req_headers is a dict when the values are known and a LIST OF NAMES when only the names are —
    rtk-query reports the second shape. Assuming the dict raised AttributeError and crashed the
    whole HAR export on Grafana, so nothing was written at all."""
    import sys as _s
    _s.path.insert(0, ROOT)
    from jshound.export import har, openapi
    rec = [{"method": "POST", "template": "/a", "_host": "h",
            "req_headers": ["x-tenant-id", "x-trace"]},
           {"method": "POST", "template": "/b", "_host": "h",
            "req_headers": {"authorization": "Bearer x"}}]
    doc = har.build(rec)
    names = {h["name"] for e in doc["log"]["entries"] for h in e["request"]["headers"]}
    assert {"x-tenant-id", "x-trace", "authorization"} <= names, names


def test_headers_reach_the_openapi_document():
    """A name with no value is still what a tester has to send, so it belongs on the operation."""
    import sys as _s
    _s.path.insert(0, ROOT)
    from jshound.export import openapi
    rec = [{"method": "POST", "template": "/a", "_host": "h",
            "req_headers": ["x-tenant-id", "content-type"]}]
    op = openapi.build(rec)["paths"]["/a"]["post"]
    hdr = {p["name"] for p in op["parameters"] if p["in"] == "header"}
    assert hdr == {"x-tenant-id"}, hdr      # content-type is carried by the media type already


# ── lowercase `x-` names ─────────────────────────────────────────────────────────────────────────

def _xh_names():
    import json
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, "tests", "fixtures", "x-headers")
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", d],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]
    recs = json.loads(r.stdout)
    assert recs, "analyzed nothing"
    return {x.get("name") for x in recs if x.get("_kind") == "client-header"}


def test_a_lowercase_x_dashed_name_is_a_header():
    """273 distinct `x-*` keys appear across the corpora and nearly all are genuine. The blanket
    rejection of lowercase-dashed names was costing `x-amz-user-agent` and `x-deployment-id`."""
    got = _xh_names()
    assert "x-deployment-id" in got
    assert "x-amz-user-agent" in got


def test_a_css_ish_lowercase_key_is_still_not_a_header():
    """Why the rejection existed: `s["font-size"] = "12px"` must not register."""
    assert "font-size" not in _xh_names()


def test_a_dom_attribute_write_is_not_a_header():
    """Popper.js does `e.attributes["x-out-of-boundaries"] = ""` on a tooltip element — markup, not a
    request. `attributes`/`dataset`/`style`/`classList` are DOM-fixed property names a minifier cannot
    rename, which is what makes the receiver usable here where a name test is not."""
    got = _xh_names()
    assert "x-out-of-boundaries" not in got
    assert "x-chart-id" not in got
