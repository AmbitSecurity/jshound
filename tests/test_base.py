"""What counts as a client's base URL.

A wrong base is the worst thing here: a real-looking route pointed somewhere the app never calls. Both
directions of error have cost real bugs, so both are pinned. The false-negative direction was found by
forcing every precheck in the extractor open and diffing the output.
"""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "axios-base")


def _paths(case):
    d = os.path.join(FIX, case)
    assert os.path.isdir(d), d
    r = _lib.analyze(d)
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


def test_a_relative_base_is_applied_when_the_client_is_found_by_binding():
    """This was pinned as a deliberate limitation: a relative base was refused because a client was
    found by its receiver's NAME, which collides under minification, and on Grafana that glued a k8s
    resource base onto the legacy REST API. A client is found by binding now, which cannot collide."""
    got = _paths("relative-base-is-not-applied")
    assert ("GET", "/api/v2/users") in got


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
