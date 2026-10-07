"""Next's served build manifest — the one source in this repo that DECLARES routes instead of
inferring them.

Everything else here reads code and concludes something about it, so it can be wrong about an endpoint.
`_buildManifest.js` is the build's own statement of what it routes, which makes these tests mostly about
not losing or corrupting that certainty: reading the URL from the document rather than constructing it,
keeping the parameter names, not counting route names as chunks, and evaluating untrusted JavaScript
without handing it the process.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.nextmanifest import asset_base_of, manifest_urls      # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "next-build-manifest")
JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "jshound", "js", "nextmanifest.js")


def run(*names, timeout=60):
    r = subprocess.run(["node", JS] + [os.path.join(FIX, n) for n in names],
                       capture_output=True, timeout=timeout)
    return json.loads(r.stdout or b"{}")


@pytest.fixture(scope="module")
def out():
    return run("_buildManifest.js", "_ssgManifest.js", "_clientMiddlewareManifest.js")


def templates(out):
    return {r["template"] for r in out["routes"] + out["api_routes"]}


# ---- finding the manifest in the document -------------------------------------------------------

def test_the_manifest_url_is_read_from_the_document_not_constructed():
    """The bug this exists for: Supabase serves its studio bundle from an `assetPrefix` on another
    host, so `{origin}/_next/static/{buildId}/_buildManifest.js` 404s and all 293 routes were lost.
    The document carries the real absolute URL, query string included."""
    html = ('<script src="https://frontend-assets.supabase.com/studio/dc9/_next/static/'
            'build-TfctsWXpff2fKS/_buildManifest.js?dpl=dpl_8Wk3"></script>')
    urls = manifest_urls("https://supabase.com/dashboard", html)
    assert urls == ["https://frontend-assets.supabase.com/studio/dc9/_next/static/"
                    "build-TfctsWXpff2fKS/_buildManifest.js?dpl=dpl_8Wk3"]


def test_a_relative_reference_resolves_against_the_document():
    urls = manifest_urls("https://x.test/app/page",
                         '<script src="/_next/static/bid/_buildManifest.js"></script>')
    assert urls == ["https://x.test/_next/static/bid/_buildManifest.js"]


def test_a_manifest_linked_as_a_preload_is_found():
    """Builds link these as `<script src>`, as `<link rel=preload as=script>` and as `rel=prefetch`.
    Collection is unfiltered by `rel` because selection happens on the exact basename."""
    html = '<link rel="preload" as="script" href="/_next/static/b/_buildManifest.js">'
    assert manifest_urls("https://x.test/", html) == ["https://x.test/_next/static/b/_buildManifest.js"]


@pytest.mark.parametrize("ref", [
    "/_next/static/b/_buildManifest.js.map",     # the source map, not the manifest
    "/_next/static/chunks/my_buildManifest.js",  # a chunk whose name merely ends the same way
    "/_next/static/b/buildManifest.js",          # no leading underscore
])
def test_a_lookalike_filename_is_not_a_manifest(ref):
    """Selection is exact basename equality, so nothing nearby can be mistaken for the real file."""
    assert manifest_urls("https://x.test/", f'<script src="{ref}"></script>') == []


def test_manifests_are_returned_in_a_stable_order_without_duplicates():
    html = ('<script src="/_next/static/b/_ssgManifest.js"></script>'
            '<script src="/_next/static/b/_buildManifest.js"></script>'
            '<script src="/_next/static/b/_buildManifest.js"></script>')
    urls = manifest_urls("https://x.test/", html)
    assert [os.path.basename(u) for u in urls] == ["_buildManifest.js", "_ssgManifest.js"]


def test_malformed_markup_does_not_raise():
    assert manifest_urls("https://x.test/", "<script src=<<< unclosed") == []
    assert manifest_urls("https://x.test/", "") == []


# ---- the asset prefix ---------------------------------------------------------------------------

def test_the_asset_base_comes_from_the_manifest_url():
    """Chunk paths in the manifest are relative; this is where they actually resolve."""
    assert asset_base_of([
        "https://frontend-assets.supabase.com/studio/dc9/_next/static/build-X/_buildManifest.js"
    ]) == "https://frontend-assets.supabase.com/studio/dc9/_next/"


def test_no_asset_base_is_claimed_when_the_url_has_no_next_segment():
    assert asset_base_of(["https://x.test/weird/_buildManifest.js"]) == ""
    assert asset_base_of([]) == ""


# ---- evaluating the manifest --------------------------------------------------------------------

def test_routes_are_recovered_with_their_parameter_names_intact(out):
    """The names are the reason this beats every other route source: `{ref}` and `{indexName}` tell
    you what the segment IS, where a filename-derived route can only offer `{param1}`."""
    assert "/api/platform/storage/{ref}/vector-buckets/{id}/indexes/{indexName}" in templates(out)
    assert "/project/{ref}/storage/files" in templates(out)


@pytest.mark.parametrize("declared,expected", [
    ("[...slug]", "/blog/{slug}"),       # catch-all
    ("[[...path]]", "/docs/{path}"),     # optional catch-all
])
def test_catch_all_segments_become_named_parameters(out, declared, expected):
    assert expected in templates(out)


def test_api_routes_are_separated_from_page_routes(out):
    """A Next `/api/*` route is server code the client calls — API surface. A page route is not, and
    merging the two is how a route list stops being useful for reconnaissance."""
    assert {r["template"] for r in out["api_routes"]} == {
        "/api/content/graphql",
        "/api/platform/projects/{ref}",
        "/api/platform/storage/{ref}/vector-buckets/{id}/indexes/{indexName}",
    }
    assert not any(r["template"].startswith("/api/") for r in out["routes"])


def test_framework_internals_are_not_reported_as_routes(out):
    """`/_app` and `/_error` are addressable by the router but are not surface anyone can request."""
    assert not {"/_app", "/_error", "/_document"} & templates(out)


def test_a_route_present_only_in_sortedpages_is_still_reported(out):
    """`sortedPages` and the object's own keys are unioned, so a build that lists a route in one and
    not the other does not lose it."""
    assert "/only-in-sortedpages" in templates(out)


def test_route_names_are_not_counted_as_chunks(out):
    """A real bug, caught by arithmetic: iterating every array in the manifest reported 486 chunks for
    Supabase because `sortedPages` is an array of 295 route names. Only `.js` entries are chunks."""
    assert all(c.endswith(".js") for c in out["chunks"])
    assert not any(c.startswith("/") for c in out["chunks"])
    assert "static/chunks/9aa.js" in out["chunks"]


def test_stylesheets_are_not_counted_as_chunks(out):
    """`/_app` lists `static/css/app.css` alongside its JavaScript."""
    assert not any(c.endswith(".css") for c in out["chunks"])


def test_rewrites_are_recovered_with_their_phase_and_destination(out):
    """A rewrite source is a requestable public path that may appear in no route list, and its
    destination names internal structure the client never links to."""
    by_src = {r["template"]: r for r in out["rewrites"]}
    assert by_src["/studio/dc95335a8db3/_next/{path}"]["destination"] == "/dashboard/_next/:path+"
    assert by_src["/studio/dc95335a8db3/_next/{path}"]["phase"] == "beforeFiles"
    assert by_src["/.well-known/vercel/flags"]["destination"] is None
    assert by_src["/.well-known/vercel/flags"]["phase"] == "afterFiles"


def test_middleware_matchers_are_reported_as_written(out):
    """`/api/:function*` is the matcher as the developer wrote it: it states which paths are
    intercepted, which no route list tells you."""
    assert out["middleware"] == [{"source": "/api/:function*", "has_regexp": True}]


def test_the_prerendered_set_is_read_from_a_javascript_set(out):
    """`_ssgManifest.js` assigns `new Set([...])`, not an array — which is why it is evaluated."""
    assert set(out["ssg"]) == {"/blog/{slug}", "/account/me"}


def test_each_manifest_reports_which_globals_it_defined(out):
    found = {s["file"]: s["found"] for s in out["sources"]}
    assert found["_buildManifest.js"] == ["__BUILD_MANIFEST"]
    assert found["_ssgManifest.js"] == ["__SSG_MANIFEST"]
    assert found["_clientMiddlewareManifest.js"] == ["__MIDDLEWARE_MATCHERS"]
    assert out["errors"] == []


# ---- the sandbox --------------------------------------------------------------------------------

def test_the_manifest_cannot_reach_the_process():
    """These files come off the open internet and are RUN. The context holds `self` and the standard
    JavaScript globals — no `process`, no `require`, no `fs` — so a file reaching for one gets a
    ReferenceError that is reported, and the run continues."""
    out = run("hostile.js")
    assert out["errors"] and "process is not defined" in out["errors"][0]["error"]
    assert out["routes"] == [] and out["api_routes"] == []


def test_one_unreadable_manifest_does_not_lose_the_others():
    """Partial failure is normal — a CDN can serve one of the three as an error page."""
    out = run("hostile.js", "_buildManifest.js")
    assert len(out["errors"]) == 1
    assert "/account/me" in templates(out)


# ---- what the absence of a manifest is allowed to claim -----------------------------------------

@pytest.mark.parametrize("html,is_next,says", [
    ('<script src="/_next/static/chunks/a.js"></script>', True, "App Router"),
    ('<script>self.__next_f.push([1,"x"])</script>', True, "App Router"),
    ("<html><body>a plain site</body></html>", False, "not a Next.js app"),
])
def test_a_missing_manifest_does_not_assert_a_stack_that_is_not_in_evidence(
        monkeypatch, html, is_next, says):
    """`reason` is read by humans, so it has to be as honest as a finding. Saying "expected for an App
    Router build" about tiktok.com — which was never Next — states something not in evidence, which is
    the same mistake as reporting an endpoint nothing in the bundle supports."""
    import jshound.nextmanifest as nm
    monkeypatch.setattr(nm, "http_get",
                        lambda u, timeout=30, with_url=False: (200, html, u) if with_url else (200, html))
    d = nm.read("https://x.test/")
    assert d["available"] is False
    assert d["is_next"] is is_next
    assert says in d["reason"]


def test_an_unreachable_document_is_not_reported_as_a_missing_manifest(monkeypatch):
    import jshound.nextmanifest as nm
    monkeypatch.setattr(nm, "http_get",
                        lambda u, timeout=30, with_url=False: (403, "", u) if with_url else (403, ""))
    d = nm.read("https://x.test/")
    assert d["available"] is False and "unreachable" in d["reason"]
