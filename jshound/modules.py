#!/usr/bin/env python3
"""modules.py: turn a webpack bundle back into a module tree you can read.

A bundler splits code and then has to link it again, and it writes that linkage into the output: numeric
module ids, an export table, and imports by id. None of that is lossy. Minification destroyed LOCAL names;
it kept module boundaries, the import graph, and every exported and property name, because those are the
contract. That structure is what makes a value followable across a module boundary instead of being a dead
end in one 26MB line, which is the concrete reason "indirect control flow" reads as impossible.

  modules.py <outdir> <file.js…>        write the tree, print a summary

WHAT THIS DOES NOT DO, measured rather than assumed: it keys on webpack's module registry, so it recovers
nothing from a flat ESM bundle. Keycloak's admin UI is Vite/Rollup and came back as 1 module / 0 imports,
that is the tool correctly reporting "no registry here", not a parse failure. Check `modules` in the output
before drawing any conclusion from a small tree, and reach for analyze.py instead when it is 1.
"""
import json, os, subprocess, sys

from jshound.nodeenv import node_env

HERE = os.path.dirname(os.path.abspath(__file__))
# Babel and terser are installed globally in the image; NODE_PATH points node at them from any cwd.
NODE_PATH = os.environ.get("NODE_PATH") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            "node_modules")
UNBUNDLE = os.path.join(HERE, "js", "unbundle.js")


def unbundle(outdir, paths):
    if not os.path.exists(UNBUNDLE):
        sys.exit(f"unbundle.js not found at {UNBUNDLE}")
    env = node_env()
    env.setdefault("NODE_PATH", NODE_PATH)
    r = subprocess.run(["node", "--max-old-space-size=4096", UNBUNDLE, outdir] + list(paths),
                       capture_output=True, text=True, env=env, timeout=900)
    if r.returncode != 0:
        # Never swallow this. An empty tree and a crashed extractor look identical from the outside, and
        # that ambiguity is how a silent failure gets read as "this bundle had nothing in it".
        print(f"! unbundle failed rc={r.returncode}: {(r.stderr or '')[:400]}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print(f"! unbundle produced no summary: {(r.stdout or '')[:300]}", file=sys.stderr)
        return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    outdir, paths = sys.argv[1], [p for p in sys.argv[2:] if os.path.exists(p)]
    if not paths:
        sys.exit("no readable input files")
    s = unbundle(outdir, paths)
    if not s:
        sys.exit(1)
    print(json.dumps(s))
    n = s.get("modules", 0)
    if n <= 1:
        print(f"  {n} module(s): no webpack registry in this bundle (Vite/Rollup/esbuild ship flat ESM).\n"
              f"  This is a correct negative, not a failure. Use analyze.py for contracts here.",
              file=sys.stderr)
    else:
        print(f"  {n} modules, {s.get('with_exports',0)} with exports, {s.get('with_imports',0)} with imports"
              f"  -> {outdir}", file=sys.stderr)


if __name__ == "__main__":
    main()
