"""cli.py — one entry point over the same calls the library exposes.

    jshound analyze ./bodies --host app.example.com -f openapi -o spec.json
    jshound chunks main.js
    jshound modules ./out main.js
    jshound routes https://app.example.com
    jshound from-har session.har -o ./bodies
    jshound sourcemaps ./bodies --scope app.example.com
    jshound fingerprint ./bodies
    jshound graphql ./bodies --summary
    jshound precision ./bodies records.json
    jshound residual ./bodies records.json
    jshound export records.json -f har -o traffic.har

`analyze`, `chunks`, `modules`, `export` and `residual` make no network requests. `routes` and
`sourcemaps` do, and say so in their help — enumerating an App Router app means fetching the pages
that name the next round of chunks, and a sourcemap has to be asked for.
"""
import argparse
import json
import os
import sys

from . import __version__


def _emit(data, out, text=False):
    payload = data if text else json.dumps(data, indent=2)
    if out:
        with open(out, "w") as fh:
            fh.write(payload + ("\n" if text else ""))
        print(f"-> {out}", file=sys.stderr)
    else:
        print(payload)


def _format(records, fmt, title):
    from .export import har, openapi, urls
    if fmt == "openapi":
        return openapi.build(records, title=title), False
    if fmt == "har":
        return har.build(records, title=title), False
    if fmt == "urls":
        return "\n".join(urls.build(records, with_method=True)), True
    return records, False                                    # jsonl/raw


def cmd_analyze(a):
    from .analyze import analyze as run
    records, paths = run(a.bodies, a.host, a.manifest)
    print(f"{len(paths)} scripts -> {len(records)} records", file=sys.stderr)
    data, text = _format(records, a.format, a.title or a.host or "Recovered API")
    _emit(data, a.out, text)


def cmd_export(a):
    records = json.load(open(a.records))
    data, text = _format(records, a.format, a.title or "Recovered API")
    _emit(data, a.out, text)


def cmd_chunks(a):
    from . import chunk_map
    src = sys.stdin.read() if a.file in ("-", None) else open(a.file, encoding="utf-8",
                                                              errors="replace").read()
    res = chunk_map(src)
    if a.quiet:
        _emit("\n".join(res.get("chunks") or []), a.out, text=True)
    else:
        _emit(res, a.out)


def cmd_modules(a):
    from .modules import unbundle
    _emit(unbundle(a.outdir, a.files), a.out)


def cmd_routes(a):
    from .approutes import main as run
    sys.argv = ["approutes", a.url] + (["--out", a.out] if a.out else [])
    run()


def cmd_manifest(a):
    """Whichever framework declared its routes. Next, Nuxt and SvelteKit each ship their route table,
    under different names and in different formats, so all three are tried and the reasons are
    reported when none does — "no manifest" is a fact about the target worth stating precisely."""
    from .nextmanifest import read as read_next
    from .nuxtmanifest import read as read_nuxt
    from .remixmanifest import read as read_remix
    from .sveltekitmanifest import read as read_sveltekit

    reasons = []
    for label, read in (("next", read_next), ("nuxt", read_nuxt),
                        ("sveltekit", read_sveltekit), ("remix", read_remix)):
        d = read(a.url)
        if not d.get("available"):
            reasons.append(f"{label}: {d.get('reason')}")
            continue
        if label == "next":
            pages, api = d.get("routes", []), d.get("api_routes", [])
            print(f"  next, declared by the build: {len(pages) + len(api)} routes "
                  f"({len(api)} api, {len(pages)} page) · {len(d.get('chunks', []))} chunks · "
                  f"{len(d.get('rewrites', []))} rewrites · {len(d.get('ssg', []))} prerendered",
                  file=sys.stderr)
            out = [r["template"] for r in api] + [r["template"] for r in pages]
        elif label == "remix":
            acts = [r for r in d["routes"] if r.get("has_action")]
            print(f"  {d.get('flavour')}, declared by the build: {len(d['routes'])} routes · "
                  f"{len(d.get('data_endpoints', []))} data endpoints ({d.get('data_protocol')}) · "
                  f"{len(acts)} accept a submission · {len(d.get('chunks', []))} chunks",
                  file=sys.stderr)
            out = sorted({r["template"] for r in d["routes"]}) + [
                (f"{e['template']}?{e['query']}" if e.get("query") else e["template"])
                for e in d.get("data_endpoints", [])]
        elif label == "sveltekit":
            print(f"  sveltekit, declared by the build: {len(d['routes'])} routes · "
                  f"{len(d['data_endpoints'])} __data.json endpoints · "
                  f"{len(d['matchers'])} matchers", file=sys.stderr)
            out = [r["template"] for r in d["routes"]] + d["data_endpoints"]
        else:
            print(f"  nuxt, declared by the build: {len(d['routes'])} prerendered routes + "
                  f"{len(d['rule_routes'])} from route rules · {len(d['route_rules'])} rules total",
                  file=sys.stderr)
            out = d["routes"] + d["rule_routes"]
        for r in out:
            print(r)
        if a.json:
            json.dump(d, open(a.json, "w"), indent=1)
        return 0

    print("  no framework manifest:", file=sys.stderr)
    for r in reasons:
        print(f"    {r}", file=sys.stderr)
    return 1


def cmd_fetch(a):
    from .fetch import collect
    collect(a.url, a.out, not a.no_chunks, a.max_chunks)


def cmd_from_har(a):
    from .fromhar import convert
    convert(a.har, a.out)


def cmd_sourcemaps(a):
    from .sourcemaps import harvest
    hosts = [h.strip().lower() for h in (a.scope or "").split(",") if h.strip()]
    harvest(a.bodies, hosts, a.dry_run, a.max_sources)


def cmd_fingerprint(a):
    from .fingerprint import scan
    _emit(scan(a.bodies), a.out)


def cmd_graphql(a):
    from .graphqlops import scan, to_har
    ops = scan(a.bodies)
    if a.har:
        _emit(to_har(ops, a.har), a.out)
        print(f"{len(ops)} operation(s) -> HAR for {a.har}", file=sys.stderr)
        return
    if a.summary:
        for r in ops:
            v = ", ".join(f"${x['name']}: {x['type']}" for x in r["variables"])
            print(f"{r['operation_type']:<13}{r['operation']:<42}({v})")
    else:
        _emit(ops, a.out)
    print(f"{len(ops)} operation(s)", file=sys.stderr)


def cmd_precision(a):
    from .precision import check
    check(a.bodies, a.records, a.verbose)


def cmd_residual(a):
    from .residual import classify
    classify(a.bodies, a.records, verbose=a.verbose)


def build_parser():
    p = argparse.ArgumentParser(prog="jshound",
                                description="Recover a SPA's API contracts from its shipped JavaScript.")
    p.add_argument("--version", action="version", version=f"jshound {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    fmt = dict(choices=["openapi", "har", "urls", "jsonl"], default="jsonl",
               help="openapi=Burp/Caido contract import, har=Site Map import, urls=nuclei/ffuf")

    s = sub.add_parser("analyze", help="directory of saved JS/HTML -> contracts (no network)")
    s.add_argument("bodies")
    s.add_argument("--host", help="attribute relative paths to this host")
    s.add_argument("--manifest", help="a crawl .jsonl, to resolve relative chunk paths to URLs")
    s.add_argument("--title")
    s.add_argument("-f", "--format", **fmt)
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("export", help="re-format a saved records.json")
    s.add_argument("records")
    s.add_argument("--title")
    s.add_argument("-f", "--format", **fmt)
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("chunks", help="lazy-chunk URLs, by evaluating webpack's own builder")
    s.add_argument("file", nargs="?", default="-")
    s.add_argument("-q", "--quiet", action="store_true", help="just the URLs, one per line")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_chunks)

    s = sub.add_parser("modules", help="webpack bundle -> readable module tree")
    s.add_argument("outdir")
    s.add_argument("files", nargs="+")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_modules)

    s = sub.add_parser("routes", help="Next.js App Router enumeration (FETCHES)")
    s.add_argument("url")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_routes)

    s = sub.add_parser("manifest", help="Next's served build manifest -> DECLARED routes (FETCHES)")
    s.add_argument("url")
    s.add_argument("--json")
    s.set_defaults(fn=cmd_manifest)

    s = sub.add_parser("fetch", help="collect a page's JavaScript (FETCHES)")
    s.add_argument("url")
    s.add_argument("-o", "--out", default="./bodies")
    s.add_argument("--no-chunks", action="store_true")
    s.add_argument("--max-chunks", type=int, default=400)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("from-har", help="HAR export (DevTools/Burp/Caido) -> bodies dir (no network)")
    s.add_argument("har")
    s.add_argument("-o", "--out", default="./bodies")
    s.set_defaults(fn=cmd_from_har)

    s = sub.add_parser("sourcemaps", help="fetch .map beside recorded scripts (FETCHES)")
    s.add_argument("bodies")
    s.add_argument("--scope", help="comma-separated hosts to fetch from")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max-sources", type=int, default=4000)
    s.set_defaults(fn=cmd_sourcemaps)

    s = sub.add_parser("fingerprint", help="what built this app, and what to expect from it")
    s.add_argument("bodies")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_fingerprint)

    s = sub.add_parser("graphql", help="GraphQL operations the client ships (no network)")
    s.add_argument("bodies")
    s.add_argument("--summary", action="store_true", help="one line per operation")
    s.add_argument("--har", metavar="ENDPOINT",
                   help="emit HAR of replayable POSTs to this GraphQL endpoint")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_graphql)

    s = sub.add_parser("precision", help="find endpoints the extractor INVENTED")
    s.add_argument("bodies")
    s.add_argument("records")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_precision)

    s = sub.add_parser("residual", help="measure what the extractor MISSED")
    s.add_argument("bodies")
    s.add_argument("records")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_residual)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    os.environ.setdefault("NODE_PATH", os.environ.get("NODE_PATH", "/usr/local/lib/node_modules"))
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
