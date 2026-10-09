"""SvelteKit's client route table, which lives inside the entry MODULE rather than in a manifest file.

The reason this file is mostly about resolution is that the table is reached through a minified export
alias, and the first version resolved it by searching the module for the local name. Minified modules
reuse one-letter names in different scopes, so that found whichever declaration came first, on
shadcn-svelte it walked to a Svelte template fragment and reported a route table that was not one.
Scope-correct resolution is the fix, and a structural check on the result is the backstop.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.sveltekitmanifest import entry_url                        # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sveltekit-entry")
JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "jshound", "js", "sveltekit.js")


def run(name):
    env = dict(os.environ)
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    r = subprocess.run(["node", JS, os.path.join(FIX, name)], capture_output=True, env=env, timeout=60)
    return json.loads(r.stdout or b"{}")


@pytest.fixture(scope="module")
def out():
    return run("app.minified.js")


def templates(out):
    return [r["template"] for r in out["routes"]]


# ---- resolution ---------------------------------------------------------------------------------

def test_the_dictionary_is_resolved_through_scope_not_by_searching_for_the_name(out):
    """The fixture holds a decoy: a `const s = {"not": "a route table"}` inside `helper()`, declared
    BEFORE the real `s` at module scope. A name search finds the decoy; a scope lookup from the export
    finds the export's actual binding."""
    assert out["errors"] == []
    assert "/" in templates(out)
    assert "not" not in str(templates(out))


def test_an_export_reached_through_a_wrapper_call_still_resolves(out):
    """Builds wrap these: `matchers` is handed to a helper as `t({...})`, and reading only direct
    object literals reported zero matchers for an app that has one."""
    assert out["matchers"] == ["documentation"]


def test_a_dictionary_that_resolves_to_a_non_route_object_is_refused():
    """The backstop. If resolution lands somewhere plausible but wrong, the answer is "no route table",
    not a list of invented routes, a fabricated route costs more here than a missed one."""
    out = run("app.wrongobject.js")
    assert out["routes"] == []
    assert out["errors"] and "not a route table" in out["errors"][0]["error"]


# ---- route ids to URL templates -----------------------------------------------------------------

@pytest.mark.parametrize("expected", [
    "/",                            # root
    "/apps",                        # (authed) group contributes no segment
    "/blog",
    "/blog/{slug}",                 # [slug]
    "/docs/{topic}/{path}",         # [...path=documentation] keeps the NAME, drops the matcher
    "/maybe/{opt}",                 # [[opt]] optional
])
def test_route_ids_become_url_templates(out, expected):
    assert expected in templates(out)


def test_the_filesystem_spelling_is_kept_beside_the_template(out):
    """A route group is organisational, and showing it was dropped is more useful than hiding that
    the URL and the source path differ."""
    by_t = {r["template"]: r["id"] for r in out["routes"]}
    assert by_t["/apps"] == "/(authed)/apps"


def test_a_constrained_segment_reports_its_matcher(out):
    """`[...path=documentation]` says the segment is validated by the app's own `documentation`
    matcher, so it is not free-form."""
    r = next(r for r in out["routes"] if r["template"] == "/docs/{topic}/{path}")
    assert r["matchers"] == [{"param": "path", "matcher": "documentation"}]


def test_node_indices_are_flattened_and_unsigned(out):
    """Real dictionary values are `[5]`, `[-12]` and `[-7, [2]]`, nested and signed. The sign and
    nesting are not a documented contract, so the indices are read and nothing is concluded from how
    they were written."""
    by_t = {r["template"]: r["nodes"] for r in out["routes"]}
    assert by_t["/apps"] == [2, 7]
    assert by_t["/blog"] == [12]
    assert by_t["/docs/{topic}/{path}"] == [2, 3, 14]


def test_server_loads_is_reported_raw_without_a_per_route_claim(out):
    """Mapping node indices back to routes needs the sign/nesting encoding, so no route is labelled
    as running a server load."""
    assert out["server_load_nodes"] == [0, 2, 3, 4]
    assert all("server_load" not in r for r in out["routes"])


# ---- finding the entry module -------------------------------------------------------------------

def test_the_entry_is_found_as_a_tag_attribute():
    html = '<link href="./_app/immutable/entry/app.Dy3uvAgJ.js" rel="modulepreload">'
    u, base = entry_url("https://svelte.dev/", html)
    assert u == "https://svelte.dev/_app/immutable/entry/app.Dy3uvAgJ.js"
    assert base == ""


def test_the_entry_is_found_inside_an_inline_bootstrap_import():
    """SvelteKit's bootstrap imports the entry from an inline script, so it is not a `src` anywhere.
    Reading attributes alone reported four real SvelteKit apps as "not a SvelteKit app"."""
    html = ('<script>Promise.all([import("./_app/immutable/entry/start.NSieiQfr.js"),'
            'import("./_app/immutable/entry/app.CY3L_9AJ.js")]).then(x => x);</script>')
    u, _ = entry_url("https://realworld.svelte.dev/", html)
    assert u == "https://realworld.svelte.dev/_app/immutable/entry/app.CY3L_9AJ.js"


def test_a_base_path_is_recovered_so_routes_are_absolute():
    """`paths.base` prefixes every route, and the entry's own URL is where it can be read from."""
    html = '<link href="/docs/app/_app/immutable/entry/app.X.js" rel="modulepreload">'
    u, base = entry_url("https://x.test/docs/app/", html)
    assert base == "/docs/app"


def test_a_site_with_no_entry_module_is_not_claimed_as_sveltekit():
    """threlte.xyz and skeleton.dev are Astro; geoffrich.net is a plain static site."""
    assert entry_url("https://x.test/", '<script src="/_astro/index.js"></script>') == ("", "")
    assert entry_url("https://x.test/", "") == ("", "")


def test_a_chunk_that_is_not_the_entry_is_not_mistaken_for_it():
    html = ('<link href="/_app/immutable/chunks/app.B1h.js" rel="modulepreload">'
            '<link href="/_app/immutable/entry/start.X.js" rel="modulepreload">')
    assert entry_url("https://x.test/", html) == ("", "")
