"""The two record formats, and that each is what its name says.

`-f jsonl` used to emit a pretty-printed JSON ARRAY. Nothing in the repo noticed, because every test
and every example read it back with `json.load`, which is exactly why a user doing `jq -c` per line
or `while read line` was the one who found out.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "examples", "demo-app")


def _results(tmp_path):
    """Run the real command on the demo app and return its results folder."""
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-m", "jshound.cli", FIX, "-o", str(out), "-q"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-1200:]
    return out, r.stdout


def test_every_output_is_written(tmp_path):
    out, _ = _results(tmp_path)
    for name in ("endpoints.txt", "requests.jsonl", "urls.txt", "openapi.json"):
        assert (out / name).exists(), name


def test_the_extractors_working_out_is_not_an_output(tmp_path):
    """`records.json` was the extractor's internal state, about 90% of it not requests at all, and it
    made the output look like a debug dump. It stays inside the library, where pentest reads it."""
    out, _ = _results(tmp_path)
    assert not (out / "records.json").exists()


def test_requests_jsonl_is_one_request_per_line(tmp_path):
    out, _ = _results(tmp_path)
    lines = [json.loads(ln) for ln in (out / "requests.jsonl").read_text().splitlines() if ln.strip()]
    assert lines
    for ln in lines:
        assert ln["method"] and ln["url"], ln


def test_the_file_and_the_screen_list_the_same_requests(tmp_path):
    out, stdout = _results(tmp_path)
    rows = [ln for ln in stdout.splitlines() if ln.strip()]
    lines = [ln for ln in (out / "requests.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == len(lines) > 0


def test_nothing_is_truncated(tmp_path):
    """Bodies were cut at 64 characters with `,...}`, on screen and in the saved file, where the rest
    could not be recovered at all."""
    out, stdout = _results(tmp_path)
    assert ",...}" not in stdout and ",...}" not in (out / "endpoints.txt").read_text()


def test_read_records_takes_either_shape_and_refuses_an_export(tmp_path):
    """The library reader used by `precision` and `residual`."""
    import jshound
    recs = [{"method": "GET", "template": "/a"}]
    arr, jl, spec = tmp_path / "a.json", tmp_path / "a.jsonl", tmp_path / "spec.json"
    arr.write_text(json.dumps(recs))
    jl.write_text("\n".join(json.dumps(r) for r in recs))
    spec.write_text(json.dumps({"openapi": "3.1.0", "paths": {}}))
    assert jshound.read_records(str(arr)) == jshound.read_records(str(jl)) == recs
    import pytest
    with pytest.raises(ValueError):
        jshound.read_records(str(spec))


def _table(records):
    from jshound.export import table
    return table.build(records)


def test_table_carries_the_method_the_body_and_the_encoding():
    rows = _table([{"method": "post", "template": "/v1/media", "body_keys": ["file"],
                    "body_encoding": "multipart", "body_encoding_basis": "read from the call site"}])
    assert rows == "POST   /v1/media  multipart/form-data  {file}"


def test_a_partial_body_is_marked():
    """`body_partial` means the named fields are real and the list is incomplete. A format that drops
    that marker turns a typed hand-off into a claim of completeness."""
    rows = _table([{"method": "POST", "template": "/v1/x", "body_keys": ["a"], "body_partial": True,
                    "content_type": "application/json"}])
    assert "~{a}" in rows


def test_a_body_is_rendered_as_a_shape_not_a_list_of_names():
    """A body is a document. `body: email, pageUri` is not one — the records carry the shape, with
    each value saying where that field comes from, so that is what is printed."""
    rows = _table([{"method": "POST", "template": "/v1/subscribe",
                    "body_keys": ["email", "pageUri"],
                    "body_shape": {"email": "computed:trim", "pageUri": "state:href"},
                    "content_type": "application/json"}])
    assert '{"email":"computed:trim","pageUri":"state:href"}' in rows


def test_a_shape_is_preferred_over_an_unresolvable_schema():
    """A FormData upload has a usable shape and an unresolvable schema; printing `{EXPR}` there would
    throw away the one field that did resolve."""
    rows = _table([{"method": "POST", "template": "/v1/media", "body_keys": ["file"],
                    "body_shape": {"file": "file-upload"}, "body_schema": "{EXPR}"}])
    assert '{"file":"file-upload"}' in rows and "{EXPR}" not in rows


def test_an_inferred_encoding_is_marked():
    """Read at the call site and inferred from the file's other builders are different claims."""
    read = _table([{"method": "POST", "template": "/v1/x", "body_encoding": "json",
                    "body_encoding_basis": "read from the call site"}])
    guessed = _table([{"method": "POST", "template": "/v1/x", "body_encoding": "json",
                       "body_encoding_basis": "INFERRED: the only request builder in this file"}])
    assert read.endswith("application/json") and not read.endswith("?")
    assert guessed.endswith("application/json?")


def test_columns_never_collide_however_long_a_row_is():
    """A long path or a long body list is wider than its column, and `f"{s:<{w}}"` does not pad a
    string that already exceeds the width — so two fields ran together with no separator."""
    rows = _table([
        {"method": "POST", "template": "/a", "body_keys": ["x"], "content_type": "application/json"},
        {"method": "POST", "template": "/" + "p" * 90,
         "body_keys": ["k" + str(i) for i in range(12)], "content_type": "application/json"},
    ]).splitlines()
    for line in rows:
        assert "kapplication" not in line and "papplication" not in line
        assert "  application/json" in line


def test_a_record_that_is_not_an_endpoint_is_left_out():
    assert _table([{"_kind": "callargs", "name": "fetch", "fields": ["method"]}]) == ""


def test_a_wrapper_path_is_not_pinned_to_the_page():
    """A browser sink resolves `/x` against the page. A wrapper (`api.post("/platform/...")`) puts its
    own base in front, and on supabase that base is the API host, so naming the page would print a URL
    the app never requests."""
    from jshound.export.requests import _url
    page = "https://app.example.com/dash"
    assert _url({"template": "/v1/x", "_page": page, "sink": "fetch"}) == "https://app.example.com/v1/x"
    assert _url({"template": "/platform/x", "_page": page, "sink": "opts-sink"}) == "/platform/x"


def test_a_path_without_a_slash_resolves_like_the_browser_resolves_it():
    """Gluing `apis/x` onto the host printed `https://play.grafana.orgapis/x`."""
    from jshound.export.requests import _url
    assert _url({"template": "apis/x", "_page": "https://a.test/d/", "sink": "fetch"}) == "https://a.test/d/apis/x"
    assert _url({"template": "apis/x", "_page": "https://a.test/d", "sink": "fetch"}) == "https://a.test/apis/x"


def test_columns_stay_aligned_when_a_row_has_no_body():
    rows = _table([
        {"method": "GET", "template": "/a/very/long/path/that/is/longer", "content_type": "application/json"},
        {"method": "POST", "template": "/b", "body_keys": ["x"], "content_type": "text/plain"},
    ]).splitlines()
    assert rows[0].index("application/json") == rows[1].index("text/plain")
