"""A zero has to mean "I looked and there was nothing", not "I never looked".

Every command here took a path and, given the wrong KIND of path, reported zero of whatever it
counts with exit 0. `analyze examples/demo-app/app.4f2a9c1e.js`, the file from the line above it in
the README's own quickstart, printed `0 scripts -> 0 records` and, with `-o spec.json`, wrote a
valid, importable, empty OpenAPI document. That reads as "this host has no API": a claim about the
target, manufactured from a typo.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A_FILE = os.path.join(ROOT, "examples", "demo-app", "app.4f2a9c1e.js")
A_DIR = os.path.join(ROOT, "examples", "demo-app")


def _guard(fn, *args):
    """Call a library entry point that reads a corpus and return what it refused with, or None."""
    try:
        fn(*args)
    except SystemExit as e:
        return str(e)
    return None


@pytest.fixture
def records(tmp_path):
    import jshound
    out = tmp_path / "recs.json"
    out.write_text(json.dumps(jshound.analyze(A_DIR, host="demo-app.test")))
    return str(out)


def _readers():
    """Every library function that reads a directory of bodies."""
    import jshound
    from jshound.fingerprint import scan as fingerprint
    from jshound.graphqlops import scan as graphql
    return {"analyze": jshound.analyze, "graphql": graphql, "fingerprint": fingerprint}


@pytest.mark.parametrize("name", ["analyze", "graphql", "fingerprint"])
def test_a_file_where_a_directory_belongs_is_refused(name):
    why = _guard(_readers()[name], A_FILE)
    assert why and ("is a file" in why or "not a directory" in why), why


@pytest.mark.parametrize("name", ["analyze", "graphql", "fingerprint"])
def test_a_url_where_a_directory_belongs_names_the_fetch_step(name):
    """The easy mistake, because the command itself takes a URL."""
    why = _guard(_readers()[name], "https://play.grafana.org")
    assert why and "is a URL" in why, why


@pytest.mark.parametrize("name", ["analyze", "graphql", "fingerprint"])
def test_a_missing_directory_is_refused(name):
    why = _guard(_readers()[name], "/nope/nothing-here")
    assert why and ("no such directory" in why or "not a directory" in why), why


def test_precision_will_not_score_against_an_empty_corpus(tmp_path, records):
    """Every endpoint is "not in corpus" when there is no corpus, so this scored 100% invented from
    no evidence at all."""
    from jshound.precision import check
    empty = tmp_path / "empty"
    empty.mkdir()
    why = _guard(check, str(empty), records)
    assert why and "0 bytes" in why, why


def test_residual_will_not_issue_a_clean_bill_from_an_empty_corpus(tmp_path, records):
    """The inverse, and the more dangerous direction: "0 missed, 0.0%" is the most reassuring line
    this tool can print."""
    from jshound.residual import classify
    empty = tmp_path / "empty"
    empty.mkdir()
    why = _guard(classify, str(empty), records)
    assert why and "0 files" in why, why


def test_a_file_where_a_directory_belongs_does_not_score_either(records):
    from jshound.precision import check
    from jshound.residual import classify
    for fn in (check, classify):
        why = _guard(fn, A_FILE, records)
        assert why and "is a file" in why, fn.__module__


def test_a_real_corpus_still_scores(records, capsys):
    from jshound.precision import check
    from jshound.residual import classify
    assert _guard(check, A_DIR, records) is None
    assert _guard(classify, A_DIR, records) is None
    out = capsys.readouterr().out
    assert "endpoints checked" in out and "files read" in out


# ---- a helper that could not run is not a corpus without endpoints -----------------------------
#
# Every node helper here was called as `json.loads(r.stdout or b"{}")`, which turns a crash into an
# empty result: `static_ast.js` prints `[]` from its own top-level catch, `chunkmap.js` printed `{}`,
# and the manifest readers reported an app whose build declares no routes. The exit code is the only
# thing that separates those from the real thing.

class _Ran:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_clean_exit_is_not_a_failure():
    from jshound.nodeenv import node_failure
    assert node_failure(_Ran(0, b"[]")) is None


def test_a_non_zero_exit_is_a_failure_even_with_output():
    """`returncode != 0 and not r.stdout` was the old condition, and it never fired: the extractor
    always prints `[]` before exiting, so every crashed batch had stdout and was counted as OK."""
    from jshound.nodeenv import node_failure
    assert node_failure(_Ran(1, b"[]", b"Error: boom\n")) == "Error: boom"


def test_a_clean_exit_with_no_output_is_a_failure_when_output_was_required():
    from jshound.nodeenv import node_failure
    assert node_failure(_Ran(0, b"")) is None                      # tolerated by default
    assert node_failure(_Ran(0, b"", b""), need_output=True)       # but not when it must produce


def test_the_reason_is_the_error_line_not_nodes_version_banner():
    from jshound.nodeenv import node_failure
    stderr = (b"node:internal/modules/cjs/loader:1143\n  throw err;\n"
              b"Error: Cannot find module '@babel/parser'\n    at Module._resolveFilename\n"
              b"Node.js v20.17.0\n")
    why = node_failure(_Ran(1, b"", stderr))
    assert why.startswith("Error: Cannot find module")
    assert "npm install" in why                 # actionable, and node's banner is not the reason


def test_chunk_map_raises_when_the_extractor_cannot_run(tmp_path, monkeypatch):
    """A non-editable install puts the JS where no `node_modules` is above it, so Babel cannot load.
    This returned `{}` with an empty stderr and exit 0, a bundle that declares no chunks."""
    import shutil

    import jshound
    pkg = tmp_path / "jshound"
    (pkg / "js").mkdir(parents=True)
    for f in ("chunkmap.js", "literal.js"):
        shutil.copy(os.path.join(ROOT, "jshound", "js", f), str(pkg / "js" / f))
    monkeypatch.setattr(jshound, "__file__", str(pkg / "__init__.py"))
    monkeypatch.delenv("NODE_PATH", raising=False)
    with pytest.raises(RuntimeError) as e:
        jshound.chunk_map(open(A_FILE).read())
    assert "Cannot find module" in str(e.value) and "npm install" in str(e.value)


def test_chunk_map_still_works_normally():
    res = __import__("jshound").chunk_map(open(A_FILE).read())
    assert len(res["chunks"]) == 3 and res["stats"]["idsResolved"] == 3


def _cli(*args, stdin=""):
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run([sys.executable, "-m", "jshound.cli", *args], input=stdin,
                          capture_output=True, text=True, cwd=root, timeout=120)


def test_a_mistake_gets_a_sentence_not_a_fetch(tmp_path):
    """`/nonexistent/dir` was fetched as `https:///nonexistent/dir` and reported "unreachable", and
    `htps://x` was attempted as it was typed. Each is now one line saying what is wrong, exit 1."""
    out = str(tmp_path / "out")
    r = _cli("/nonexistent/dir", "-o", out)
    assert r.returncode == 1 and "no such directory" in r.stderr, r.stderr
    r = _cli("htps://example.com", "-o", out)
    assert r.returncode == 1 and "only http and https" in r.stderr, r.stderr
    f = tmp_path / "app.js"
    f.write_text("fetch('/x')")
    r = _cli(str(f), "-o", out)
    assert r.returncode == 1 and "is a file" in r.stderr, r.stderr
    assert "ERR" not in r.stderr, "nothing should have been requested"


def test_nothing_to_read_is_an_error():
    r = _cli(stdin="")
    assert r.returncode == 1 and "nothing to read" in r.stderr
