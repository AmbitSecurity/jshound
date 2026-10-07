"""Remix and React Router v7 route manifests.

`__remixManifest` was already a fingerprint marker here — proof the app is Remix — while nothing read
anything out of it. The part that needed care is that the two versions use DIFFERENT data protocols,
and guessing wrong produces a 200 that carries nothing.
"""
import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "remix-manifest")
JS = os.path.join(ROOT, "jshound", "js", "remixmanifest.js")


def run(name):
    env = dict(os.environ)
    env.setdefault("NODE_PATH", os.path.join(ROOT, "node_modules"))
    r = subprocess.run(["node", JS, os.path.join(FIX, name)],
                       capture_output=True, env=env, timeout=60)
    out = json.loads(r.stdout or b"{}")
    assert out.get("routes"), f"{name}: no routes, so every assertion below would be vacuous"
    return out


@pytest.fixture(scope="module")
def v2():
    return run("remix-v2-inline.js")


@pytest.fixture(scope="module")
def v7():
    return run("react-router-v7-linked.js")


# ---- parsing the two shapes --------------------------------------------------------------------

def test_an_inlined_manifest_beside_esm_imports_is_read(v2):
    """The fixture carries `import * as route0 from "..."` above the assignment, as Remix ships it.
    Running the script in a vm throws `Cannot use import statement outside a module` before reaching
    the assignment — the first version of this recovered nothing for that reason, so only the
    assigned expression is evaluated."""
    assert v2["flavour"] == "remix"
    assert v2["version"] == "9c45bfe8"


def test_the_renamed_v7_global_is_read(v7):
    """React Router v7 renamed it to `__reactRouterManifest` and serves it as a linked file."""
    assert v7["flavour"] == "react-router-v7"
    assert v7["version"] == "98aa57ec"


# ---- the route tree ----------------------------------------------------------------------------

def test_route_paths_are_assembled_through_their_parents(v2):
    """A Remix route `path` is a SEGMENT, not a URL: `account/login` is only meaningful nested under
    `root`, which contributes nothing because its own path is empty."""
    by_id = {r["id"]: r["template"] for r in v2["routes"]}
    assert by_id["root"] == "/"
    assert by_id["routes/account.login"] == "/account/login"


def test_an_optional_segment_becomes_a_named_slot(v2):
    """`:locale?` is a parameter, and the name is worth keeping."""
    by_id = {r["id"]: r["template"] for r in v2["routes"]}
    assert by_id["routes/($locale)._index"] == "/{locale}"


def test_a_splat_route_is_templated(v7):
    by_id = {r["id"]: r["template"] for r in v7["routes"]}
    assert by_id["pages/doc"] == "/*"


def test_a_submission_target_is_flagged(v7):
    """`hasAction` says the path accepts a non-GET. reactrouter.com's real one is /color-scheme."""
    acts = [r["template"] for r in v7["routes"] if r["has_action"]]
    assert acts == ["/color-scheme"]


# ---- the data protocol, which differs ----------------------------------------------------------

def test_v2_uses_a_query_carrying_the_route_id(v2):
    """Verified on hydrogen.shop: `?_data=root` answers 200 with `text/remix-deferred`, and an
    INVENTED id answers 403 — which is what makes the id-from-the-manifest approach safe. A route
    list alone would not do, because the id and the URL path are different strings."""
    got = {(e["template"], e["query"]) for e in v2["data_endpoints"]}
    assert ("/", "_data=root") in got
    assert ("/{locale}", "_data=routes/($locale)._index") in got


def test_v7_uses_a_path_suffix_and_not_the_query(v7):
    """Verified on reactrouter.com: `/home.data` answers 200 with 33,582 bytes and
    `/invented-route.data` answers 404. On v7, `?_data=root` answers 200 with text/html — the
    ordinary page — so emitting the v2 form here would look like a hit and carry nothing."""
    got = {e["template"] for e in v7["data_endpoints"]}
    assert "/home.data" in got
    assert "/.data" in got
    assert not any(e.get("query") for e in v7["data_endpoints"])


def test_only_routes_with_a_loader_get_a_data_endpoint(v2):
    """`routes/account.login` has an action and no loader, so it has no data endpoint."""
    assert not any("account/login" in e["template"] for e in v2["data_endpoints"])


def test_pathless_layout_routes_do_not_duplicate_a_data_url(v7):
    """`root` and any pathless layout both resolve to `/`. Routes are kept per id because the id is
    the provenance, but a data endpoint is a URL — reactrouter.com emitted `/.data` three times
    before these were deduplicated."""
    urls = [e["template"] for e in v7["data_endpoints"]]
    assert len(urls) == len(set(urls)), urls


# ---- chunks ------------------------------------------------------------------------------------

def test_every_routes_chunks_are_collected(v2):
    """`module` plus `imports` per route, and the entry's, give an authoritative chunk list."""
    assert "https://cdn.example.com/assets/root-CSnehLZw.js" in v2["chunks"]
    assert "https://cdn.example.com/assets/Cart-CVozpZzD.js" in v2["chunks"]
    assert "https://cdn.example.com/assets/entry.client-C8PL9TPQ.js" in v2["chunks"]
