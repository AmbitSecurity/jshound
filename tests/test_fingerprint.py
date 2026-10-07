"""Stack fingerprinting. Every assertion here is a false positive this tool actually produced while
it was matching text instead of parsing — the whole point of the file is that a name appearing in a
bundle is not evidence the thing is running."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fingerprint import document_signals, js_signals   # noqa: E402


def _js(tmp_path, src, name="a.js"):
    p = tmp_path / name
    p.write_text(src)
    return js_signals([str(p)])[0]


def test_a_bundler_name_inside_a_string_is_not_a_detection(tmp_path):
    """Grafana was reported as a Next app because `__NEXT_DATA__` appears once inside a string, and
    as an Angular app because `ng-version` appears in chunks it vendors for plugin compatibility."""
    sig = _js(tmp_path, 'console.log("__NEXT_DATA__ and __webpack_require__ and __NUXT__");')
    assert "next" not in sig and "webpack" not in sig and "nuxt" not in sig


def test_a_real_identifier_is_a_detection(tmp_path):
    assert "webpack" in _js(tmp_path, "var x = __webpack_require__(42);")


def test_a_global_read_through_a_member_is_a_detection(tmp_path):
    """Turbopack's chunk list is `(globalThis.TURBOPACK||(globalThis.TURBOPACK=[])).push([...])` —
    a member property, not a `__turbopack_*` identifier. Vercel's site ships 144 of these and was
    detected as no bundler at all."""
    assert "turbopack" in _js(tmp_path, "(globalThis.TURBOPACK||(globalThis.TURBOPACK=[])).push([1]);")
    assert "nuxt" in _js(tmp_path, "if (window.__NUXT__) start();")


def test_a_property_key_is_not_a_detection(tmp_path):
    """`{__NEXT_DATA__: 1}` says nothing about what is running."""
    assert "next" not in _js(tmp_path, "var o = {__NEXT_DATA__: 1, parcelRequire: 2};")


def test_prototype_names_are_not_bundlers(tmp_path):
    """`IDENT["toString"]` on a plain object literal returns the inherited method, which is truthy —
    the first version of this reported nine bundlers named things like
    'function valueOf() { [native code] }'."""
    sig = _js(tmp_path, "var a = {}.toString; var b = {}.constructor; var c = {}.hasOwnProperty;")
    for bad in list(sig):
        assert "native code" not in bad and bad in (
            "webpack", "rspack", "turbopack", "vite", "rollup", "parcel", "esbuild",
            "next", "nuxt", "remix", "sveltekit", "solid", "esm-module", "esm-dynamic-import"), bad


def test_chunk_prefix_names_are_matched(tmp_path):
    assert "webpack" in _js(tmp_path, "(self.webpackChunkmyapp=self.webpackChunkmyapp||[]).push([[1],{}]);")


def test_document_signals_are_structural(tmp_path):
    """A framework announces itself with an element id, a data- attribute, or the path a preload
    points at — not with its name appearing somewhere in the text."""
    doc = tmp_path / "i.html"
    doc.write_text('<html><head><link rel="modulepreload" href="/_app/immutable/x.js">'
                   '</head><body><div data-astro-cid-a1>x</div>'
                   '<script id="__NEXT_DATA__">{}</script></body></html>')
    found = document_signals(str(doc))
    assert {"sveltekit", "astro", "next"} <= found


def test_a_name_in_document_text_is_not_a_document_signal(tmp_path):
    doc = tmp_path / "j.html"
    doc.write_text("<html><body><p>We migrated off __NEXT_DATA__ and ng-version last year.</p></body></html>")
    assert document_signals(str(doc)) == set()


def test_bundler_signals_are_reported_per_host(tmp_path):
    """A page loads other people's code and you cannot tell by hostname: tldraw is a Vite app that
    bundles Clerk's auth SDK (webpack), served from clerk.tldraw.com — its own subdomain. Excluding
    'third-party' hosts cannot work, and trying it broke Grafana, whose own assets are on
    grafana-assets.grafana.net while its document is play.grafana.org. So nothing is excluded and
    the host is reported beside the signal."""
    import json as _json

    from jshound.fingerprint import scan
    (tmp_path / "a.js").write_text("export const x = 1; import('./y.js');")
    (tmp_path / "b.js").write_text("(self.webpackChunkvendor=self.webpackChunkvendor||[]).push([[1],{}]);")
    (tmp_path / "_origin.jsonl").write_text(
        _json.dumps({"f": "a.js", "u": "https://app.test/a.js", "d": "https://app.test/"}) + "\n" +
        _json.dumps({"f": "b.js", "u": "https://auth.app.test/b.js", "d": "https://app.test/"}) + "\n")
    r = scan(str(tmp_path))
    hosts = r["bundler_hosts"]
    assert "webpack" in hosts
    assert dict(hosts["webpack"])["auth.app.test"] == 1
    assert r["document_host"] == "app.test"


def test_ember_announces_itself_in_a_config_meta(tmp_path):
    """Ember names its own app in the meta: <meta name="discourse/config/environment">,
    <meta name="ember-website/config/environment">. The app name varies, the suffix does not."""
    doc = tmp_path / "i.html"
    doc.write_text('<html><head><meta name="discourse/config/environment" content="%7B%7D">'
                   "</head><body></body></html>")
    assert "ember" in document_signals(str(doc))


def test_angular_compiler_output_is_a_member_name(tmp_path):
    """A client-rendered Angular app writes nothing into the served document — ng-version is added
    during hydration — so the document rule alone can never name it. Its compiler output is static
    class properties: material.angular.dev carries ɵfac 219 times."""
    assert "angular" in _js(tmp_path, "class A{}\nA.ɵfac = 1;\nA.ɵcmp = 2;")


def test_a_framework_is_named_from_js_only_when_it_is_everywhere(tmp_path):
    """Grafana bundles AngularJS for its plugin system — 10 files of 163, 6% — and a count-based
    rule called it an Angular app. A fifth of the corpus is the application; 6% is something it
    bundles."""
    from jshound.fingerprint import scan

    sparse = tmp_path / "sparse"
    sparse.mkdir()
    for i in range(20):
        (sparse / f"f{i}.js").write_text("export const x = 1;")
    (sparse / "vendor.js").write_text("class A{}\nA.ɵfac = 1;")      # 1 of 21 ≈ 5%
    assert scan(str(sparse))["framework"] is None

    dense = tmp_path / "dense"
    dense.mkdir()
    for i in range(5):
        (dense / f"f{i}.js").write_text("class A{}\nA.ɵfac = %d;" % i)
    for i in range(5):
        (dense / f"g{i}.js").write_text("export const x = 1;")            # 5 of 10 = 50%
    r = scan(str(dense))
    assert r["framework"] == "angular" and "javascript" in r["framework_basis"]


def _doc(tmp_path, html, name="index.html"):
    p = tmp_path / name
    p.write_text(html)
    return document_signals(str(p))


def test_fresh_is_named_from_its_island_comments(tmp_path):
    """Fresh (Deno) writes nothing a tag attribute can carry. It delimits each hydration island with
    a COMMENT pair — `<!--frsh:island:HelloBar:1:-->` — and appends a build cache key to every asset
    it serves. A comment is part of the document's structure, so it is the same kind of evidence as
    an element id. Measured on deno.com."""
    assert "fresh" in _doc(
        tmp_path,
        '<html><body><!--frsh:island:HelloBar:1:--><div>hi</div><!--/frsh:island--></body></html>')
    assert "fresh" in _doc(
        tmp_path,
        '<link rel="icon" href="/favicon.ico?__frsh_c=7e8f41a6028a1acaa12f4ce9aeb52e1b44dc2">',
        name="b.html")


def test_an_unrelated_comment_is_not_fresh(tmp_path):
    """The comment has to be Fresh's island marker, not any comment."""
    assert "fresh" not in _doc(tmp_path, "<html><!-- build: 2026-10-06 --><body>x</body></html>",
                               name="c.html")


def test_vue_is_detected_but_not_crowned_from_one_file(tmp_path):
    """`__VUE__` and `__vue_app__` are the devtools and DOM-element wire contracts, so they are
    definitive — and they live in the runtime chunk alone. On antdv.com that is 1 file of 137, under
    the 20% a JS-only signal needs, because one file cannot tell "is Vue" from "bundles Vue". Being
    listed rather than named is the honest answer."""
    assert "vue" in _js(tmp_path, "window.__VUE__ = { version: 3 };")
    assert "vue" in _js(tmp_path, "el.__vue_app__ = app;", name="b.js")


def test_vues_sfc_scope_id_is_a_string_and_not_a_detection(tmp_path):
    """The signal that looked like it would carry Vue and does not. The compiled form is
    `_export_sfc(x, [["__scopeId", "data-v-e4e38630"]])` — the name is a string literal, and this
    file does not detect things named inside strings."""
    assert "vue" not in _js(tmp_path, 'const a = w(t, [["__scopeId", "data-v-e4e38630"]]);')


def test_gatsby_is_named_from_its_mount_element_id(tmp_path):
    """`<div id="___gatsby">` appears exactly once in a Gatsby document. gatsbyjs.com was otherwise
    reported as webpack with no framework at all."""
    assert "gatsby" in _doc(tmp_path, '<html><body><div id="___gatsby"><div/></div></body></html>',
                            name="g.html")


def test_a_vendored_library_does_not_become_the_framework(tmp_path):
    """laravel.com ships a chunk called `inertia-D-BlvnUF.js` and marketing copy naming Inertia, and
    is reported as vite with no framework — correctly. The name of a bundled file is not the stack."""
    assert _doc(tmp_path, '<script type="module" src="/build/assets/inertia-D-BlvnUF.js"></script>',
                name="v.html") == set()
