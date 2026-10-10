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
    those as .js put 46 copies of the same document into a corpus of 102, polluting the analysis,
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
    document has no `<script src>` at all, the entry module is reached only through a dynamic
    `import()` inside an inline script, so a fetcher reading attributes finds nothing to analyse."""
    import jshound.fetch as f
    served = {
        "https://x.test/": (200, DOC_SVELTEKIT),
        "https://x.test/_app/immutable/entry/start.A.js": (200, 'import "./chunk.C.js";'),
        "https://x.test/_app/immutable/entry/app.B.js": (200, "export const dictionary = {};"),
        "https://x.test/_app/immutable/entry/chunk.C.js": (200, "export const q = 1;"),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False, **kw: (
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
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False, **kw: (
        (*served.get(u, (404, "")), u) if with_url else served.get(u, (404, ""))))
    # app.B.js and the inline script survive; start.A.js answered 200 with a document and does not.
    assert f.collect("https://x.test/", str(tmp_path), with_chunks=False) == 2


def test_a_lazy_chunks_own_static_imports_are_followed(tmp_path, monkeypatch):
    """How Vite and Rollup split code: the entry lazily loads a route, and the route statically imports
    a shared module. Imports used to be followed only from the scripts the page itself loaded, so the
    shared module, two hops out, was never fetched and nothing said so. Every file is scanned for both
    now, until a round adds nothing."""
    import jshound.fetch as f
    served = {
        "https://x.test/": (200, '<html><script type="module" src="/assets/app.A1b2c3.js"></script></html>'),
        "https://x.test/assets/app.A1b2c3.js": (200, 'import("./route.B4d5e6.js").then(m => m.go());'),
        "https://x.test/assets/route.B4d5e6.js": (200, 'import { api } from "./shared.C7f8a9.js"; export const go = () => api();'),
        "https://x.test/assets/shared.C7f8a9.js": (200, 'export const api = () => fetch("/v1/ping");'),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False, **kw: (
        (*served.get(u, (404, "")), u) if with_url else served.get(u, (404, ""))))
    monkeypatch.setattr(f, "host_is_private", lambda h: False)
    rep = {}
    f.collect("https://x.test/", str(tmp_path), with_chunks=True, report=rep)
    got = {r["u"] for r in (json.loads(l) for l in open(os.path.join(str(tmp_path), "_origin.jsonl")))}
    assert "https://x.test/assets/route.B4d5e6.js" in got
    assert "https://x.test/assets/shared.C7f8a9.js" in got, "the route's own import was not followed"
    assert rep.get("files_unscanned") == 0


def test_a_file_that_cannot_be_scanned_is_counted(tmp_path, monkeypatch):
    """Whatever an unscannable file declares is missing, and a count is the only way anyone hears."""
    import jshound.fetch as f
    served = {
        "https://x.test/": (200, '<html><script src="/assets/app.A1b2c3.js"></script></html>'),
        "https://x.test/assets/app.A1b2c3.js": (200, "var a = 1;"),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False, **kw: (
        (*served.get(u, (404, "")), u) if with_url else served.get(u, (404, ""))))
    monkeypatch.setattr(f, "host_is_private", lambda h: False)
    monkeypatch.setattr(f, "scan_files", lambda paths: {p: None for p in paths})
    rep = {}
    f.collect("https://x.test/", str(tmp_path), with_chunks=True, report=rep)
    assert rep.get("files_unscanned") == 1


def test_imports_resolve_through_the_pages_import_map(tmp_path, monkeypatch):
    """remix.run's modules import source paths (`./fathom.ts`) that only its import map turns into
    the hashed files it serves. Resolved without the map, all 103 of its imports were 404s."""
    import jshound.fetch as f
    page = ('<html><head><script type="importmap">{"imports": {'
            '"/assets/app/fathom.ts": "/assets/app/fathom.@apt6hu.ts", "lit": "/vendor/lit.@x1y2z3.js"}}'
            '</script><script type="module" src="/assets/app/entry.@eQkcqZ.ts"></script></head></html>')
    served = {
        "https://x.test/": (200, page),
        "https://x.test/assets/app/entry.@eQkcqZ.ts": (200, 'import "./fathom.ts"; import { html } from "lit";'),
        "https://x.test/assets/app/fathom.@apt6hu.ts": (200, 'export const t = () => fetch("/api/track");'),
        "https://x.test/vendor/lit.@x1y2z3.js": (200, "export const html = 1;"),
    }
    monkeypatch.setattr(f, "http_get", lambda u, timeout=30, with_url=False, **kw: (
        (*served.get(u, (404, "")), u) if with_url else served.get(u, (404, ""))))
    monkeypatch.setattr(f, "host_is_private", lambda h: False)
    rep = {}
    f.collect("https://x.test/", str(tmp_path), with_chunks=True, report=rep)
    got = {r["u"] for r in (json.loads(l) for l in open(os.path.join(str(tmp_path), "_origin.jsonl")))}
    assert "https://x.test/assets/app/fathom.@apt6hu.ts" in got
    assert "https://x.test/vendor/lit.@x1y2z3.js" in got, "a bare name the map defines is fetched"
    assert rep.get("chunks_unreachable", 0) == 0
    # and the map itself is data, not a script to analyse
    assert not any("importmap" in open(os.path.join(str(tmp_path), n)).read()
                   for n in os.listdir(str(tmp_path)) if n.startswith("inline_"))


def test_a_redirected_page_is_read_where_it_landed(tmp_path, monkeypatch):
    """demo.connectrpc.com redirects to connectrpc.com/demo/. Its scripts were resolved against the
    host that was asked for, which answered `/_astro/page.js` with its catch-all page, so the app
    read as two files and none of its code."""
    import jshound.fetch as f
    landed = "https://example.com/demo/"
    served = {landed: (200, '<html><script type="module" src="/_astro/page.js"></script></html>'),
              "https://example.com/_astro/page.js": (200, "fetch('/api/x');")}

    def fake(u, timeout=30, with_url=False, **kw):
        if u == "https://demo.example.com/":
            u = landed                                    # where following the redirect ends up
        st, body = served.get(u, (200, "<!doctype html><html>catch-all</html>"))
        return (st, body, u) if with_url else (st, body)

    monkeypatch.setattr(f, "http_get", fake)
    monkeypatch.setattr(f, "host_is_private", lambda h: False)
    assert f.collect("https://demo.example.com/", str(tmp_path), with_chunks=False) == 1
    rows = [json.loads(l) for l in open(os.path.join(str(tmp_path), "_origin.jsonl"))]
    assert "https://example.com/_astro/page.js" in {r["u"] for r in rows}
    assert {r["d"] for r in rows} == {landed}             # and its requests resolve against it

