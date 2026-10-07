"""HAR import is the answer to every target the fetcher cannot reach — an app that injects its own
script tags, sits behind a login, or needs a real browser. These cover the ways a HAR goes wrong."""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fromhar import convert   # noqa: E402


def _har(entries):
    fd, p = tempfile.mkstemp(suffix=".har")
    with os.fdopen(fd, "w") as fh:
        json.dump({"log": {"version": "1.2", "entries": entries}}, fh)
    return p


def _entry(url, mime, text, encoding=None, referer=None):
    content = {"mimeType": mime, "text": text}
    if encoding:
        content["encoding"] = encoding
    headers = [{"name": "Referer", "value": referer}] if referer else []
    return {"request": {"method": "GET", "url": url, "headers": headers},
            "response": {"status": 200, "content": content}}


def test_only_scripts_and_documents_are_written():
    har = _har([
        _entry("https://x.test/a.js", "application/javascript", "fetch('/api/a')"),
        _entry("https://x.test/i.png", "image/png", base64.b64encode(b"x").decode(), "base64"),
        _entry("https://x.test/s.css", "text/css", "body{}"),
        _entry("https://x.test/", "text/html", "<html></html>"),
    ])
    with tempfile.TemporaryDirectory() as d:
        assert convert(har, d) == 2
        names = [f for f in os.listdir(d) if f != "_origin.jsonl"]
        assert sorted(n.rsplit(".", 1)[1] for n in names) == ["html", "js"]


def test_base64_bodies_are_decoded():
    body = "fetch('/api/secret')"
    har = _har([_entry("https://x.test/b.js", "application/javascript",
                       base64.b64encode(body.encode()).decode(), "base64")])
    with tempfile.TemporaryDirectory() as d:
        convert(har, d)
        js = [f for f in os.listdir(d) if f.endswith(".js")][0]
        assert body in open(os.path.join(d, js)).read()


def test_referer_becomes_the_document_so_attribution_is_right():
    """A script served by a CDN belongs, for resolving its relative paths, to the page that loaded
    it — not to the CDN. The Referer is the only record of that in a HAR."""
    har = _har([_entry("https://cdn.test/app.js", "application/javascript", "fetch('/api/a')",
                       referer="https://app.test/dashboard")])
    with tempfile.TemporaryDirectory() as d:
        convert(har, d)
        row = json.loads(open(os.path.join(d, "_origin.jsonl")).readline())
        assert row["u"] == "https://cdn.test/app.js"
        assert row["d"] == "https://app.test/dashboard"


def test_entries_without_bodies_are_counted_not_crashed():
    """Most exporters omit response bodies unless asked, and that must not look like a target with
    no API."""
    har = _har([{"request": {"method": "GET", "url": "https://x.test/a.js", "headers": []},
                 "response": {"status": 200, "content": {"mimeType": "application/javascript"}}}])
    with tempfile.TemporaryDirectory() as d:
        assert convert(har, d) == 0


def test_js_by_extension_when_mime_is_wrong():
    har = _har([_entry("https://x.test/a.js", "application/octet-stream", "fetch('/api/a')")])
    with tempfile.TemporaryDirectory() as d:
        assert convert(har, d) == 1


def test_a_har_round_trips_into_recovered_endpoints(tmp_path):
    """END TO END, because this is the repo's whole answer for an authenticated app and only the
    writing half was covered. SUPPORT.md points at HAR import for anything the fetcher cannot reach —
    behind a login, behind a real browser — so the claim that matters is not "files were written" but
    "endpoints came back", including the parts that need cross-file resolution.

    Every measured number in SUPPORT.md comes from an anonymous fetch. This test is the one place the
    login-gated path is exercised at all, so it checks the harder recoveries rather than just a URL:
    a base held in a const and joined at the call site, an axios instance's `baseURL`, and a header.
    """
    import subprocess

    doc = ('<html><head><script src="/static/a.js"></script>'
           '<script src="/static/b.js"></script></head><body><div id="root"></div></body></html>')
    js1 = ('const API = "/api/v3";'
           'export function me(){ return fetch(API + "/users/me", '
           '{ headers: { "x-org-id": "o1" } }); }')
    js2 = ('import axios from "axios";'
           'const c = axios.create({ baseURL: "https://api.corp.test/v2" });'
           'export const del = (id) => c.delete("/widgets/" + id);')

    entries = [{"request": {"method": "GET", "url": u},
                "response": {"status": 200, "content": {"mimeType": m, "text": t}}}
               for u, m, t in (("https://app.corp.test/", "text/html", doc),
                               ("https://app.corp.test/static/a.js", "application/javascript", js1),
                               ("https://app.corp.test/static/b.js", "application/javascript", js2))]
    har = tmp_path / "session.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))

    bodies = tmp_path / "bodies"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "from-har", str(har),
                        "-o", str(bodies)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]

    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", str(bodies)],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]
    recs = json.loads(r.stdout)
    got = {(x.get("method"), x.get("template")) for x in recs if x.get("sink")}

    # the base is a const in the same file, joined at the call site
    assert ("GET", "/api/v3/users/me") in got, got
    # the base is an axios instance's baseURL, applied to a relative call
    assert ("DELETE", "https://api.corp.test/v2/widgets/{param:id}") in got, got
    # and the request header came along
    assert "x-org-id" in {x.get("name") for x in recs if x.get("_kind") == "client-header"}


def test_captured_json_responses_are_kept():
    """`analyze`'s `json_records()` exists to mine captured API responses and had no way of receiving
    one: the fetcher only writes scripts, and this importer accepted only JS and HTML, so a HAR's API
    bodies were dropped at the door. That is the wrong thing to drop here of all places — a HAR is how
    you reach an application behind a login, and a logged-in session's HAR is mostly API responses.

    `+json` is included because real APIs ship `application/vnd.api+json` and `application/problem+json`.
    Images and stylesheets stay excluded, which is what the filter was always for.
    """
    har = _har([
        _entry("https://x.test/api/v2/me", "application/json", '{"self":"/api/v2/users/42"}'),
        _entry("https://x.test/api/v2/cfg", "application/vnd.api+json", '{"a":"/api/v2/flags"}'),
        _entry("https://x.test/rsc", "text/x-component", '0:["x"]'),
        _entry("https://x.test/i.png", "image/png", base64.b64encode(b"x").decode(), "base64"),
        _entry("https://x.test/s.css", "text/css", "body{}"),
    ])
    with tempfile.TemporaryDirectory() as d:
        assert convert(har, d) == 3
        exts = sorted(f.rsplit(".", 1)[1] for f in os.listdir(d) if f != "_origin.jsonl")
        assert exts == ["json", "json", "rsc"], exts


def test_a_json_body_is_not_written_as_javascript():
    """The extension decides which reader sees the body, and getting it wrong is not a small error: a
    JSON body handed to Babel as `.js` is a parse error that costs the batch, which is the failure the
    document parser's own comment records. `js_files()` reads .js/.mjs/.rsc; `json_records()` reads
    .json/.rsc."""
    har = _har([_entry("https://x.test/api/data", "application/json", '{"k":"/api/v1/x"}')])
    with tempfile.TemporaryDirectory() as d:
        convert(har, d)
        names = [f for f in os.listdir(d) if f != "_origin.jsonl"]
        assert names and all(n.endswith(".json") for n in names), names


def test_a_captured_api_response_round_trips_into_records(tmp_path):
    """END TO END: the response describes more of the API than the page does, which is the whole
    reason `json_records()` was written — it claims 61 paths on one scope and 53 on another that no
    other channel produced. Before this it had no input at all."""
    import subprocess

    api = {"user": {"links": {"self": "/api/v2/users/42", "avatar": "/api/v2/users/42/avatar"}},
           "endpoints": [{"uri": "/api/v2/sessions/{session_id}/archive"}]}
    # `_har` writes the file and returns its PATH.
    har = _har([
        _entry("https://app.test/", "text/html", "<html><body>x</body></html>"),
        _entry("https://app.test/api/v2/me", "application/json", json.dumps(api)),
    ])

    bodies = tmp_path / "bodies"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "from-har", har,
                        "-o", str(bodies)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-600:]
    out = tmp_path / "recs.json"
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", str(bodies),
                        "-o", str(out)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-600:]
    got = {x.get("template") for x in json.loads(out.read_text())}
    assert "/api/v2/users/42/avatar" in got, got
    # the templated one survives as a template rather than as an instance
    assert "/api/v2/sessions/{session_id}/archive" in got, got


def test_a_corpus_with_no_javascript_at_all_still_yields_records(tmp_path):
    """`analyze` returned immediately when `js_files()` was empty, which skipped every channel that
    does not read JavaScript — forms, captured responses, document state, page config. Two real inputs
    hit that: a HAR of an API-heavy session, which is this project's only route to an authenticated
    surface, and a server-rendered page with no bundle. Both reported "0 scripts -> 0 records" while
    holding the surface in plain sight."""
    import subprocess

    har = _har([
        _entry("https://app.test/", "text/html", "<html><body>x</body></html>"),
        _entry("https://app.test/api/v2/me", "application/json",
               '{"user":{"links":{"avatar":"/api/v2/users/42/avatar"}}}'),
    ])
    bodies = tmp_path / "bodies"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-m", "jshound.cli", "from-har", har, "-o", str(bodies)],
                   capture_output=True, text=True, cwd=root, check=True)
    assert not [f for f in os.listdir(bodies) if f.endswith(".js")], "fixture must have no scripts"

    out = tmp_path / "recs.json"
    subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", str(bodies), "-o", str(out)],
                   capture_output=True, text=True, cwd=root, check=True)
    got = {x.get("template") for x in json.loads(out.read_text())}
    assert "/api/v2/users/42/avatar" in got, got


def test_a_directory_with_nothing_readable_still_returns_nothing(tmp_path):
    """The guard did not go away, it moved: nothing of any kind to read means nothing to do."""
    import subprocess
    (tmp_path / "notes.txt").write_text("not a body")
    out = tmp_path / "recs.json"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", str(tmp_path), "-o", str(out)],
                   capture_output=True, text=True, cwd=root, check=True)
    assert json.loads(out.read_text()) == []
