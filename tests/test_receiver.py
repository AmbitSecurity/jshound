"""What the receiver IS, rather than what the argument looks like.

`.get`/`.post`/`.put`/`.delete` are HTTP verbs and they are also ordinary methods on Map,
URLSearchParams, Headers and FormData. The member sink fired on all of them, and the only thing
between that and a fabricated endpoint was whether the first argument looked path-shaped — which
`"user/profile"` does. Arguing about the argument cannot fix that. Where the receiver is a local
binding its construction site says what it is, and a value built by `new Map()` has no endpoints.
"""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "non-http-receivers")


def _records():
    r = _lib.analyze(FIX)
    assert r.returncode == 0, r.stderr[-1200:]
    recs = json.loads(r.stdout)
    assert recs, "analyzed nothing — the fixture must be a directory with real records"
    return recs


def _templates():
    return {str(r.get("template") or "") for r in _records() if r.get("template")}


def test_a_map_lookup_is_not_an_endpoint():
    """`cache.get("user/profile")` — the path is fine, the receiver is a Map."""
    assert "user/profile" not in _templates()


def test_a_search_param_read_is_not_an_endpoint():
    assert "redirect/next" not in _templates()


def test_a_header_read_is_not_an_endpoint():
    assert "content/type" not in _templates()


def test_a_formdata_read_is_not_an_endpoint():
    assert "upload/file" not in _templates()


def test_a_weakmap_lookup_is_not_an_endpoint():
    assert "org/members" not in _templates()


def test_a_resolved_real_client_still_resolves():
    """`axios.create({baseURL})` is a client, and the check must not touch it."""
    assert "/v1/users" in _templates()


def test_a_client_from_an_unresolvable_factory_still_resolves():
    """The check is one-sided: it rejects only what it can prove is not a client. `makeClient()`
    proves nothing either way, so the record is kept exactly as it was before."""
    assert "/v2/orders" in _templates()


def test_a_bare_module_call_still_resolves():
    assert "/v3/invoices" in _templates()


def test_nothing_else_was_quietly_dropped():
    """The three real endpoints and no fabrications — stated as an exact set so a future change that
    removes a real one fails here rather than looking like an improvement."""
    got = {t for t in _templates() if t.startswith("/v")}
    assert got == {"/v1/users", "/v2/orders", "/v3/invoices"}
