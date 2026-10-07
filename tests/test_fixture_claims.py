"""The numbers in bench/COMPARISON.md are a public claim about this repo's own fixture. If the
extractor changes and the document does not, the comparison becomes a lie told confidently. These
assert the jshound column so that can't happen quietly."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEMO = os.path.join(ROOT, "examples", "demo-app")


def _records():
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", DEMO,
                        "--host", "demo-app.test"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def test_fixture_yields_the_six_documented_endpoints():
    eps = [r for r in _records() if (r.get("_kind") or "endpoint") == "endpoint"]
    assert len(eps) == 6, [r.get("template") for r in eps]
    assert sum(1 for r in eps if r.get("method") and r.get("method") != "UNKNOWN") == 6
    assert sum(1 for r in eps
               if r.get("body_keys") or isinstance(r.get("body_schema"), dict)) == 3


def test_cross_module_baseurl_endpoints_are_recovered():
    """The three `${this.baseUrl}/v1/...` call sites are the whole point of the fixture — `baseUrl`
    is assigned in a different module from the call."""
    tmpls = {r.get("template") for r in _records()}
    for want in ("{baseUrl}/v1/subscribe", "{baseUrl}/v1/media"):
        assert want in tmpls, sorted(t for t in tmpls if t and "v1" in t)


def test_three_lazy_chunk_urls_are_built():
    """None of these strings exists in the bundle; the runtime builds them from two id maps."""
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "chunks",
                        os.path.join(DEMO, "app.4f2a9c1e.js")],
                       capture_output=True, text=True, cwd=ROOT)
    res = json.loads(r.stdout)
    assert res["stats"]["idsResolved"] == 3 and res["stats"]["idsSeen"] == 3
    assert sorted(res["chunks"]) == sorted([
        "static/chunks/settings.a1b2c3d4.js",
        "static/chunks/billing.e5f6a7b8.js",
        "static/chunks/admin.c9d0e1f2.js",
    ])
    src = open(os.path.join(DEMO, "app.4f2a9c1e.js")).read()
    for c in res["chunks"]:
        assert c not in src, f"{c} is a literal in the bundle — the fixture no longer proves anything"


def test_the_readmes_opening_blocks_are_the_real_output():
    """The first thing anyone sees is three console blocks. They are pasted output, so they rot
    silently — this pins them to the commands that produce them."""
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()

    def run(*args):
        r = subprocess.run([sys.executable, "-m", "jshound.cli", *args],
                           capture_output=True, text=True, cwd=root)
        assert r.returncode == 0, r.stderr[-800:]
        return r.stdout.rstrip("\n")

    chunks = run("chunks", "examples/demo-app/app.4f2a9c1e.js", "--quiet")
    assert chunks in readme, f"README's chunks block is stale:\n{chunks}"

    table = run("analyze", "examples/demo-app", "--host", "demo-app.test", "-f", "table")
    assert table in readme, f"README's table block is stale:\n{table}"

    # ...and the claim the chunks block is making, which is the whole hook
    src = open(os.path.join(root, "examples", "demo-app", "app.4f2a9c1e.js"), encoding="utf-8").read()
    for built in chunks.splitlines():
        assert built not in src, f"{built} IS in the bundle — the README's premise is wrong"
