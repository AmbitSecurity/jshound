"""The numbers in bench/COMPARISON.md are a public claim about this repo's own fixture. If the
extractor changes and the document does not, the comparison becomes a lie told confidently. These
assert the jshound column so that can't happen quietly."""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEMO = os.path.join(ROOT, "examples", "demo-app")


def _records():
    r = _lib.analyze(DEMO, "--host", "demo-app.test")
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def test_fixture_yields_the_six_documented_endpoints():
    eps = [r for r in _records() if (r.get("_kind") or "endpoint") == "endpoint"]
    assert len(eps) == 6, [r.get("template") for r in eps]
    assert sum(1 for r in eps if r.get("method") and r.get("method") != "UNKNOWN") == 6
    assert sum(1 for r in eps
               if r.get("body_keys") or isinstance(r.get("body_schema"), dict)) == 3


def test_cross_module_baseurl_endpoints_are_recovered():
    """The three `${this.baseUrl}/v1/...` call sites are the whole point of the fixture, `baseUrl`
    is assigned in a different module from the call."""
    tmpls = {r.get("template") for r in _records()}
    for want in ("{baseUrl}/v1/subscribe", "{baseUrl}/v1/media"):
        assert want in tmpls, sorted(t for t in tmpls if t and "v1" in t)


def test_three_lazy_chunk_urls_are_built():
    """None of these strings exists in the bundle; the runtime builds them from two id maps."""
    import jshound
    res = jshound.chunk_map(open(os.path.join(DEMO, "app.4f2a9c1e.js")).read())
    assert res["stats"]["idsResolved"] == 3 and res["stats"]["idsSeen"] == 3
    assert sorted(res["chunks"]) == sorted([
        "static/chunks/settings.a1b2c3d4.js",
        "static/chunks/billing.e5f6a7b8.js",
        "static/chunks/admin.c9d0e1f2.js",
    ])
    src = open(os.path.join(DEMO, "app.4f2a9c1e.js")).read()
    for c in res["chunks"]:
        assert c not in src, f"{c} is a literal in the bundle, the fixture no longer proves anything"


def test_the_readmes_offline_block_is_the_real_output(tmp_path):
    """The README's offline example is pasted output, so it rots silently. This runs the command it
    shows and checks the table it prints is in the README, line for line."""
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "examples/demo-app",
                        "-o", str(tmp_path / "out"), "-q"],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]
    table = r.stdout.rstrip("\n")
    assert table, "printed nothing"
    assert "$ jshound examples/demo-app\n" + table + "\n```" in readme, \
        f"README's offline block is stale:\n{table}"


def test_the_examples_page_shows_real_output(tmp_path):
    """examples/README.md pastes three outputs. Each is regenerated here and must be on the page."""
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = open(os.path.join(root, "examples", "README.md"), encoding="utf-8").read()
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "examples/demo-app",
                        "-o", str(tmp_path / "out"), "-q"], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]
    table = r.stdout.rstrip("\n")
    assert "$ jshound examples/demo-app\n" + table + "\n```" in page, f"table is stale:\n{table}"
    urls = (tmp_path / "out" / "urls.txt").read_text().rstrip("\n")
    assert "$ cat .jshound/demo-app/urls.txt\n" + urls + "\n```" in page, f"urls.txt is stale:\n{urls}"
    import jshound
    chunks = "\n".join(jshound.chunk_urls(open(os.path.join(root, "examples", "demo-app", "app.4f2a9c1e.js")).read()))
    assert chunks + "\n```" in page, f"chunk URLs are stale:\n{chunks}"
