"""Output from real bundler runs, not hand-written approximations. Every assertion here is a gap
found by building a tiny app with the actual tool and pointing this at the result."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jshound   # noqa: E402
import _lib  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


def _chunks(path):
    return jshound.chunk_map(open(path, encoding="utf-8", errors="replace").read())


def _analyze(d):
    r = _lib.analyze(d)
    assert r.returncode == 0, r.stderr[-1200:]
    return [x for x in json.loads(r.stdout) if (x.get("_kind") or "endpoint") == "endpoint"]


def test_parcel_is_detected():
    """Parcel suffixes its runtime global per build: parcelRequire5707, not parcelRequire."""
    from jshound.fingerprint import scan
    assert scan(os.path.join(FIX, "parcel-build"))["bundler"] == "parcel"


def test_parcel_object_chunk_table():
    """Object.assign(o.i ??= {}, {jrWAC: "feature.9e64f881.js"}) — an object, where the Vite pass
    only read arrays."""
    res = _chunks(os.path.join(FIX, "parcel-build", "app.js"))
    assert "feature.9e64f881.js" in res["chunks"], res["chunks"]


def test_parcel_endpoints_survive_the_build():
    eps = {(e.get("method"), e.get("template")) for e in _analyze(os.path.join(FIX, "parcel-build"))}
    assert ("POST", "/api/v1/users") in eps, eps


def test_module_federation_ids_come_from_the_call_sites():
    """Its builder carries no id map — `n.u = e => e + ".5eb33312.js"` — so harvesting object
    literals inside the builder found nothing and it was never run. Every lazy import still asks
    for its chunk through __webpack_require__.e(480)."""
    res = _chunks(os.path.join(FIX, "webpack5-module-federation", "main.js"))
    assert res["chunks"] == ["480.5eb33312.js"], res
    assert res["stats"]["idsResolved"] == res["stats"]["idsSeen"] == 1


def test_module_federation_endpoints():
    eps = {(e.get("method"), e.get("template"))
           for e in _analyze(os.path.join(FIX, "webpack5-module-federation"))}
    assert ("POST", "/api/v1/orders") in eps, eps


def test_metro_is_detected():
    """Metro's registry is `__d(f, id, deps)` and `__r(id)` — one and two characters, too generic
    to match. Every bundle opens by assigning __METRO_GLOBAL_PREFIX__ and __BUNDLE_START_TIME__."""
    from jshound.fingerprint import scan
    assert scan(os.path.join(FIX, "metro-rn"))["bundler"] == "metro"


def test_metro_contracts_extract():
    """fetch, new URL(path, base) and XMLHttpRequest.open, all from real Metro output."""
    eps = {(e.get("method"), e.get("template")) for e in _analyze(os.path.join(FIX, "metro-rn"))}
    assert ("POST", "/api/v1/items") in eps, eps
    assert ("GET", "https://api.rn.test/v1/items") in eps, eps
    assert any(m == "DELETE" for m, _t in eps), eps


def test_esm_bundlers_extract_and_split():
    """esbuild --splitting and rollup -f es. Nothing broke on either: `esm (no bundler runtime
    marker)` is the correct answer, not a detection failure — pure ESM carries no runtime global."""
    from jshound.fingerprint import scan
    for name in ("esbuild-esm", "rollup-esm"):
        d = os.path.join(FIX, name)
        assert scan(d)["bundler"] == "esm (no bundler runtime marker)", name
        eps = {(e.get("method"), e.get("template")) for e in _analyze(d)}
        assert ("POST", "/api/v1/beta") in eps, (name, eps)
        assert ("GET", "https://api.eb.test/v1/alpha") in eps, (name, eps)
        chunks = _chunks(os.path.join(d, "app.js"))["chunks"]
        assert any("lazy-" in c for c in chunks), (name, chunks)
