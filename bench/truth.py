#!/usr/bin/env python3
"""truth.py: score a tool against the application's OWN published API spec.

    python bench/truth.py grafana-spec.json --tool jshound=bm.json --tool linkfinder=lf.json

Comparing extractors to each other only ever answers "who printed more lines". Several large
open-source applications publish an OpenAPI/Swagger document describing their real API, Grafana
ships `public/api-merged.json` (207 paths, 314 operations), Gitea ships
`templates/swagger/v1_json.tmpl`, and a self-hosted instance of one of those turns the comparison
into arithmetic:

    precision   of what the tool reported, how much is a real endpoint
    recall      of what the application declares, how much did the tool find
    undocumented  real-looking API paths the tool found that the spec does NOT contain

A SPEC IS A RULER, NOT A PRIZE. Recovering an endpoint somebody already documented is worth nothing
to anyone doing reconnaissance, they could read the docs. The spec is here because it is the only
way to prove the output is not invented: that the paths exist and, uniquely, that the VERBS are
right. No count can establish that, which is why every tool in this category is compared on counts.

So the number to read last is recall and the number to read first is `undocumented`. On Grafana the
published spec holds 206 paths while the shipped JavaScript yields 248 further `/api/*` paths that
are in no spec at all, that gap is the product. Recall also carries a caveat worth stating: a spec
covers the whole API while a web client only calls the part it needs, so no tool reading JavaScript
can reach 100%.

Path templates are compared structurally: `/roles/{roleUID}` and `/roles/{param1}` are the same
endpoint, so every placeholder collapses to `{}` before matching.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.export.paths import looks_like_source_file, normalize   # noqa: E402

_PH = re.compile(r"\{[^{}]*\}")
# Deliberately narrower than the display buckets in classify.py. `/admin` and `/auth` are UI routes
# at least as often as API ones, Grafana's `/admin/authentication/ldap` is a settings PAGE, and
# counting those as undiscovered API surface is how an impressive number stops being true.
_API_SHAPED = re.compile(r"^/(api|graphql|gql|rest|rpc|_api)\b"
                         r"|^/v\d+/"
                         r"|^/[a-z0-9-]+/(v\d+|api)\b", re.I)


def canon(p, base=""):
    """A path in the one form both sides can be compared in, or None if it is not a path at all.

    Routed through the exporters' own `normalize` rather than a second implementation: when this file
    had its own idea of what a path is, a fix to the shared one left the scoreboard still counting
    `/api/-- Mixed --/snapshot` as a discovered endpoint. One definition, or the numbers drift apart.
    """
    p = (p or "").split("?", 1)[0]
    p = re.sub(r"/\./", "/", p)
    if not p:
        return None
    p = normalize(p)[0]
    if looks_like_source_file(p):
        return None
    p = p.rstrip("/") or "/"
    if base and not p.startswith(base.rstrip("/") + "/"):
        p = base.rstrip("/") + p
    p = _PH.sub("{}", p)
    p = re.sub(r"/+", "/", p)
    # `/api`, `/api/*` and bare `/` are not endpoints; they are what is left when nothing resolved.
    return p if p not in ("/", base.rstrip("/"), base.rstrip("/") + "/*") else None


# ONLY THE SPEC IS RELATIVE TO basePath. A swagger document lists `/access-control/roles` and declares
# basePath `/api`; a tool reads `/api/access-control/roles` straight out of the bundle, already
# absolute. Prefixing both sides turned every non-API path the tool found into a fake API path,
# `/.git` was scored as `/api/.git` and counted as undocumented surface. Spec side only.
def canon_tool(p):
    return canon(p, "")


def load_spec(path):
    d = json.load(open(path))
    base = d.get("basePath") or ""
    if d.get("servers"):
        from urllib.parse import urlsplit
        base = urlsplit(d["servers"][0].get("url", "")).path or ""
    out = {}
    for p, item in (d.get("paths") or {}).items():
        methods = {m.upper() for m in item
                   if m.lower() in ("get", "post", "put", "patch", "delete", "head", "options")}
        c = canon(p, base)          # spec paths ARE relative to basePath
        if c:
            out.setdefault(c, set()).update(methods)
    return out, base


def load_tool(path, base=""):   # noqa: ARG001 - base intentionally unused; see canon_tool
    """Accepts a jshound records.json, an OpenAPI doc, or a plain list/lines of paths."""
    raw = open(path).read().strip()
    try:
        d = json.loads(raw)
    except ValueError:
        return {c: set() for c in (canon_tool(x) for x in raw.splitlines() if x.strip()) if c}
    if isinstance(d, dict) and "paths" in d:
        return {c: {m.upper() for m in item} for c, item in
                ((canon_tool(p), it) for p, it in d["paths"].items()) if c}
    if isinstance(d, dict):
        d = d.get("paths") or list(d.values())[0]
    out = {}
    for r in d:
        if isinstance(r, str):
            c = canon_tool(r)
            if c:
                out.setdefault(c, set())
        elif isinstance(r, dict):
            c = canon_tool(r.get("template") or r.get("url") or r.get("path"))
            if c:
                m = (r.get("method") or "").upper()
                out.setdefault(c, set()).update({m} if m and m != "UNKNOWN" else set())
    return out


def score(spec, tool, base=""):
    """A client routinely stores the API prefix once and calls the rest relative to it,
    `backendSrv.get("/access-control/roles")` with `/api` prepended at request time, so the bundle
    holds the unprefixed form while the spec declares the prefixed one. Neither side is wrong, and
    insisting on one spelling scored 114 real matches as misses. A tool path counts as a hit if
    either spelling is declared; `undocumented` requires BOTH to be absent, so the prefix ambiguity
    can never manufacture a discovery.
    """
    sp, tp = set(spec), set(tool)
    b = base.rstrip("/")

    def forms(p):
        out = {p}
        if b and not p.startswith(b + "/"):
            out.add(b + p)
        if b and p.startswith(b + "/"):
            out.add(p[len(b):])
        return out

    hit = {p for p in tp if forms(p) & sp}
    covered = {q for p in hit for q in forms(p)} & sp
    precision = len(hit) / len(tp) if tp else 0.0
    recall = len(covered) / len(sp) if sp else 0.0
    # Of the endpoints it DID find, how often is the verb right? Only a tool that reports a method
    # can score here at all, which is itself the finding.
    checked = [p for p in hit if tool[p]]
    right = sum(1 for p in checked
                if tool[p] & set().union(*[spec[q] for q in forms(p) if q in spec]))
    # Found, API-shaped, and in no spec: the part a reader cannot get from documentation.
    undoc = [p for p in tp if not (forms(p) & sp) and _API_SHAPED.match(p)]
    return {"reported": len(tp), "declared": len(sp), "hits": len(covered),
            "undocumented": len(undoc),
            "undocumented_sample": sorted(undoc)[:10],
            "precision": precision, "recall": recall,
            "method_checked": len(checked),
            "method_correct": right,
            "method_accuracy": (right / len(checked)) if checked else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--tool", action="append", required=True, metavar="NAME=FILE")
    ap.add_argument("--json")
    a = ap.parse_args()

    spec, base = load_spec(a.spec)
    print(f"reference: {len(spec)} paths, "
          f"{sum(len(v) for v in spec.values())} operations, basePath={base or '/'}\n")

    rows, found_by_any = {}, set()
    for t in a.tool:
        name, _, path = t.partition("=")
        tool = load_tool(path, base)
        rows[name] = (tool, score(spec, tool, base))
        found_by_any |= set(tool) & set(spec)

    hdr = (f"{'tool':<14}{'reported':>9}{'hits':>6}{'precision':>11}{'recall':>8}"
           f"{'verb ok':>9}{'UNDOCUMENTED':>14}")
    print(hdr); print("-" * len(hdr))
    for name, (tool, s) in rows.items():
        ma = "n/a" if s["method_accuracy"] is None else f"{s['method_accuracy']:.0%}"
        print(f"{name:<14}{s['reported']:>9}{s['hits']:>6}{s['precision']:>10.0%}"
              f"{s['recall']:>8.0%}{ma:>9}{s['undocumented']:>14}")
    for name, (_t, s) in rows.items():
        if s["undocumented_sample"]:
            print(f"\n{name}, API paths in NO spec (sample):")
            for p in s["undocumented_sample"][:6]:
                print(f"    {p}")

    print(f"\nprecision = of what the tool reported, the share that is a real declared endpoint.")
    print(f"recall    = of the {len(spec)} paths the application declares, the share found.")
    print(f"UNDOCUMENTED = API-shaped paths found that the spec does not contain. Read this one first:")
    print(f"            it is the surface a reader cannot get from the documentation.")
    print(f"verb ok   = of its hits, how often the HTTP method is right. A tool that reports no")
    print(f"            method cannot score, which is the point.")
    if a.json:
        json.dump({k: v[1] for k, v in rows.items()}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
