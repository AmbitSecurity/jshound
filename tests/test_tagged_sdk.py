"""A Stainless-generated SDK, which is the Anthropic, OpenAI and Cloudflare SDKs among others.

claude.ai bundles the Anthropic SDK, and one 1.4 MB file of it produced 136 records with no HTTP
method and 66 paths containing a fabricated `hook_` segment. Three separate causes, each reproduced
here in the smallest shape that shows it.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound   # noqa: E402

SDK = r'''
function Kb(s, ...v) { return s.reduce((a, x, i) => a + x + (v[i] ?? ""), ""); }
class Rb { constructor(c) { this._client = c; } }
class Deployments extends Rb {
  archive(e, t) { return this._client.post(Kb`/v1/deployments/${e}/archive?beta=true`, { body: t }); }
  retrieve(e)   { return this._client.get(Kb`/v1/deployments/${e}?beta=true`); }
  list(e)       { return this._client.getAPIList(Kb`/v1/deployments/${e}/runs?beta=true`, Page, {}); }
}
// The trap. A hook registry elsewhere in the same file assigns a template to a variable that the
// minifier also happened to call `e`. Resolving `${e}` above by NAME borrowed this value.
class Hooks {
  register() { for (const h of this.hooks) { let e = `hook_${this.nextCallbackId++}`; this.map.set(e, h); } }
}
'''


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    d = tmp_path_factory.mktemp("sdk")
    (d / "sdk.js").write_text(SDK)
    return [r for r in jshound.analyze(str(d), host="api.test")
            if (r.get("_kind") or "endpoint") in ("endpoint", "path-literal")]


def by_path(records, fragment):
    return [r for r in records if fragment in str(r.get("template"))]


def test_a_tagged_template_url_keeps_its_method(records):
    """`this._client.post(Kb\\`...\\`)` fell to the resolver's default and rendered `{taggedtemplate}`,
    so the call site was thrown away and only a method-less route constant survived."""
    archive = by_path(records, "/archive")
    assert archive and {r.get("method") for r in archive} == {"POST"}, archive


def test_getapilist_is_a_get(records):
    """Stainless pages every list endpoint through `getAPIList`, which issues a GET with no `.get`
    anywhere in sight. Every one of the 27 such calls in claude.ai's SDK file came out with no verb."""
    runs = by_path(records, "/runs")
    assert runs and {r.get("method") for r in runs} == {"GET"}, runs


def test_a_slot_is_never_filled_from_an_unrelated_variable_of_the_same_name(records):
    """`${e}` is a parameter of `archive`. The `let e = \\`hook_...\\`` belongs to another function.
    Keyed by name, the first was filled from the second and 66 SDK routes came out as
    `/v1/.../hook_{EXPR}`, a path segment that does not exist."""
    assert not [r for r in records if "hook_" in str(r.get("template"))]


def test_no_method_less_duplicate_survives_beside_the_real_endpoint(records):
    """Once the call site resolves, the route constant naming the same path is a duplicate."""
    assert not [r for r in records if r.get("method") in (None, "UNKNOWN")
                and "/v1/deployments" in str(r.get("template"))]
