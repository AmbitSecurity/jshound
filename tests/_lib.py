"""What the old `jshound analyze` / `export` / `from-har` subcommands printed, from the library.

The CLI is one command now, so these tests reach the extractor the way pentest does, through the
library, instead of spawning a CLI verb that no longer exists. The returned object has the same
`returncode` / `stdout` / `stderr` the subprocess call did, so each test body reads as it always has.
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _flag(args, *names, default=None):
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
    return default


def analyze(path, *args):
    """`jshound analyze PATH [--host H] [-f FMT] [-o FILE] [--include-unresolved]`."""
    import jshound
    from jshound.export import har, openapi, table, urls
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            if not os.path.isdir(path):
                return Result(1, "", f"analyze: {path!r} is not a directory")
            recs = jshound.analyze(path, host=_flag(args, "--host"))
    except SystemExit as e:
        return Result(1, "", str(e))
    fmt = _flag(args, "-f", "--format", default="json")
    if fmt == "urls":
        out = "\n".join(urls.build(recs, with_method=True,
                                   include_unresolved="--include-unresolved" in args))
    elif fmt == "table":
        out = table.build(recs, with_origin=False)
    elif fmt == "openapi":
        out = json.dumps(openapi.build(recs, title=_flag(args, "--host") or "Recovered API"))
    elif fmt == "har":
        out = json.dumps(har.build(recs))
    elif fmt == "jsonl":
        out = "\n".join(json.dumps(r) for r in recs)
    else:
        out = json.dumps(recs)
    dest = _flag(args, "-o", "--out")
    if dest:
        with open(dest, "w") as fh:
            fh.write(out)
        out = ""
    return Result(0, out + ("\n" if out else ""), err.getvalue())


def from_har(har_path, out_dir):
    """`jshound from-har FILE -o DIR`."""
    from jshound.fromhar import convert
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            convert(har_path, out_dir)
    except SystemExit as e:
        return Result(1, "", str(e))
    return Result(0, "", err.getvalue())
