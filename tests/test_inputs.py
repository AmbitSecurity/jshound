"""A zero has to mean "I looked and there was nothing", not "I never looked".

Every command here took a path and, given the wrong KIND of path, reported zero of whatever it
counts with exit 0. `analyze examples/demo-app/app.4f2a9c1e.js` — the file from the line above it in
the README's own quickstart — printed `0 scripts -> 0 records` and, with `-o spec.json`, wrote a
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


def run(*args):
    return subprocess.run([sys.executable, "-m", "jshound.cli", *args],
                          capture_output=True, text=True, cwd=ROOT)


@pytest.fixture
def records(tmp_path):
    out = os.path.join(str(tmp_path), "recs.json")
    r = run("analyze", A_DIR, "--host", "demo-app.test", "-o", out)
    assert r.returncode == 0, r.stderr[-800:]
    return out


@pytest.mark.parametrize("cmd", ["analyze", "graphql", "fingerprint"])
def test_a_file_where_a_directory_belongs_is_refused(cmd):
    r = run(cmd, A_FILE)
    assert r.returncode != 0
    assert "is a file" in r.stderr or "not a directory" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("cmd", ["analyze", "graphql", "fingerprint"])
def test_a_url_where_a_directory_belongs_names_the_fetch_step(cmd):
    """The easy mistake, because `fetch`, `manifest` and `routes` all DO take a URL."""
    r = run(cmd, "https://play.grafana.org")
    assert r.returncode != 0
    assert "is a URL" in r.stderr and "jshound fetch" in r.stderr


@pytest.mark.parametrize("cmd", ["analyze", "graphql", "fingerprint"])
def test_a_missing_directory_is_refused(cmd):
    r = run(cmd, "/nope/nothing-here")
    assert r.returncode != 0
    assert "no such directory" in r.stderr or "not a directory" in r.stderr


def test_precision_will_not_score_against_an_empty_corpus(tmp_path, records):
    """Every endpoint is "not in corpus" when there is no corpus, so this scored 100% invented from
    no evidence at all."""
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    r = run("precision", empty, records)
    assert r.returncode != 0
    assert "0 bytes" in r.stderr and "questionable" not in r.stdout


def test_residual_will_not_issue_a_clean_bill_from_an_empty_corpus(tmp_path, records):
    """The inverse, and the more dangerous direction: "0 missed, 0.0%" is the most reassuring line
    this tool can print."""
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    r = run("residual", empty, records)
    assert r.returncode != 0
    assert "0 files" in r.stderr and "NOT extracted" not in r.stdout


def test_a_file_where_a_directory_belongs_does_not_score_either(records):
    for cmd in ("precision", "residual"):
        r = run(cmd, A_FILE, records)
        assert r.returncode != 0, cmd
        assert "is a file" in r.stderr, cmd


def test_a_real_corpus_still_scores(records):
    r = run("precision", A_DIR, records)
    assert r.returncode == 0, r.stderr[-500:]
    assert "endpoints checked" in r.stdout
    r = run("residual", A_DIR, records)
    assert r.returncode == 0, r.stderr[-500:]
    assert "files read" in r.stdout


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
    This returned `{}` with an empty stderr and exit 0 — a bundle that declares no chunks."""
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
