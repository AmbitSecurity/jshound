"""Vite/Rollup chunk discovery. Webpack hides its chunk URLs behind a function this tool evaluates;
Vite writes them down, so they are literals, definite, not speculative. Both shapes are covered,
along with the array that must NOT be mistaken for a dependency table."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound   # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "vite-literal-chunks", "app.js")


def test_vite_chunks_come_back_structured_not_speculative():
    res = jshound.chunk_map(open(FIX).read())
    chunks = set(res["chunks"])
    assert {"assets/src-97epdJdY.js", "assets/react-dom-BzP9Lo1b.js",
            "assets/preload-helper-Czpn1I53.js"} <= chunks      # the mapDeps table
    assert {"./BoardHistoryLog-D7tgZ4er.js", "./settings-Bq12Xy9z.js"} <= chunks   # import()
    assert not res["speculative"], "literals are definite; nothing should be guessed here"


def test_an_ordinary_string_array_is_not_a_dependency_table():
    chunks = set(jshound.chunk_map(open(FIX).read())["chunks"])
    assert "alpha" not in chunks and "beta" not in chunks


def test_unparseable_input_still_returns_the_documented_shape():
    """This wrote a bare `[]`, the format from before stats existed, so every consumer doing
    .get("chunks") crashed on the first unparseable file in a corpus."""
    res = jshound.chunk_map("this is (((not javascript")
    assert isinstance(res, dict)
    assert res["chunks"] == [] and res["parsed"] is False
    assert res["stats"]["idsSeen"] == 0


def test_webpack_builder_still_works():
    """The Vite pass must not disturb the runtime-builder path it sits beside."""
    demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "demo-app", "app.4f2a9c1e.js")
    res = jshound.chunk_map(open(demo).read())
    assert res["stats"]["idsResolved"] == 3 and len(res["chunks"]) == 3


def test_webpack4_named_builder_function():
    """webpack 4 emits `function jsonpScriptSrc(e){return p+({…}[e]||e)+"."+{…}[e]+".chunk.js"}`
    instead of assigning `.u`. GitLab is webpack 4 and reported 0 chunks from a runtime declaring
    1,272 of them."""
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "webpack4-jsonp", "app.js")
    res = jshound.chunk_map(open(fx).read())
    assert set(res["chunks"]) == {"vendors-main.073e6052.chunk.js",
                                  "pages.admin.e5fd8aa1.chunk.js"}, res["chunks"]


def test_one_builder_is_not_counted_twice():
    """webpack 5's `r.u = e => …` is an assignment AND a function, so both visitors see it. The demo
    fixture went from 3 ids seen to 6 while still producing 3 URLs."""
    demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "demo-app", "app.4f2a9c1e.js")
    st = jshound.chunk_map(open(demo).read())["stats"]
    assert st["idsSeen"] == 3 and st["idsResolved"] == 3 and st["builders"] == 1


def test_the_jsonp_runtime_is_not_mistaken_for_a_url_builder():
    """webpack 4's chunk-loading runtime is also a one-parameter function that mentions ".js" and
    indexes by its parameter, and it CONTAINS the real builder, so a nested-aware check promotes
    both. GitLab then reported 2,553 ids seen against 1,272 resolved, reading as a 50% failure when
    half the denominator was never a chunk id."""
    runtime = (
        '(function(){function outer(e){'
        '  function jsonpScriptSrc(c){return "/a/"+({"x":"x"}[c]||c)+"."+{"x":"h1"}[c]+".chunk.js"}'
        '  for (var i in e) { if (e[i]) e[i][0]; }'
        '  return e[0];'
        '}})()')
    res = jshound.chunk_map(runtime)
    assert res["stats"]["builders"] == 1, res["stats"]
    assert res["stats"]["idsSeen"] == res["stats"]["idsResolved"]
    assert res["chunks"] == ["/a/x.h1.chunk.js"]


def test_a_zero_argument_function_is_not_a_chunk_builder():
    """Figma ships `{u: () => i}` and `{u: () => n}`, ordinary object properties that happen to be
    named `u`. Both were counted as chunk URL builders, reporting 2 builders and 12 ids against 0
    resolved. A function that was never told which chunk cannot build its URL."""
    res = jshound.chunk_map('var o = {u: () => i, v: 1}; var p = {u: () => n};')
    assert res["stats"]["builders"] == 0, res["stats"]
    assert res["stats"]["idsSeen"] == 0


def test_a_one_argument_builder_is_still_accepted():
    res = jshound.chunk_map('var o = {u: e => "static/" + {7:"abc"}[e] + ".js"};')
    assert res["chunks"] == ["static/abc.js"], res
