"""What counts as a client's base URL.

A wrong base is the worst thing here: a real-looking route pointed somewhere the app never calls. Both
directions of error have cost real bugs, so both are pinned. The false-negative direction was found by
forcing every precheck in the extractor open and diffing the output.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "axios-base")


def _paths(case):
    d = os.path.join(FIX, case)
    assert os.path.isdir(d), d
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", d],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-1200:]
    recs = json.loads(r.stdout)
    got = {(x.get("method"), x.get("template")) for x in recs if x.get("sink")}
    assert got, f"{case}: no sink records, so the assertion would be vacuous"
    return got


def test_basepath_and_prefixurl_are_base_keys_too():
    """Neither was matched by the original key family, so an origin under either was ignored and these
    calls came back as `/things` and `/publish`."""
    got = _paths("basepath-and-prefixurl")
    assert ("GET", "https://svc.mycorp.io/v1/things") in got
    assert ("POST", "https://edge.mycorp.io/publish") in got


def test_a_relative_base_is_deliberately_not_applied():
    """A known limitation, pinned so the reason is not lost and the behaviour is not changed by
    accident. Accepting a relative base was implemented and measured: it fixed this file and made
    Grafana worse, producing four paths that glue the k8s-style resource base onto the legacy REST API
    and displacing four correct records. The cause is that `applyBase` resolves a client by receiver
    NAME, which collides under minification; an origin value makes a mis-attribution conspicuous,
    a relative one does not. See the fixture for the full account."""
    got = _paths("relative-base-is-not-applied")
    assert ("GET", "/users") in got
    assert not any("/api/v2" in str(t) for _m, t in got)


def test_an_absolute_base_still_works():
    assert ("GET", "https://api.mycorp.io/v2/accounts") in _paths("absolute-base")


def test_a_generic_key_with_a_relative_value_is_not_a_base():
    """`url`/`uri` are in the key family but do not say "base". Treating `{url: "/users"}` as one would
    report `/users/orders`."""
    got = _paths("generic-key-is-not-a-base")
    assert ("GET", "/orders") in got
    assert ("POST", "/invoices") in got
    assert not any("/users" in str(t) for _m, t in got)
    assert not any("/graphql" in str(t) for _m, t in got)


def test_an_origin_under_a_non_base_key_is_not_a_base():
    """The reason the value test exists. amcharts is configured
    `pathToImages: "http://www.amcharts.com/lib/3/images/"`, which no test on the value can tell from
    an API base; treating it as one produced four fabricated endpoints on unrelated clients."""
    got = _paths("origin-under-a-non-base-key")
    assert ("GET", "/environment/list/names") in got
    assert not any("amcharts" in str(t) for _m, t in got)


def test_a_base_key_whose_value_is_not_a_path_or_an_origin_is_not_a_base():
    """`{baseURL: "production"}` is an environment name and `{baseURL: "?v=2"}` is a query string.
    Accepting a relative value under a base key must not mean accepting any string."""
    got = _paths("base-key-with-a-non-path")
    assert ("GET", "/orders") in got
    assert ("GET", "/invoices") in got
    assert not any("production" in str(t) or "?v=2" in str(t) for _m, t in got)
