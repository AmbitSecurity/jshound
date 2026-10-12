"""What is allowed to come out of the chunk map as a URL.

Six places in `chunkmap.js` add to the chunk set, so the guard sits at the one point they all pass
through: a filter per call site is a filter the next call site forgets.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound   # noqa: E402


def test_a_string_that_cannot_be_a_url_path_is_not_a_chunk():
    """Supabase's runtime holds the whole Google Tag Manager snippet in an id map,
    `(function(w,d,s,l,i){w[l]=w[l]||[]...`, and it came back out as a chunk URL to go and
    download. A URL path component cannot carry whitespace, a quote, a bracket or a brace without
    being escaped first, so a string holding any of them was never a path."""
    src = ('(function(){var r={};r.p="";'
           'r.u=function(e){return ({'
           '1:"static/chunks/real.a1b2.js",'
           '2:"(function(w,d,s,l,i){w[l]=w[l]||[];})(window,document)",'
           '3:"has a space.js"'
           '})[e]};r.e(1);r.e(2);r.e(3);})()')
    res = jshound.chunk_map(src)
    assert "static/chunks/real.a1b2.js" in res["chunks"]
    for c in res["chunks"] + res["speculative"]:
        assert not any(ch in c for ch in "(){} \"'"), c


def test_what_the_guard_rejected_is_counted_not_dropped_quietly():
    """"40 declared, 3 of them not paths" is a different report from "37 declared"."""
    src = ('(function(){var r={};r.p="";'
           'r.u=function(e){return ({1:"a.js",2:"not a path.js"})[e]};r.e(1);r.e(2);})()')
    res = jshound.chunk_map(src)
    assert res["stats"]["notPathLike"] >= 1


def test_a_clean_bundle_rejects_nothing():
    fix = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "examples", "demo-app", "app.4f2a9c1e.js")
    res = jshound.chunk_map(open(fix).read())
    assert len(res["chunks"]) == 3
    assert res["stats"]["notPathLike"] == 0
    assert res["stats"]["idsSeen"] == res["stats"]["idsResolved"] == 3


def test_a_chain_of_id_tests_gives_each_id_its_own_url():
    """discord.com's builder is a chain of 5,400 `"715528"===e?""+e+".5ff9…js":…` tests. Taking the
    first branch whenever it resolved gave all 5,397 ids chunk 715528's hash: 5,397 URLs that 404,
    and the real chunks never fetched. The test decides the branch."""
    import jshound
    src = ('var T={};T.u=e=>"11"===e?""+e+".aaaaaaaa.js":"22"===e?""+e+".bbbbbbbb.js":'
           '"33"===e?""+e+".cccccccc.js":""+e+".dddddddd.js";'
           'T.e("11");T.e("22");T.e("33");T.e("44");')
    got = set(jshound.chunk_urls(src))
    assert {"11.aaaaaaaa.js", "22.bbbbbbbb.js", "33.cccccccc.js", "44.dddddddd.js"} <= got, got
    assert not any(u.startswith("22.aaaaaaaa") or u.startswith("44.aaaaaaaa") for u in got)


def test_a_dotted_list_of_words_is_not_a_chunk():
    """claude.ai ships a list of SQL function names joined by dots, `...rpads.ltrim.md5...
    jsonb_agg.jsonb_object_agg...`. The text scan matched it up to the `.js` of `.jsonb_object_agg`
    and fetched a 900-character "chunk" from two hosts."""
    words = ".".join(["abs", "acos", "atan2d", "rpads", "ltrim", "md5", "octet_length", "position",
                      "json_to_recordset", "jsonb_agg", "jsonb_object_agg", "justify_days"])
    src = 'var k="' + words + '".split(".");import("./assets/route-a1b2c3d4.js");'
    res = jshound.chunk_map(src)
    every = res["chunks"] + res["speculative"]
    assert not any("ltrim" in c for c in every), every
