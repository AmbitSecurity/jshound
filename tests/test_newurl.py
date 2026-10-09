"""`new URL(path, base)` across sinks. Only the fetch branch understood it, so the same call through
an axios-style client or `new Request` resolved to nothing at all."""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "new-url-sinks")


def _endpoints():
    r = _lib.analyze(FIX)
    assert r.returncode == 0, r.stderr[-1500:]
    return {(x.get("method"), x.get("template")) for x in json.loads(r.stdout)
            if (x.get("_kind") or "endpoint") == "endpoint"}


def test_new_url_resolves_through_every_sink():
    got = _endpoints()
    for want in (("POST", "https://api.demo.test/v2/axios-style"),
                 ("GET", "https://api.demo.test/v2/axios-get"),
                 ("DELETE", "https://api.demo.test/v2/request-obj"),
                 ("POST", "https://api.demo.test/v1/event"),
                 ("PUT", "https://api.demo.test/v1/plain")):
        assert want in got, (want, sorted(got))


def test_private_class_field_base_still_resolves():
    """tldraw's is `fetch(new URL("/v1/event", this.#cfg.endpoint), …)` — the base is a private
    class field."""
    assert ("POST", "https://api.demo.test/v1/logs") in _endpoints()


def _qs(template_fragment):
    import subprocess
    fx = os.path.join(ROOT, "tests", "fixtures", "searchparams")
    r = _lib.analyze(fx)
    assert r.returncode == 0, r.stderr[-1200:]
    for x in json.loads(r.stdout):
        if (x.get("_kind") or "endpoint") == "endpoint" and template_fragment in str(x.get("template")):
            return [q["name"] for q in (x.get("query_schema") or [])]
    return None


def test_searchparams_resolve_through_href():
    """`fetch(u)` and `fetch(u.toString())` both resolved their parameters while `fetch(u.href)` —
    the commonest spelling — reported none, which reads as 'takes no query parameters'."""
    assert _qs("/v1/items") == ["cursor"]


def test_searchparams_including_conditional_and_append():
    got = _qs("/v1/search")
    assert set(got) == {"q", "page", "includeArchived"}, got


def test_a_literal_query_string_is_still_read():
    assert set(_qs("/v1/direct")) == {"term", "limit"}
