"""Module specifiers, by parsing rather than by pattern.

Every case here is a way the regex this replaced was wrong. It read
`(?:\\bfrom|\\bimport)\\s*\\(?\\s*["']([^"']+\\.m?js)["']`, which both invents specifiers out of text that
is not code and misses real ones that do not end in `.js`. On the 30 real bundles fetched from
svelte.dev the two agree exactly — 34 specifiers each — so this is not a behaviour change on ordinary
input, it is the removal of two failure modes on unusual input.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import document_imports, js_imports                 # noqa: E402


def specs(tmp_path, src, name="a.js"):
    p = tmp_path / name
    p.write_text(src)
    return js_imports([str(p)])[str(p)]


@pytest.mark.parametrize("src,spec", [
    ('import x from "./static.js";', "./static.js"),
    ('export { y } from "./reexport.js";', "./reexport.js"),
    ('export * from "./star.js";', "./star.js"),
    ('const p = import("./dynamic.js");', "./dynamic.js"),
    ('import(\n  "./multiline.js"\n);', "./multiline.js"),
])
def test_every_form_that_makes_the_browser_fetch_a_module_is_found(tmp_path, src, spec):
    assert specs(tmp_path, src) == [spec]


@pytest.mark.parametrize("src,spec", [
    ('import "./side-effect.css";', "./side-effect.css"),
    ('import x from "./no-extension";', "./no-extension"),
])
def test_a_specifier_without_a_js_extension_is_still_a_specifier(tmp_path, src, spec):
    """The regex required the specifier to end in `.js`, so it silently skipped these."""
    assert specs(tmp_path, src) == [spec]


@pytest.mark.parametrize("src", [
    '// import "./in-a-comment.js"',
    'const s = "import \'./in-a-string.js\'";',
    'const t = `from "./in-a-template.js"`;',
])
def test_the_word_import_in_text_that_is_not_code_is_not_a_specifier(tmp_path, src):
    """The regex matched all three. A fetcher acting on them requests URLs the app never had."""
    assert specs(tmp_path, src) == []


def test_a_computed_specifier_names_no_file_and_is_not_guessed_at(tmp_path):
    """`import(someVariable)` could be anything; inventing a URL for it is worse than omitting it."""
    assert specs(tmp_path, 'const m = "x"; import(m); import(`./${m}.js`);') == []


def test_a_file_that_does_not_parse_does_not_lose_the_others(tmp_path):
    """One unparseable file in a batch is normal — a CDN can serve an error page with a .js name."""
    good = tmp_path / "good.js"
    good.write_text('import x from "./ok.js";')
    bad = tmp_path / "bad.js"
    bad.write_text("<!DOCTYPE html><html>this is not javascript</html>")
    out = js_imports([str(good), str(bad)])
    assert out[str(good)] == ["./ok.js"]
    assert out[str(bad)] == []


def test_duplicate_specifiers_are_reported_once(tmp_path):
    assert specs(tmp_path, 'import "./a.js"; import("./a.js");') == ["./a.js"]


def test_no_files_means_no_work(tmp_path):
    assert js_imports([]) == {}


# ---- the document's inline bootstrap ------------------------------------------------------------

def test_an_inline_script_import_is_resolved_against_the_document():
    """This is what made four SvelteKit apps detectable: the entry module is imported from an inline
    script and appears nowhere as a `src`."""
    html = ('<script>Promise.all([import("./_app/immutable/entry/start.X.js"),'
            'import("./_app/immutable/entry/app.Y.js")]);</script>')
    assert document_imports("https://x.test/", html) == [
        "https://x.test/_app/immutable/entry/start.X.js",
        "https://x.test/_app/immutable/entry/app.Y.js",
    ]


def test_a_bare_package_specifier_has_no_url_and_is_dropped():
    """`import("svelte")` is a build-time specifier; there is nothing to fetch."""
    html = '<script>import("svelte"); import("@sveltejs/kit");</script>'
    assert document_imports("https://x.test/", html) == []


def test_a_document_with_no_inline_scripts_needs_no_parser():
    assert document_imports("https://x.test/", '<script src="/a.js"></script>') == []
    assert document_imports("https://x.test/", "") == []
