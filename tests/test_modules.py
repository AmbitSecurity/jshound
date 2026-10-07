"""Module registry recovery. webpack emits ids in whatever `optimization.moduleIds` is set to, and
each mode broke this differently on a real target, so all three are covered here along with the
object that must NOT be mistaken for a registry."""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNBUNDLE = os.path.join(ROOT, "jshound", "js", "unbundle.js")


def _run(src):
    with tempfile.TemporaryDirectory() as d:
        js = os.path.join(d, "b.js")
        open(js, "w").write(src)
        out = os.path.join(d, "out")
        r = subprocess.run(["node", UNBUNDLE, out, js], capture_output=True, text=True,
                           env={**os.environ, "NODE_PATH": os.path.join(ROOT, "node_modules")})
        assert r.returncode == 0, r.stderr[-1500:]
        try:
            return json.loads(r.stdout or "{}"), sorted(
                os.listdir(os.path.join(out, "modules"))) if os.path.isdir(
                    os.path.join(out, "modules")) else []
        except ValueError:
            return {}, []


def _map(entries):
    body = ",".join(f'{k}:function(e,t,r){{{v}}}' for k, v in entries)
    return "(()=>{var m={" + body + "};})()"


def test_numeric_ids():
    res, files = _run(_map([("134", 'r.d(t,{a:()=>1})'), ("6710", 'var o=r(134)'),
                            ("828", 'r.d(t,{b:()=>2})')]))
    assert res.get("modules", 0) >= 3


def test_path_ids_named_moduleids():
    """`moduleIds: 'named'` and anything pnpm-resolved key the map by source path. Element returned
    0 imports and 0 exports from 2.6 MB before this was accepted."""
    res, _f = _run(_map([('"../../node_modules/.pnpm/x@1/src/logger.ts"', 'r.d(t,{a:()=>1})'),
                         ('"src/vector/app.tsx"', 'var o=r("../../node_modules/.pnpm/x@1/src/logger.ts")'),
                         ('"src/hooks/useFocus.ts"', 'r.d(t,{b:()=>2})')]))
    assert res.get("modules", 0) >= 3


def test_hashed_ids_deterministic_moduleids():
    """GitLab's 1.4 MB bundle is 849 entries keyed `++os`, `+18S`, `/23t` — exactly one numeric.
    A key-format test rejected 848 real modules."""
    entries = [(f'"{c}z{i:02d}"', "r.d(t,{a:()=>1})") for i, c in enumerate("abcdefghij")]
    res, _f = _run(_map(entries))
    assert res.get("modules", 0) >= 8


def test_an_id_containing_a_slash_does_not_abort_the_run():
    """base64 ids include `/` — GitLab ships a module called `0/Qg`. Writing that straight to disk
    threw ENOENT and killed the run after earlier modules had been written, so the output looked
    merely small rather than aborted."""
    entries = [(f'"0/Q{i}"', "r.d(t,{a:()=>1})") for i in range(10)]
    res, _f = _run(_map(entries))
    assert res.get("modules", 0) >= 8


def test_an_ordinary_object_of_callbacks_is_not_a_registry():
    src = ("(()=>{var handlers={onClick:function(e){return 1},onSubmit:function(e){return 2},"
           "onChange:function(e){return 3}};})()")
    res, _f = _run(src)
    assert res.get("modules", 0) == 0


def test_a_module_id_cannot_write_outside_the_output_directory():
    """A path id is attacker-influenced data: it comes from a bundle served by the target. Writing
    it verbatim would place files anywhere on disk the bundle asked for. The escape is flattened
    rather than dropped, so the module is still recovered."""
    with tempfile.TemporaryDirectory() as d:
        js = os.path.join(d, "b.js")
        open(js, "w").write(
            '(()=>{var m={"../../../../etc/passwd.js":function(e,t,r){r.d(t,{a:()=>1})},'
            '"src/a.ts":function(e,t,r){var o=r("../../../../etc/passwd.js")},'
            '"src/b.ts":function(e,t,r){r.d(t,{b:()=>2})}};})()')
        out = os.path.join(d, "out")
        r = subprocess.run(["node", UNBUNDLE, out, js], capture_output=True, text=True,
                           env={**os.environ, "NODE_PATH": os.path.join(ROOT, "node_modules")})
        assert r.returncode == 0, r.stderr[-800:]
        assert json.loads(r.stdout)["modules"] == 3          # recovered, not dropped
        written = []
        for root, _dirs, files in os.walk(out):
            written += [os.path.realpath(os.path.join(root, f)) for f in files]
        assert written, "nothing was written"
        for p in written:
            assert p.startswith(os.path.realpath(out)), p
