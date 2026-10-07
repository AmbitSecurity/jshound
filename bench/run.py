#!/usr/bin/env python3
"""bench/run.py — compare jshound against the other JavaScript analysers, reproducibly.

    python bench/run.py ./bodies --host app.example.com

Runs whatever is installed over the SAME corpus of already-downloaded files and prints a table.
Tools that are not on PATH are reported as absent rather than silently skipped.

TWO THINGS THIS MEASURES THAT A PATH COUNT DOES NOT.

**Noise.** Run a string extractor over a webpack bundle and it reports hundreds of "endpoints".
Read them and most are documentation links plus strings like `/VwdtK` and `/+U0Y/` — minifier output
that happens to contain a slash. `bench/classify.py` buckets every tool's output by the same rules,
so debris cannot be counted as attack surface.

**Attribution.** A crawl saves scripts from every host a page loads: the app, its CDN, the chat
widget, the error reporter, Google. A tool with no notion of origin reports all of them as the
target's surface. Measured on one corpus of 12 hosts, an extractor's apparent API lead came from
`/v1/chooseServer` (www.gstatic.com) and `/sheriff/api/v1/rules/search` (tradingview-widget.com) —
other people's endpoints, in a report about yours. `--host` turns that into a column: `on-target` vs
`3rd-party`, resolved through the crawler's own `_origin.jsonl`.

Reporting more paths is easy. Reporting the target's requests is the job.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jshound.export.paths import looks_like_source_file, normalize   # noqa: E402
from classify import tally                                            # noqa: E402

LF = os.environ.get("LINKFINDER", "LinkFinder/linkfinder.py")
PY_BIN = os.environ.get("BENCH_PYTHON", sys.executable)


def _norm(s):
    if not s or not isinstance(s, str):
        return None
    s = s.split("?", 1)[0]
    if not s.startswith("/"):
        return None
    p, _params, _ok = normalize(s)
    return None if looks_like_source_file(p) else p


def origin_hosts(bodies):
    """-> {saved filename: host that served it}, from the crawler's manifest."""
    out = {}
    q = os.path.join(bodies, "_origin.jsonl")
    if not os.path.exists(q):
        return out
    for line in open(q, errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        u = r.get("u") or ""
        if r.get("f") and "://" in u:
            out[r["f"]] = u.split("/")[2].lower()
    return out


def on_target(host, targets):
    """None when we cannot tell. `targets` is a list because an app's own assets routinely live on a
    different hostname — Grafana serves play.grafana.org from grafana-assets.grafana.net — and
    calling a target's own CDN 'third-party' would be a worse error than not attributing at all.
    Only the operator knows which hosts are theirs, so they pass them."""
    if not targets or not host or host == "unattributed.invalid":
        return None
    return any(host == t or host.endswith("." + t) for t in targets)


def _files(bodies):
    for root, _d, fs in os.walk(bodies):
        for f in fs:
            if f.endswith((".js", ".mjs", ".cjs")):
                yield os.path.join(root, f)


# Each runner returns (paths:set, with_method:int, with_body:int, path_host:{path: host})
def run_jshound(bodies, targets):
    import jshound as bm
    recs = bm.analyze(bodies)                       # unfiltered; we attribute here, like every tool
    eps = [r for r in recs if (r.get("_kind") or "endpoint") in ("endpoint", "route")]
    paths, ph, wm, wb = set(), {}, 0, 0
    for r in eps:
        p = _norm(r.get("template") or r.get("url"))
        if not p:
            continue
        paths.add(p)
        ph.setdefault(p, (r.get("_host") or "").lower())
        if r.get("method") and r.get("method") != "UNKNOWN":
            wm += 1
        if r.get("body_keys") or isinstance(r.get("body_schema"), dict):
            wb += 1
    return paths, wm, wb, ph


def run_linkfinder(bodies, targets):
    script = shutil.which("linkfinder.py") or (LF if os.path.exists(LF) else None)
    if not script:
        return None
    hosts = origin_hosts(bodies)
    paths, ph = set(), {}
    for f in _files(bodies):
        try:
            r = subprocess.run([PY_BIN, script, "-i", f, "-o", "cli"],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        h = hosts.get(os.path.basename(f), "")
        for line in r.stdout.splitlines():
            p = _norm(line.strip())
            if p:
                paths.add(p)
                ph.setdefault(p, h)
    return paths, 0, 0, ph


def run_xnlinkfinder(bodies, targets):
    exe = shutil.which("xnLinkFinder") or os.path.join(os.path.dirname(PY_BIN), "xnLinkFinder")
    if not os.path.exists(exe):
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "o.txt")
        # A CATCH-22 THAT MAKES THIS TOOL NOT COMPARABLE ON A LOCAL CORPUS, stated rather than
        # scored. `-sf` is mandatory, and it filters results by the DOMAIN of each link — but paths
        # extracted from a bundle are relative (`/api/foo`) and carry no domain, so any scope value
        # removes all of them and the run writes an empty file. Omitting `-sf` makes it refuse to
        # start. It is built to crawl live URLs, which is a different job from reading files on
        # disk, and reporting "0 paths" for that would be a result it never had a chance to earn.
        # A wildcard is tried because it is the only value that could pass relative paths through.
        cmd = [exe, "-i", bodies, "-o", out, "-sf", "*"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            print("  ! xnLinkFinder timed out", file=sys.stderr)
            return None
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            print("  ! xnLinkFinder: not comparable on a file corpus — its mandatory scope filter "
                  "matches on link domain, and bundle paths are relative. Reported as absent, not "
                  "as zero.", file=sys.stderr)
            return None
        paths = set()
        if os.path.exists(out):
            for line in open(out, errors="replace"):
                p = _norm(line.strip())
                if p:
                    paths.add(p)
    return paths, 0, 0, {}          # scans a directory; no per-file provenance to attribute with


def run_jsluice(bodies, targets):
    """jsluice DOES report a method and a bodyParams list for the sinks it understands. An earlier
    version of this runner read only `url`, scoring it 0 on both columns — the same unfairness as
    mis-invoking xnLinkFinder, and it would have been the first thing anyone checked."""
    if not shutil.which("jsluice"):
        return None
    hosts = origin_hosts(bodies)
    paths, ph, wm, wb = set(), {}, 0, 0
    for f in _files(bodies):
        r = subprocess.run(["jsluice", "urls", f], capture_output=True, text=True)
        h = hosts.get(os.path.basename(f), "")
        for line in r.stdout.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            p = _norm(rec.get("url", ""))
            if not p:
                continue
            paths.add(p)
            ph.setdefault(p, h)
            if rec.get("method"):
                wm += 1
            if rec.get("bodyParams"):
                wb += 1
    return paths, wm, wb, ph


TOOLS = [("jshound", run_jshound), ("jsluice", run_jsluice),
         ("LinkFinder", run_linkfinder), ("xnLinkFinder", run_xnlinkfinder)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bodies")
    ap.add_argument("--host", help="target host(s), comma-separated. An app's own CDN counts as "
                                   "on-target, so list it: --host play.grafana.org,grafana-assets.grafana.net")
    ap.add_argument("--json", help="write raw results here")
    a = ap.parse_args()

    targets = [h.strip().lower() for h in (a.host or "").split(",") if h.strip()]
    results, absent = {}, []
    for name, fn in TOOLS:
        try:
            got = fn(a.bodies, targets)
        except Exception as e:                                   # noqa: BLE001
            print(f"  ! {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
            got = None
        if got is None:
            absent.append(name)
            continue
        results[name] = got
        print(f"  {name}: {len(got[0])} paths", file=sys.stderr)

    hdr = f"\n{'tool':<14}{'total':>7}{'api':>5}{'content':>9}{'asset':>7}{'noise':>7}{'+method':>9}{'+body':>7}"
    if targets:
        hdr += f"{'on-target':>11}{'3rd-party':>11}{'unattrib':>10}"
    print(hdr)
    print("-" * (len(hdr) - 1))
    for name, (paths, wm, wb, ph) in results.items():
        t = tally(paths)
        row = (f"{name:<14}{len(paths):>7}{t['api']:>5}{t['content']:>9}{t['asset']:>7}"
               f"{t['fragment']:>7}{wm:>9}{wb:>7}")
        if targets:
            if ph:
                tgt = sum(1 for p in paths if on_target(ph.get(p), targets))
                third = sum(1 for p in paths if on_target(ph.get(p), targets) is False)
                row += f"{tgt:>11}{third:>11}{len(paths) - tgt - third:>10}"
            else:
                row += f"{'n/a':>11}{'n/a':>11}{'n/a':>10}"
        print(row)

    if targets:
        print("\nAPI-shaped paths that belong to someone else:")
        for name, (paths, _m, _b, ph) in results.items():
            bad = sorted(p for p in paths
                         if tally([p])["api"] and on_target(ph.get(p), targets) is False)
            if bad:
                print(f"  {name}: {len(bad)}  e.g. " + ", ".join(
                    f"{p} ({ph.get(p)})" for p in bad[:3]))

    if absent:
        print(f"\nnot installed (not counted): {', '.join(absent)}")
    print("\napi/content/asset/noise bucket every tool's output by the same rules (bench/classify.py).\n"
          "on-target/3rd-party resolve each path to the host whose script contained it, via the\n"
          "crawler's _origin.jsonl. +method / +body are records carrying a verb and a resolved body\n"
          "contract — a path-set comparison cannot express either, which is why totals understate it.")
    if a.json:
        json.dump({k: {"paths": sorted(v[0]), "hosts": v[3]} for k, v in results.items()},
                  open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
