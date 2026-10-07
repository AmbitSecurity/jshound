#!/usr/bin/env python3
"""precision.py — find endpoints the extractor INVENTED.

    jshound precision ./bodies records.json

`residual` answers "what did we miss". This answers the opposite question, and nothing else in this
tool could: residual is structurally blind to a fabricated endpoint, because an invention is not a
literal that went unclaimed — it is a claim with no literal behind it. An extractor can score a
perfect zero on residual while reporting URLs that do not exist.

That is not hypothetical. Three fabrications were found this way, each from the same root — a record
that looks like a result but is not one:

  * a tRPC chain built a path out of minified identifiers, so Grafana reported 13 endpoints at
    /api/trpc/hK.endpoints.<name> while the string "/api/trpc" appears nowhere in its bundle;
  * moment's locale table put "MM/DD/YYYY" behind a key named L, and L.get(...) reached a
    client-verb sink, so Notion reported an endpoint whose URL was a date format;
  * requests with a resolved body and an unresolved URL were counted as endpoints, so published
    counts disagreed with what exported.

A fabricated endpoint is worse than a missed one. A miss costs coverage; an invention sends someone
at a URL that was never there, and in the output it is indistinguishable from a real finding.

WHAT IT CHECKS. Each reported path is read back against the corpus it came from:

    unencoded-space  the path holds a raw space outside a slot, so it is not a URL at all. This is
                     the one class `not-in-corpus` is blind to: the offending text IS in the corpus,
                     it was just composed into somewhere it never belonged.

    not-in-corpus    no substantial fragment of this path appears anywhere in the corpus. That is
                     what an identifier chain looks like; a real path built from a base and a suffix
                     still has at least one of its parts written down somewhere.
    date-format      the whole path is a date/time format string.

Two other tests were tried and removed rather than shipped, because a checker nobody trusts is worse
than no checker. Requiring a path's route root to appear verbatim flagged 44% of tldraw's endpoints
and caught real tRPC alongside fake. Flagging short path segments called `/api/app/user_id/init`
questionable because `app` is three characters, and tldraw's `c/{id}/d` sync routes along with it —
while never having caught either fabrication that motivated this file.

Every finding is a question, not a verdict. A path built legitimately at runtime — from a config
value, a template with a resolved base — may not appear literally anywhere, and
`/apis/plugins.grafana.app/...` looks like a member chain while being a real Kubernetes API group.
Read them; do not count them.
"""
import argparse
import json
import os
import re
import sys

_MINIFIED = re.compile(r"^[a-zA-Z$_][a-zA-Z0-9$_]{0,2}$")
_DATE = re.compile(r"^[YMDHhmsSAaZzWwEeQqkKgGtTlLoOxX:,./\- \[\]]+$")
_DATE_TOKEN = re.compile(r"(YYYY|YY|MM|DD|HH|hh|mm|ss|SSS)")
# Segments that are short but are real: versions, well-known two-letter resources.
_OK_SHORT = {"v1", "v2", "v3", "v4", "v5", "me", "id", "ui", "io", "ai", "db", "ws", "qa",
             "eu", "us", "js", "css", "api", "rpc", "cdn", "sso", "otp", "pk", "ok"}


def _corpus_text(bodies, cap=400_000_000):
    """Every byte of JavaScript in the corpus, concatenated once."""
    chunks, total = [], 0
    for root, _d, files in os.walk(bodies):
        for f in sorted(files):
            if not f.endswith((".js", ".mjs", ".cjs", ".html", ".ts")):
                continue
            try:
                s = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            chunks.append(s)
            total += len(s)
            if total > cap:
                break
    return "".join(chunks)


def _prefix(path):
    """The first one or two segments, which a real route almost always writes down somewhere.

    Checking individual fragments is too lenient: Grafana's fabricated
    /api/trpc/hK.endpoints.upsertRuleGroupForNamespace is built from identifiers that DO appear in
    the bundle, so fragment-wise every part of it is "present". What is absent is the route root —
    the string "/api/trpc" occurs nowhere in Grafana, because Grafana does not serve tRPC. A client
    that talks to a prefix has to name that prefix.
    """
    segs = [s for s in path.split("/") if s and not s.startswith("{")]
    if not segs:
        return None
    pref = "/" + "/".join(segs[:2])
    return pref if len(pref) >= 5 else None


_SLOT = re.compile(r"\{[^{}]*\}")


def check(bodies, records_path, verbose=False):
    records = json.load(open(records_path))
    eps = [r for r in records if (r.get("_kind") or "endpoint") == "endpoint"]
    text = _corpus_text(bodies)
    findings = {"not-in-corpus": [], "date-format": [], "unencoded-space": []}

    for r in eps:
        t = str(r.get("template") or r.get("url") or "")
        if not t:
            continue
        bare = re.sub(r"https?://[^/]+", "", t)
        if _DATE.match(t) and _DATE_TOKEN.search(t):
            findings["date-format"].append((t, r.get("sink")))
            continue
        # A RAW SPACE CANNOT BE IN A URL — and `not-in-corpus` below cannot see this one, because the
        # offending text IS in the corpus. Grafana defines `{type: "mixed", uid: "-- Mixed --"}` in one
        # object literal; the extractor's property index filled every `${x.uid}` slot with it and
        # produced 20 addresses like `/datasources/uid/-- Mixed --/health`. Every fragment of that path
        # is written down somewhere, so the fragment test passes it, and the checker reported 4
        # questionable while 20 fabrications sat in the output. The composition was invented, not the
        # characters. Tested on the skeleton so a slot name can never trip it.
        if re.search(r"\s", _SLOT.sub("", bare)):
            findings["unencoded-space"].append((t, r.get("sink")))
            continue
        if bare and bare in text:          # written down verbatim — it is real
            continue
        # THE PREFIX RULE WAS TOO AGGRESSIVE TO SHIP. Requiring a path's route root to appear
        # verbatim flagged 44% of tldraw's endpoints and also flagged REAL tRPC, because a client
        # that assembles a path from a base and a suffix never writes the join down. A checker at
        # that false-positive rate teaches people to ignore it, which is worse than not having one.
        #
        # So the surviving test is narrow: a path none of whose substantial fragments appear
        # anywhere. That is what an identifier chain looks like and what a built-from-parts real
        # path does not, because at least one of its parts is a literal somewhere.
        frags = [x for x in re.split(r"[/{}]", bare) if len(x) >= 5 and not _MINIFIED.match(x)]
        if frags and not any(f in text for f in frags):
            findings["not-in-corpus"].append((t, r.get("sink")))

    total = sum(len(v) for v in findings.values())
    print(f"  endpoints checked   : {len(eps)}")
    print(f"  corpus bytes read   : {len(text):,}")
    for k, v in findings.items():
        print(f"  {k:<20}: {len(v)}")
    print(f"  >>> questionable    : {total}  ({(100 * total / len(eps)) if eps else 0:.1f}%)")
    if total and (verbose or total <= 12):
        print("\n  read these — a finding is a question, not a verdict:")
        for k, v in findings.items():
            for t, sink in v[:10]:
                print(f"    [{k}] {sink}  {t}")
    return {"endpoints": len(eps), "findings": {k: len(v) for k, v in findings.items()},
            "questionable": total}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound precision")
    ap.add_argument("bodies")
    ap.add_argument("records")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    check(a.bodies, a.records, a.verbose)


if __name__ == "__main__":
    main()
