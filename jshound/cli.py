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


def _format(records, fmt, title, include_unresolved=False):
    from .export import har, openapi, table, urls
    if fmt == "table":
        return table.build(records), True
    if fmt == "openapi":
        return openapi.build(records, title=title), False
    if fmt == "har":
        return har.build(records, title=title), False
    if fmt == "urls":
        # An endpoint whose base did not resolve has no URL, so it is not in a URL list by default.
        # It is still real surface, and this is how you ask to see it with the slot left visible.
        return "\n".join(urls.build(records, with_method=True,
                                     include_unresolved=include_unresolved)), True
    if fmt == "jsonl":
        return "\n".join(json.dumps(r) for r in records), True
    return records, False                                    # json/raw


def _targets(a):
    """The hosts to map: arguments, or one per line on stdin.

    Every tool in this space takes a list, because a scope is a list. `-` or an empty argument list
    with something piped in reads stdin, so `cat scope.txt | jshound` works.
    """
    given = list(a.targets or [])
    if "-" in given or (not given and not sys.stdin.isatty()):
        given = [t for t in given if t != "-"]
        given += [ln.strip() for ln in sys.stdin if ln.strip() and not ln.startswith("#")]
    out, seen = [], set()
    for t in given:
        t = t if "://" in t else "https://" + t
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _hosts_fetched(bodies):
    """{host: distinct URLs saved}, from the origin map the fetcher wrote."""
    import json as _json
    from collections import Counter
    from urllib.parse import urlsplit
    seen, c = set(), Counter()
    try:
        for ln in open(os.path.join(bodies, "_origin.jsonl")):
            u = (_json.loads(ln).get("u") or "")
            if u and u not in seen:
                seen.add(u)
                c[urlsplit(u).netloc] += 1
    except (OSError, ValueError):
        pass
    return c


def cmd_map(a):
    """Fetch and read an application, in one command.

    `fetch` then `analyze` was always the real workflow, and making someone wire two commands
    together before seeing a single endpoint was a bad first thirty seconds. This is the default:
    `jshound app.example.com` collects the JavaScript and prints the requests.

    ONE LINE PER TARGET, and the status code is on it. This printed twelve lines of analysis log per
    target and never the one fact a user checks first. tesla.com answered 403 Access Denied, and the
    summary explained that a server-rendered page can legitimately have no API, which is the wrong
    story for a refusal. The log is still there with `-v`; warnings are always shown.

    Each target gets its own corpus under `.jshound/<host>`, kept rather than discarded, because
    collecting it is the slow part and every other command in here reads a directory of it.
    """
    import contextlib
    import io
    from urllib.parse import urlsplit

    from . import fetch as _fetch
    from .analyze import analyze as run
    from .fetch import collect

    targets = _targets(a)
    if not targets:
        print("map: no targets. Pass a hostname, or pipe a list of them in.", file=sys.stderr)
        return 1
    if a.bodies and len(targets) > 1:
        print("map: --bodies names one directory, so it cannot hold several targets. Drop it and "
              "each goes to .jshound/<host>.", file=sys.stderr)
        return 1
    _fetch.set_proxy(a.proxy or os.environ.get("JSHOUND_PROXY"), a.proxy_ca)
    _fetch.set_client(a.client)

    # THE REQUESTS, AS THEY HAPPEN: status, then URL. What a person checks first is what was asked
    # for and what came back, and the analysis log that used to fill this space answered neither.
    # Written to the REAL stderr, held from before the quiet capture below, or quiet would swallow it.
    import threading
    real_err = sys.stderr
    lock = threading.Lock()
    tty = real_err.isatty()

    def colour(code):
        if not tty:
            return f"{code}"
        c = "31" if code == "ERR" else {"2": "32", "3": "36", "4": "33", "5": "31"}.get(code[0], "0")
        return f"\033[{c}m{code}\033[0m"

    def on_request(url, status, nbytes):
        if a.quiet:
            return
        code = str(status) if status is not None else "ERR"
        size = (f"{nbytes / 1048576:.1f}M" if nbytes >= 1048576 else
                f"{nbytes // 1024}K" if nbytes >= 1024 else f"{nbytes}B") if nbytes else ""
        with lock:
            real_err.write(f"  {colour(code):>3}  {size:>6}  {url}\n")
            real_err.flush()

    _fetch.ON_REQUEST = on_request

    worst, all_records = 0, []
    width = max(len(urlsplit(t).netloc) for t in targets)
    for target in targets:
        host = urlsplit(target).hostname or target
        bodies = a.bodies or os.path.join(".jshound", host)
        label = urlsplit(target).netloc.ljust(width)
        log = io.StringIO()
        quiet = contextlib.nullcontext() if a.verbose else contextlib.redirect_stderr(log)

        rep = {}
        with quiet:
            saved = collect(target, bodies, with_chunks=not a.no_chunks, max_chunks=a.max_chunks,
                            timeout=a.timeout, allow_private=a.allow_private, report=rep)
        status = rep.get("status")
        code = str(status) if status is not None else "---"

        if rep.get("blocked"):
            tried = rep.get("tried") or []
            print(f"{label}  {code}  blocked by {rep['blocked']}"
                  + (f", tried {len(tried)} client profiles" if len(tried) > 1 else ""),
                  file=sys.stderr)
            print(f"{' ' * width}       A browser gets past this where an HTTP client cannot. Load "
                  f"the page, export a HAR, then `jshound from-har session.har`.", file=sys.stderr)
            worst = 1
            continue
        if not saved:
            why = "unreachable" if status is None else f"no scripts in a {rep.get('bytes', 0)} byte page"
            print(f"{label}  {code}  {why}", file=sys.stderr)
            worst = 1
            continue

        # NOT `a.host or host`. Passing the target's own hostname makes `analyze` filter to records
        # served BY that host, and a build's assets usually are not: supabase.com/dashboard serves
        # 209 of its 217 files from frontend-assets.supabase.com, so filtering to the document host
        # threw away 59 of 224 endpoints, all of them the app's own code.
        with quiet:
            records, paths = run(bodies, a.host, None)
        requests = [r for r in records if (r.get("_kind") or "endpoint") in ("endpoint", "route")]

        hosts = _hosts_fetched(bodies)
        own = hosts.pop(urlsplit(target).netloc, 0)
        where = f"{sum(hosts.values()) + own} files"
        if hosts:
            top = ", ".join(f"{n} from {h}" for h, n in hosts.most_common(2))
            where += f" ({top})"
        prof = rep.get("profile")
        note = f"  [as {prof}]" if prof and prof != _fetch.PROFILES[0] and not a.client else ""
        print(f"{label}  {code}  {where}  ->  {len(requests)} endpoints{note}", file=sys.stderr)
        # A CAP THAT IS HIT IS A HOLE IN THE MAP, so it gets a line of its own. At the old 400,
        # claude.ai stopped at 43% of the chunks it declares and nothing on screen said so.
        if rep.get("chunks_capped"):
            print(f"{' ' * width}       ! stopped at --max-chunks {a.max_chunks}: "
                  f"{rep.get('chunks_fetched', 0)} of {rep.get('chunks_declared', 0)} declared chunks "
                  f"fetched. Raise it to read the rest.", file=sys.stderr)
        elif rep.get("chunks_unreachable"):
            print(f"{' ' * width}       {rep['chunks_unreachable']} declared chunk(s) could not be "
                  f"fetched", file=sys.stderr)

        # Warnings are the part of the log a user must not miss, so they survive quiet mode.
        for ln in log.getvalue().splitlines():
            if ln.lstrip().startswith(("!", "!!")):
                print(f"{' ' * width}       {ln.strip()}", file=sys.stderr)
        if not requests:
            # A real 200 with scripts in it and nothing recovered. That is a real answer for a
            # server-rendered page or an app whose calls sit behind a login, and the wrong answer
            # if the app builds its script tags at runtime.
            print(f"{' ' * width}       no requests in the scripts this page loads. If the app "
                  f"builds its script tags at runtime, a HAR from a browser will see more.",
                  file=sys.stderr)
        # KEPT, not just printed. The table scrolls away and the records are what every other command
        # and every downstream tool reads, so both are written beside the corpus they came from.
        from .export import table as _table
        with open(os.path.join(bodies, "endpoints.txt"), "w") as fh:
            fh.write(_table.build(records) + "\n")
        with open(os.path.join(bodies, "records.json"), "w") as fh:
            json.dump(records, fh, indent=1)
        print(f"{' ' * width}       -> {os.path.join(bodies, 'endpoints.txt')}", file=sys.stderr)
        all_records.extend(records)

    _fetch.ON_REQUEST = None
    if not all_records:
        return worst or 1
    print(file=sys.stderr)
    data, text = _format(all_records, a.format, a.title or urlsplit(targets[0]).hostname,
                         getattr(a, 'include_unresolved', False))
    _emit(data, a.out, text)
    return worst


def cmd_analyze(a):
    from .analyze import analyze as run
    records, paths = run(a.bodies, a.host, a.manifest)
    print(f"{len(paths)} scripts -> {len(records)} records", file=sys.stderr)
    # A directory with nothing readable in it is not a host with no API, and `-o spec.json` on this
    # path wrote a valid, importable, EMPTY OpenAPI document whose description reassured the reader
    # about the 0 placeholders it contained.
    if not paths and not records:
        n = sum(len(f) for _r, _d, f in os.walk(a.bodies))
        print(f"analyze: nothing readable in {a.bodies!r} ({n} file(s) present, no .js/.mjs/.cjs/"
              f".html among them), run `jshound fetch <url> -o {a.bodies}` first", file=sys.stderr)
        return 1
    data, text = _format(records, a.format, a.title or a.host or "Recovered API",
                         getattr(a, 'include_unresolved', False))
    _emit(data, a.out, text)


def cmd_export(a):
    from . import read_records
    records = read_records(a.records)
    data, text = _format(records, a.format, a.title or "Recovered API",
                         getattr(a, 'include_unresolved', False))
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
            # A readable manifest that declares nothing is a fact about the BUILD, and a bare row of
            # zeros reads like the tool failed instead. nuxt.com served 900 prerendered routes and now
            # serves `"prerendered": []`, having moved to rendering on demand.
            if not d["routes"] and not d["rule_routes"]:
                print("  the manifest was read and declares nothing: this build prerenders no routes "
                      "and sets no route rules, so it renders on demand. Its routes are not written "
                      "down anywhere for this command to read, use `fetch` and `analyze` instead.",
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
    collect(a.url, a.out, not a.no_chunks, a.max_chunks, allow_private=a.allow_private)


def cmd_from_har(a):
    from .fromhar import convert
    convert(a.har, a.out)


def cmd_sourcemaps(a):
    from .sourcemaps import harvest
    hosts = [h.strip().lower() for h in (a.scope or "").split(",") if h.strip()]
    harvest(a.bodies, hosts, a.dry_run, a.max_sources)


def cmd_fingerprint(a):
    """A line a person can read, with the full object behind `--json`.

    This printed a 17-key JSON object and the README promised a one-line summary that was printed
    nowhere. A fingerprint answers one question, which is what to expect from this app, so the
    answer should fit on a line. `--json` and `-o` still give the object, because that is what a
    caller wants.
    """
    from .fingerprint import scan
    d = scan(a.bodies)
    if a.json or a.out:
        _emit(d, a.out)
        return
    bits = [f"bundler: {d.get('bundler') or 'none detected'}"]
    if d.get("framework"):
        bits.append(f"framework: {d['framework']} (from the {d.get('framework_basis', '?')})")
    if d.get("module_id_mode"):
        bits.append(f"module ids: {d['module_id_mode']}")
    bits.append(f"{d.get('js_parsed', 0)} of {d.get('files_scanned', 0)} files parsed")
    print("  " + " · ".join(bits))
    # Where the code is served from is the thing people get wrong about scope, so it goes on screen
    # rather than only into the JSON.
    for name, hosts in (d.get("bundler_hosts") or {}).items():
        for host, n in hosts:
            if host and host != d.get("document_host"):
                print(f"  {n} file(s) of it served from {host}, not from {d.get('document_host')}")
    if d.get("js_parse_failures"):
        print(f"  ! {d['js_parse_failures']} file(s) could not be parsed, so anything in them is "
              f"missing rather than absent")


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


PRIVATE_HELP = ("allow URLs the target names in private address space (loopback, RFC1918, "
                "link-local); on by default when the target itself is private")


def build_parser():
    p = argparse.ArgumentParser(prog="jshound",
                                description="Recover a SPA's API contracts from its shipped JavaScript.")
    p.add_argument("--version", action="version", version=f"jshound {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    p._jshound_commands = sub.choices       # read by main() for the bare-target shortcut

    # `jsonl` emitted a pretty-printed JSON ARRAY, which is not JSON Lines and broke `jq -c` per line
    # and `while read`. Rather than redefine what the default produces, the two shapes now have two
    # honest names: `json` is the array and stays the default, `jsonl` is one object per line.
    fmt = dict(choices=["openapi", "har", "urls", "table", "json", "jsonl"], default="json",
               help="table=the requests, to read; openapi=Burp/Caido contract import, har=Site Map "
                    "import, urls=nuclei/ffuf, json=records as an array (default), jsonl=one record "
                    "per line")

    s = sub.add_parser("map", help="fetch an app's JavaScript and read it, in one command (FETCHES)")
    s.add_argument("targets", nargs="*",
                   help="hostnames or URLs. `-`, or nothing with a list piped in, reads stdin")
    s.add_argument("-o", "--out", help="write the result here instead of stdout")
    # `table` here, `json` everywhere else: this command exists to be run by a person who wants to
    # see the requests, and the first thing it prints should be readable.
    s.add_argument("-f", "--format", **dict(fmt, default="table"))
    s.add_argument("--bodies", help="where to keep the JavaScript (default: .jshound/<host>)")
    s.add_argument("--host", help="keep only records served by this host, and attribute "
                                  "unknown origins to it. The default keeps every host the app "
                                  "loads its own code from")
    s.add_argument("--title", help="title for the OpenAPI/HAR document")
    s.add_argument("--no-chunks", action="store_true",
                   help="do not follow the lazy chunks the bundles declare")
    s.add_argument("--max-chunks", type=int, default=5000)
    s.add_argument("--timeout", type=int, default=30)
    s.add_argument("--allow-private", action="store_true", help=PRIVATE_HELP)
    s.add_argument("--include-unresolved", action="store_true",
                   help="in `-f urls`, also list endpoints whose base did not resolve, "
                        "with the slot left visible")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="also print the analysis log")
    s.add_argument("-q", "--quiet", action="store_true",
                   help="do not print each request as it is made")
    s.add_argument("--proxy", help="route every request through this proxy URL "
                                   "(or set JSHOUND_PROXY)")
    s.add_argument("--proxy-ca", help="CA certificate for a proxy that intercepts TLS")
    s.add_argument("--client", help="pin one browser profile instead of rotating on a block, "
                                    "e.g. safari17_0, firefox144, chrome142")
    s.set_defaults(fn=cmd_map)

    s = sub.add_parser("analyze", help="directory of saved JS/HTML -> contracts (no network)")
    s.add_argument("bodies")
    s.add_argument("--host", help="attribute relative paths to this host")
    s.add_argument("--manifest", help="a crawl .jsonl, to resolve relative chunk paths to URLs")
    s.add_argument("--title")
    s.add_argument("-f", "--format", **fmt)
    s.add_argument("-o", "--out")
    s.add_argument("--include-unresolved", action="store_true",
                   help="in `-f urls`, also list endpoints whose base did not resolve, "
                        "with the slot left visible")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("export", help="re-format a saved records.json")
    s.add_argument("records")
    s.add_argument("--title")
    s.add_argument("-f", "--format", **fmt)
    s.add_argument("-o", "--out")
    s.add_argument("--include-unresolved", action="store_true",
                   help="in `-f urls`, also list endpoints whose base did not resolve, "
                        "with the slot left visible")
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
    s.add_argument("--max-chunks", type=int, default=5000)
    s.add_argument("--allow-private", action="store_true", help=PRIVATE_HELP)
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
    s.add_argument("--json", action="store_true", help="the full object instead of a summary line")
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
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # `jshound tesla.com` means `jshound map tesla.com`. A subcommand is the common case for every
    # other verb here, but the common case for a first run is a hostname, and making that an
    # argparse error is a bad first thirty seconds.
    if argv and argv[0] not in parser._jshound_commands and not argv[0].startswith("-"):
        argv = ["map"] + argv
    elif not argv and sys.stdin.isatty():
        # `jshound` alone at a terminal printed "error: the following arguments are required: cmd",
        # which is a bad first thing to see. Show what it does instead.
        parser.print_help()
        return 0
    elif not argv:
        # `cat scope.txt | jshound`. With no argv at all the subparser is required and argparse
        # errors out before the line above can do anything, so a piped list needs its own case.
        argv = ["map"]
    a = parser.parse_args(argv)
    # NODE_PATH is deliberately NOT defaulted here. Setting it to `/usr/local/lib/node_modules`
    # shadowed every module's own fallback to the checkout's `node_modules`, which is where the Babel
    # packages actually are, and the only reason anything worked was node's upward directory walk from
    # `jshound/js/` — a walk that finds nothing once the package is installed non-editable.
    # A CLI reports what went wrong; it does not hand the user a traceback. These are the failures a
    # correct invocation can still hit — a path that is not there, a file that is not what it looks
    # like, a node helper that did not run — and each already carries a sentence saying which.
    try:
        return a.fn(a)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"{a.cmd}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{a.cmd}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
