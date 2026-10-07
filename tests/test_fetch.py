"""Fetcher correctness. The SPA catch-all is the one that matters: nearly every single-page app
answers 200 with its index.html for a URL that does not exist, so a status check alone cannot tell
a chunk from a 404."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import is_javascript   # noqa: E402


def test_an_spa_catch_all_is_not_javascript():
    """Asking excalidraw.com for a chunk that does not exist returns index.html with a 200. Saving
    those as .js put 46 copies of the same document into a corpus of 102 — polluting the analysis,
    inflating the fetched count, and making a 404 look like a hit."""
    assert not is_javascript('<!doctype html><html lang="en"><head><title>x</title>')
    assert not is_javascript("\n  <html>")
    assert not is_javascript('<?xml version="1.0"?>')


def test_content_type_is_believed_when_present():
    assert not is_javascript("export const a = 1", "text/html; charset=utf-8")


def test_real_javascript_passes():
    for src in ("export const a = 1;",
                '(()=>{var r={};r.u=e=>"x"+e+".js"})()',
                "/*! license */\nimport x from './y.js';",
                "\n\n  var a = 1 < 2;"):
        assert is_javascript(src), src


def test_a_comparison_operator_is_not_a_tag():
    """`a < b` at the start of a minified file must not read as markup."""
    assert is_javascript("a<b&&c()")


# ---- what the fetcher can SEE ------------------------------------------------------------------

DOC_SVELTEKIT = """<html><head></head><body>
<script>
  { Promise.all([import("./_app/immutable/entry/start.A.js"),
                 import("./_app/immutable/entry/app.B.js")]).then(([s, a]) => s.start(a)); }
</script>
</body></html>"""


def test_a_document_whose_only_script_is_an_inline_bootstrap_still_yields_javascript(
        tmp_path, monkeypatch):
    """Measured on realworld.svelte.dev: 1 script and 2 records before, 22 and 46 after. A SvelteKit
    document has no `<script src>` at all — the entry module is reached only through a dynamic
    `import()` inside an inline script, so a fetcher reading attributes finds nothing to analyse."""
    import jshound.fetch as f
    served = {
        "https://x.test/": (200, DOC_SVELTEKIT),
        "https://x.test/_app/immutable/entry/start.A.js": (200, 'import "./chunk.C.js";'),
        "https://x.test/_app/immutable/entry/app.B.js": (200, "export const dictionary = {};"),
        "https://x.test/_app/immutable/entry/chunk.C.js": (200, "export const q = 1;"),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False: (
        (*served.get(u, (404, "<!doctype html><html>")), u) if with_url
        else served.get(u, (404, "<!doctype html><html>"))))

    # Both entry modules, the module one of them imports, and the inline script itself.
    saved = f.collect("https://x.test/", str(tmp_path), with_chunks=True)
    got = {r["u"] for r in (json.loads(l) for l in
                            open(os.path.join(str(tmp_path), "_origin.jsonl")))}
    assert "https://x.test/_app/immutable/entry/app.B.js" in got
    assert "https://x.test/_app/immutable/entry/start.A.js" in got
    # and the module reached only THROUGH the bootstrap entry, one hop further out
    assert "https://x.test/_app/immutable/entry/chunk.C.js" in got
    assert saved == 4


def test_an_spa_catch_all_reached_through_a_bootstrap_import_is_still_rejected(tmp_path, monkeypatch):
    """The 200-with-index.html trap does not stop applying because the URL came from an import."""
    import jshound.fetch as f
    served = {
        "https://x.test/": (200, DOC_SVELTEKIT),
        "https://x.test/_app/immutable/entry/start.A.js": (200, "<!doctype html><html>nope</html>"),
        "https://x.test/_app/immutable/entry/app.B.js": (200, "export const dictionary = {};"),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False: (
        (*served.get(u, (404, "")), u) if with_url else served.get(u, (404, ""))))
    # app.B.js and the inline script survive; start.A.js answered 200 with a document and does not.
    assert f.collect("https://x.test/", str(tmp_path), with_chunks=False) == 2
