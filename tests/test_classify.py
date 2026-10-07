import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"))
from classify import bucket, tally   # noqa: E402


def test_minifier_debris_is_not_a_path():
    for p in ("/VwdtK", "/+U0Y/", "/hrkZs", "/nv0ml"):
        assert bucket(p) == "fragment", p


def test_real_surface_is_classified():
    assert bucket("/api/v1/users") == "api"
    assert bucket("/graphql") == "api"
    assert bucket("/user-guide/intro") == "content"
    assert bucket("/_next/static/x.js") == "asset"


def test_tally_is_total_preserving():
    paths = ["/api/x", "/user-guide/y", "/VwdtK", "/_next/a.js"]
    assert sum(tally(paths).values()) == len(paths)
