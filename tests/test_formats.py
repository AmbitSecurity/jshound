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


def _run(*args):
    r = subprocess.run([sys.executable, "-m", "jshound.cli", *args],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-1200:]
    return r.stdout


def test_json_is_one_array():
    recs = json.loads(_run("analyze", FIX, "--host", "demo-app.test"))
    assert isinstance(recs, list) and recs


def test_jsonl_is_one_object_per_line():
    out = _run("analyze", FIX, "--host", "demo-app.test", "-f", "jsonl")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines
    for ln in lines:                      # each line stands alone — this is the whole point
        assert isinstance(json.loads(ln), dict)


def test_both_formats_carry_the_same_records():
    arr = json.loads(_run("analyze", FIX, "--host", "demo-app.test"))
    lines = [json.loads(ln) for ln in
             _run("analyze", FIX, "--host", "demo-app.test", "-f", "jsonl").splitlines() if ln.strip()]
    assert arr == lines


def test_export_reads_either_shape(tmp_path):
    """`analyze -o f` then `export f` is the documented pipeline, so it has to accept both."""
    import jshound
    arr = os.path.join(str(tmp_path), "a.json")
    lines = os.path.join(str(tmp_path), "a.jsonl")
    _run("analyze", FIX, "--host", "demo-app.test", "-o", arr)
    _run("analyze", FIX, "--host", "demo-app.test", "-f", "jsonl", "-o", lines)
    assert jshound.read_records(arr) == jshound.read_records(lines)
    assert _run("export", arr, "-f", "urls") == _run("export", lines, "-f", "urls")


def test_an_exported_document_is_not_mistaken_for_records(tmp_path):
    spec = os.path.join(str(tmp_path), "spec.json")
    _run("analyze", FIX, "--host", "demo-app.test", "-f", "openapi", "-o", spec)
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "export", spec, "-f", "urls"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1
    assert "not records" in r.stderr and "Traceback" not in r.stderr


# ---- -f table: the glance, with the markers intact ---------------------------------------------

def _table(records):
    from jshound.export import table
    return table.build(records)


def test_table_carries_the_method_the_body_and_the_encoding():
    rows = _table([{"method": "post", "template": "/v1/media", "body_keys": ["file"],
                    "body_encoding": "multipart", "body_encoding_basis": "read from the call site"}])
    assert rows == "POST   /v1/media  {file}  multipart/form-data"


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
