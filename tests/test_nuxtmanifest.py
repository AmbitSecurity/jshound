"""Nuxt's served app manifest, the Nuxt counterpart to Next's `_buildManifest.js`.

Same principle, different shape: the route list is a DECLARATION by the build rather than an inference
from code, so these tests are about not losing that certainty and not overstating it. The two failure
modes that matter are claiming a stack that is not in evidence, and reporting an app with no manifest
as an app with zero routes.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound.nuxtmanifest as nm                                       # noqa: E402

DOC = '<html><head><script type="module" src="/_nuxt/Bx1k2.js"></script></head></html>'


def fake_http(pages, final=None):
    """Serve a dict of url -> (status, body); anything unlisted 404s with an SPA index.html.

    `final` makes the document appear to have been redirected there, which is what `with_url` exists
    to report.
    """
    def get(url, timeout=30, with_url=False):
        st, body = pages.get(url, (404, "<!DOCTYPE html><html><body>not found</body></html>"))
        return (st, body, final or url) if with_url else (st, body)
    return get


def wire(monkeypatch, pages, final=None):
    monkeypatch.setattr(nm, "http_get", fake_http(pages, final))


# ---- finding the assets base --------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("/_nuxt/entry.abc.js", "https://x.test/_nuxt/"),
    ("https://cdn.test/app/_nuxt/entry.js", "https://cdn.test/app/_nuxt/"),   # CDN + buildAssetsDir
    ("/base/_nuxt/entry.js", "https://x.test/base/_nuxt/"),                   # app.baseURL
])
def test_the_assets_base_is_read_from_the_document(ref, expected):
    """`buildAssetsDir` and `app.baseURL` both move this, and a CDN moves it to another host, the
    same trap that cost 293 routes when the Next manifest URL was built from the origin."""
    html = f'<script src="{ref}"></script>'
    assert nm.assets_base("https://x.test/page", html) == expected


def test_no_base_is_claimed_for_an_app_that_is_not_nuxt():
    assert nm.assets_base("https://x.test/", '<script src="/static/chunks/a.js"></script>') == ""
    assert nm.assets_base("https://x.test/", "") == ""


# ---- the happy path -----------------------------------------------------------------------------

@pytest.fixture
def ok(monkeypatch):
    wire(monkeypatch, {
        "https://x.test/": (200, DOC),
        "https://x.test/_nuxt/builds/latest.json": (200, json.dumps({"id": "bid-1", "timestamp": 9})),
        "https://x.test/_nuxt/builds/meta/bid-1.json": (200, json.dumps({
            "id": "bid-1", "timestamp": 9,
            "prerendered": ["/docs/intro", "/", "/docs/intro", "/blog/a"],
            "matcher": {"static": {"/admin": {"ssr": False}},
                        "wildcard": {"/api": {"proxy": "https://api.internal/**"}},
                        "dynamic": {}},
        })),
    })
    return nm.read("https://x.test/")


def test_prerendered_routes_are_reported(ok):
    assert ok["available"] is True
    assert ok["routes"] == ["/", "/blog/a", "/docs/intro"]      # sorted, deduplicated


def test_the_build_id_comes_from_latest_json(ok):
    """Two-step by design: `latest.json` names the build, and only then is the meta file known. The
    id is never guessed from the document."""
    assert ok["build_id"] == "bid-1"
    assert ok["manifest_urls"][1].endswith("/builds/meta/bid-1.json")


def test_route_rules_are_reported_with_their_pattern_and_kind(ok):
    """`routeRules` is how an app states that `/api/**` is proxied somewhere, the destination is
    internal structure nothing in the client links to."""
    by_pattern = {r["pattern"]: r for r in ok["route_rules"]}
    assert by_pattern["/api"]["rule"] == {"proxy": "https://api.internal/**"}
    assert by_pattern["/api"]["kind"] == "wildcard"
    assert by_pattern["/admin"]["kind"] == "static"


# ---- the ways it is unavailable, which are not the same as zero routes --------------------------

def test_a_site_that_is_not_nuxt_says_so(monkeypatch):
    wire(monkeypatch, {"https://x.test/": (200, "<html><body>plain</body></html>")})
    d = nm.read("https://x.test/")
    assert d["available"] is False and d["is_nuxt"] is False
    assert "not a Nuxt app" in d["reason"]


def test_a_nuxt_app_serving_no_manifest_is_not_reported_as_zero_routes(monkeypatch):
    """`experimental.appManifest: false` means there is nothing to read. Reporting that as an app with
    no routes would be a false negative dressed up as a measurement."""
    wire(monkeypatch, {"https://x.test/": (200, DOC)})      # latest.json 404s with an index.html
    d = nm.read("https://x.test/")
    assert d["available"] is False and d["is_nuxt"] is True
    assert "routes" not in d
    assert "appManifest:false" in d["reason"] and "status=404" in d["reason"]


def test_a_block_in_front_of_the_app_is_not_mistaken_for_an_absent_manifest(monkeypatch):
    """www.gitlab.com is Nuxt and answers 403 here. "Blocked" and "not built with one" are different
    facts about the target and the first version reported both as the second."""
    wire(monkeypatch, {"https://x.test/": (200, DOC),
                       "https://x.test/_nuxt/builds/latest.json": (403, "<html>denied</html>")})
    d = nm.read("https://x.test/")
    assert d["available"] is False and d["status"] == 403
    assert "blocked" in d["reason"] and "appManifest" not in d["reason"]


def test_a_static_host_answering_a_miss_with_html_is_not_parsed_as_a_manifest(monkeypatch):
    """A 200 is not evidence of a manifest: static hosts serve the SPA shell for any unknown path."""
    wire(monkeypatch, {"https://x.test/": (200, DOC),
                       "https://x.test/_nuxt/builds/latest.json": (200, "<!DOCTYPE html><html>")})
    d = nm.read("https://x.test/")
    assert d["available"] is False and "not JSON" in d["reason"]


def test_a_manifest_without_a_build_id_is_rejected(monkeypatch):
    wire(monkeypatch, {"https://x.test/": (200, DOC),
                       "https://x.test/_nuxt/builds/latest.json": (200, json.dumps({"timestamp": 1}))})
    d = nm.read("https://x.test/")
    assert d["available"] is False and "no build id" in d["reason"]


def test_a_server_rendered_app_with_nothing_prerendered_reports_an_empty_list(monkeypatch):
    """Honest zero: the manifest IS readable and declares no prerendered routes. Distinct from the
    cases above, where the manifest could not be read at all."""
    wire(monkeypatch, {
        "https://x.test/": (200, DOC),
        "https://x.test/_nuxt/builds/latest.json": (200, json.dumps({"id": "b"})),
        "https://x.test/_nuxt/builds/meta/b.json": (200, json.dumps({"prerendered": []})),
    })
    d = nm.read("https://x.test/")
    assert d["available"] is True and d["routes"] == [] and d["route_rules"] == []


def test_an_unreachable_document_is_not_reported_as_a_missing_manifest(monkeypatch):
    monkeypatch.setattr(nm, "http_get",
                        lambda u, timeout=30, with_url=False: (None, "", u) if with_url else (None, ""))
    d = nm.read("https://x.test/")
    assert d["available"] is False and "document unreachable" in d["reason"]


def test_a_non_string_entry_in_prerendered_is_ignored(monkeypatch):
    wire(monkeypatch, {
        "https://x.test/": (200, DOC),
        "https://x.test/_nuxt/builds/latest.json": (200, json.dumps({"id": "b"})),
        "https://x.test/_nuxt/builds/meta/b.json": (200, json.dumps({"prerendered": ["/a", None, 7]})),
    })
    assert nm.read("https://x.test/")["routes"] == ["/a"]


def test_a_route_rule_pattern_is_reported_as_a_route_in_its_own_field(ok):
    """Verified on unjs.io: 64 of its 66 rule patterns are absent from `prerendered` and each answers
    302, where an invented path answers 404. Separate field because "rendered" and "declared to match"
    are different claims."""
    assert ok["rule_routes"] == ["/admin", "/api"]
    assert not set(ok["rule_routes"]) & set(ok["routes"])


def test_a_rule_pattern_that_is_already_prerendered_is_not_counted_twice(monkeypatch):
    wire(monkeypatch, {
        "https://x.test/": (200, DOC),
        "https://x.test/_nuxt/builds/latest.json": (200, json.dumps({"id": "b"})),
        "https://x.test/_nuxt/builds/meta/b.json": (200, json.dumps({
            "prerendered": ["/a"], "matcher": {"static": {"/a": {"ssr": False}, "/b": {}}}})),
    })
    d = nm.read("https://x.test/")
    assert d["routes"] == ["/a"] and d["rule_routes"] == ["/b"]


def test_relative_assets_resolve_against_the_url_the_document_came_from(monkeypatch):
    """kit.svelte.dev redirects to svelte.dev and links `./_app/...`. Resolving that against the
    REQUESTED url pointed the whole lookup at the wrong host, and the app was reported as having no
    route table when it has one. The same trap applies to every reader here, so it is fixed in
    `http_get` and tested where it was found."""
    wire(monkeypatch, {
        "https://asked.test/": (200, DOC),
        "https://real.test/_nuxt/builds/latest.json": (200, json.dumps({"id": "b"})),
        "https://real.test/_nuxt/builds/meta/b.json": (200, json.dumps({"prerendered": ["/x"]})),
    }, final="https://real.test/")
    d = nm.read("https://asked.test/")
    assert d["available"] is True
    assert d["assets_base"] == "https://real.test/_nuxt/"
    assert d["routes"] == ["/x"]
