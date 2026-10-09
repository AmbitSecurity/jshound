#!/usr/bin/env python3
"""live.py: run jshound against real apps and record what happened, one row per target.

    python3 bench/live.py bench/targets.txt --tag 2026-10-09
    python3 bench/live.py bench/targets.txt --tag after --only claude.ai,linear.app

Each target is the real command, `jshound <url> -v`, so what is measured is what a user runs. The
files it fetched are kept under $CORPORA/<name>/js, which is what `bench/regress.sh` diffs two
versions of the extractor over, offline and deterministically. A live site is neither: claude.ai
served a 39 KB login page to one run and its 137 KB app to another an hour apart, so a live number
is a measurement of that moment and a regression is judged on the saved files.

Rows go to $OUT/live-<tag>.jsonl, and a table is printed at the end.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

CORPORA = os.environ.get("CORPORA", os.path.expanduser("~/.cache/jshound-corpora"))
OUT = os.environ.get("OUT", os.path.expanduser("~/.cache/jshound-regress"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def name_of(url):
    u = urlsplit(url if "://" in url else "https://" + url)
    tail = u.path.strip("/").replace("/", "_")
    return (u.hostname or "target").removeprefix("www.") + (f"_{tail}" if tail else "")


def parse(log):
    """The numbers in one run's stderr. Each pattern is a line the CLI or the fetcher prints."""
    row = {"statuses": dict(Counter(m.group(1) for m in re.finditer(r"^  (\d{3}|ERR) ", log, re.M)))}
    m = re.search(r"^\S+\s+(\d{3}|ERR|dir)\s+(.*)$", log, re.M)
    if m:
        row["status"] = m.group(1)
        rest = m.group(2)
        b = re.search(r"blocked by (.+)$", rest)
        row["blocked"] = b.group(1).strip() if b else None
        f = re.search(r"(\d+) files", rest)
        row["files"] = int(f.group(1)) if f else 0
        e = re.search(r"->\s+(\d+) endpoints", rest)
        row["endpoints"] = int(e.group(1)) if e else 0
    c = re.search(r"chunks and imports: (\d+) declared over (\d+) round\(s\), (\d+) fetched, "
                  r"(\d+) already had, (\d+) unreachable(?:; (\d+) of (\d+) found by scanning)?", log)
    if c:
        row.update(declared=int(c.group(1)), rounds=int(c.group(2)), fetched=int(c.group(3)),
                   already=int(c.group(4)), unreachable=int(c.group(5)),
                   spec_found=int(c.group(6) or 0), spec_tried=int(c.group(7) or 0))
    d = re.search(r"\d+ script\(s\) -> \S+\s+\[(.+?)\]", log)
    if d:
        row["found_by"] = {k: int(v) for v, k in re.findall(r"(\d+) ([\w-]+)", d.group(1))}
    u = re.search(r"! (\d+) file\(s\) could not be scanned", log)
    row["unscanned"] = int(u.group(1)) if u else 0
    row["capped"] = bool(re.search(r"! stopped at \d+ chunks", log))
    row["warnings"] = sorted({ln.strip()[:160] for ln in log.splitlines() if ln.strip().startswith("!")})
    return row


def entry_of(corpus):
    """Which entry script the page named, so a site serving different pages to different runs shows."""
    try:
        docs = [f for f in os.listdir(corpus) if f.endswith(".html")]
        html = open(os.path.join(corpus, docs[0]), errors="replace").read() if docs else ""
    except OSError:
        return None
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    return {"page_bytes": len(html), "first_script": srcs[0][-80:] if srcs else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--only", help="comma-separated names or hostnames to run")
    ap.add_argument("--timeout", type=int, default=1800, help="per target, seconds")
    a = ap.parse_args()
    urls = [ln.strip() for ln in open(a.targets) if ln.strip() and not ln.startswith("#")]
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        urls = [u for u in urls if name_of(u) in keep or urlsplit(u).hostname in keep
                or (urlsplit(u).hostname or "").removeprefix("www.") in keep]
    os.makedirs(OUT, exist_ok=True)
    out_path = os.path.join(OUT, f"live-{a.tag}.jsonl")
    rows = []
    for i, url in enumerate(urls, 1):
        name = name_of(url)
        dest = os.path.join(CORPORA, name)
        if os.path.isdir(dest):
            subprocess.run(["rm", "-rf", "--", dest], check=False)
        os.makedirs(dest, exist_ok=True)
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, "-m", "jshound.cli", url, "-o", dest, "-v"],
                               capture_output=True, text=True, timeout=a.timeout, cwd=REPO)
            log, rc = ANSI.sub("", r.stderr), r.returncode
        except subprocess.TimeoutExpired as e:
            log, rc = ANSI.sub("", (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes)
                               else (e.stderr or "")), "timeout"
        secs = round(time.time() - t0, 1)
        open(os.path.join(dest, "run.log"), "w").write(log)
        row = {"name": name, "url": url, "rc": rc, "seconds": secs, **parse(log),
               "entry": entry_of(os.path.join(dest, "js"))}
        rows.append(row)
        with open(out_path, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{i}/{len(urls)}] {name:28s} {str(row.get('status')):5s} files {row.get('files', 0):5d}  "
              f"endpoints {row.get('endpoints', 0):5d}  declared {row.get('declared', 0):5d}  "
              f"unreachable {row.get('unreachable', 0):4d}  unscanned {row.get('unscanned', 0):3d}  "
              f"{secs:7.1f}s" + (f"  BLOCKED {row['blocked']}" if row.get("blocked") else ""), flush=True)
    print(f"\n{len(rows)} targets -> {out_path}")


if __name__ == "__main__":
    main()
