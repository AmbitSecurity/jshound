"""cli.py: the whole command.

    jshound app.example.com                 fetch it, follow every lazy chunk, list the requests
    jshound a.example.com b.example.com     several at once
    cat scope.txt | jshound                 a list, one per line
    jshound .jshound/app.example.com/js     read what was already downloaded, without the network

It used to be fourteen subcommands for what is one job done from two starting points, and the first
thing a new reader had to get past. Everything they did is still in the library (`jshound.analyze`,
`jshound.chunk_map`, `jshound.unfetched`, the exporters), which is what pentest calls.
"""
import argparse
import json
import os
import sys
import time

from . import __version__
from .fetch import MAX_CHUNKS as _MAX_CHUNKS


def _hosts_fetched(corpus):
    """{host: distinct URLs saved}, from the origin map the fetcher wrote."""
    from collections import Counter
    from urllib.parse import urlsplit
    seen, c = set(), Counter()
    try:
        for ln in open(os.path.join(corpus, "_origin.jsonl")):
            u = json.loads(ln).get("u") or ""
            if u and u not in seen:
                seen.add(u)
                c[urlsplit(u).netloc] += 1
    except (OSError, ValueError):
        pass
    return c


def _graphql_operations(corpus):
    """Every GraphQL operation the client ships, with the page each file came from.

    The extractor ties an operation to a call site; the document scanner reads every operation in
    the bundle. railway.com: 1 against 966. A scanner failure costs its operations and is said, it
    does not cost the run.
    """
    from . import graphql_operations
    try:
        ops = graphql_operations(corpus)
    except Exception as e:                                 # noqa: BLE001
        print(f"  ! GraphQL scan failed: {str(e)[:160]}", file=sys.stderr)
        return []
    ops = ops if isinstance(ops, list) else (ops or {}).get("operations") or []
    page = {}
    try:
        for ln in open(os.path.join(corpus, "_origin.jsonl")):
            row = json.loads(ln)
            page.setdefault(row.get("f"), row)
    except (OSError, ValueError):
        pass
    from urllib.parse import urlsplit
    for op in ops:
        row = page.get(os.path.basename(op.get("_file") or "")) or {}
        if row.get("d"):
            op.setdefault("_page", row["d"])
        if row.get("u"):
            op.setdefault("_host", urlsplit(row["u"]).hostname)
    return ops


def _safe_name(name):
    return "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in name)[:120] or "target"


def _write_results(out_dir, records, title):
    """The requests, in every format, untruncated. The screen is for looking; these are for using.

    Only requests. The extractor's other record kinds (path literals, call-site arguments, validation
    schemas, encoders) are its working-out, about 90% of what it emits, and they stay inside the
    library, where pentest's ingest reads them. They were written here as `records.json` and made the
    output look like a debug dump.
    """
    from .export import openapi, table, urls
    from .export.requests import requests_only, to_lines
    os.makedirs(out_dir, exist_ok=True)
    reqs = requests_only(records)
    files = {
        "endpoints.txt": table.build(records) + "\n",
        "requests.jsonl": "".join(json.dumps(line) + "\n" for line in to_lines(records)),
        "urls.txt": "\n".join(urls.build(reqs, with_method=True)) + "\n",
        "openapi.json": json.dumps(openapi.build(reqs, title=title), indent=1),
    }
    stale = os.path.join(out_dir, "records.json")
    if os.path.exists(stale):
        os.remove(stale)                    # from a run before this output existed
    for name, body in files.items():
        with open(os.path.join(out_dir, name), "w") as fh:
            fh.write(body)
    where = os.path.join(out_dir, "endpoints.txt")
    rel = os.path.relpath(where)
    return rel if not rel.startswith("..") else where


def _inputs(a):
    """What to read, in order: arguments, or one per line on stdin.

    An input is whatever it looks like: a directory is a corpus already on disk, read without
    touching the network, and anything else is a hostname or a URL to fetch and then read.
    """
    given = list(a.targets or [])
    if "-" in given or (not given and not sys.stdin.isatty()):
        given = [t for t in given if t != "-"]
        given += [ln.strip() for ln in sys.stdin if ln.strip() and not ln.lstrip().startswith("#")]
    out, seen = [], set()
    for t in given:
        # A mistake gets a sentence, not a fetch. `/nonexistent/dir` was requested as the URL
        # `https:///nonexistent/dir` and reported "unreachable", and `htps://x` was attempted as is.
        if not os.path.isdir(t):
            looks_like_path = t.startswith(("/", "./", "../", "~")) or "\\" in t
            if os.path.isfile(t):
                out.append(("error", f"{t} is a file; jshound reads a site, or a folder of files a run saved", t))
                continue
            if looks_like_path:
                out.append(("error", f"no such directory: {t}", t))
                continue
            scheme = t.split("://", 1)[0].lower() if "://" in t else ""
            if scheme and scheme not in ("http", "https"):
                out.append(("error", f"{t}: only http and https URLs can be fetched", t))
                continue
        if os.path.isdir(t):
            d = os.path.abspath(t).rstrip("/")
            # `.jshound/claude.com/js` is a corpus this tool wrote, and re-reading it should put the
            # results back beside it, named for the target, not in a new folder called `js`.
            name = os.path.basename(os.path.dirname(d)) if os.path.basename(d) == "js" else \
                os.path.basename(d)
            item = ("dir", d, name)
        else:
            from urllib.parse import urlsplit
            url = t if "://" in t else "https://" + t
            item = ("url", url, urlsplit(url).netloc or t)
        if item[1] not in seen:
            seen.add(item[1])
            out.append(item)
    return out


def cmd_map(a):
    """The whole tool: `jshound <target>...`.

    A target is a hostname or URL to fetch, or a directory already on disk to read again. Every
    target's results go to `.jshound/<name>/` in four formats, untruncated, and the JavaScript it read
    goes to `.jshound/<name>/js/` beneath them, so results and corpus never share a directory.

    On screen: each request as it is made, one line per target with the status code and anything that
    cut the map short, then the endpoints. Nothing is truncated anywhere.
    """
    import contextlib
    import io
    import threading

    from . import fetch as _fetch
    from .analyze import analyze as run
    from .export import table as _table

    targets = _inputs(a)
    if not targets:
        print("jshound: nothing to read. Give it a hostname, a URL or a directory, "
              "or pipe a list in.", file=sys.stderr)
        return 1
    _fetch.set_proxy(a.proxy or os.environ.get("JSHOUND_PROXY"), a.proxy_ca)
    _fetch.set_client(a.client)

    # Written to the REAL stderr, held from before the quiet capture below, or quiet would swallow it.
    real_err, lock, tty = sys.stderr, threading.Lock(), sys.stderr.isatty()

    def colour(code):
        if not tty:
            return code
        c = "31" if code == "ERR" else {"2": "32", "3": "36", "4": "33", "5": "31"}.get(code[0], "0")
        return f"\033[{c}m{code}\033[0m"

    def on_request(url, status, nbytes):
        if a.quiet:
            return
        code = str(status) if status is not None else "ERR"
        size = (f"{nbytes / 1048576:.1f}M" if nbytes >= 1048576 else
                f"{nbytes // 1024}K" if nbytes >= 1024 else f"{nbytes}B") if nbytes else ""
        with lock:
            real_err.write(f"  {colour(code)}  {size:>6}  {url}\n")
            real_err.flush()

    _fetch.ON_REQUEST = on_request
    width = max(len(t[2]) for t in targets)
    pad = " " * (width + 2)
    worst, printed = 0, []

    try:
        for kind, src, name in targets:
            label = name.ljust(width)
            if kind == "error":
                print(f"{label}  -    {src}", file=sys.stderr)
                worst = 1
                continue
            if a.out:
                out_dir = os.path.join(a.out, _safe_name(name)) if len(targets) > 1 else a.out
            elif kind == "dir" and os.path.basename(src) == "js":
                out_dir = os.path.dirname(src)
            else:
                out_dir = os.path.join(".jshound", _safe_name(name))
            corpus = src if kind == "dir" else os.path.join(out_dir, "js")
            log = io.StringIO()
            quiet = contextlib.nullcontext() if a.verbose else contextlib.redirect_stderr(log)
            rep, code = {}, kind

            if kind == "url":
                t_fetch = time.time()
                with quiet:
                    saved = _fetch.collect(src, corpus, with_chunks=not a.no_chunks,
                                           max_chunks=a.max_chunks, timeout=a.timeout,
                                           allow_private=a.allow_private, report=rep)
                rep["fetch_seconds"] = round(time.time() - t_fetch, 1)
                code = str(rep.get("status")) if rep.get("status") is not None else "ERR"
                if rep.get("blocked"):
                    # NAME WHAT WAS TRIED. The run that failed on 2026-10-09 had tried five browsers
                    # a year out of date and said only "tried 5 client profiles", which hid the one
                    # thing a reader could fix: a newer curl_cffi imitates newer browsers.
                    tried = ", ".join(rep.get("tried") or []) or "?"
                    try:
                        import curl_cffi
                        ver = f"curl_cffi {curl_cffi.__version__}"
                    except Exception:                      # noqa: BLE001
                        ver = "no curl_cffi installed"
                    print(f"{label}  {code}  blocked by {rep['blocked']}", file=sys.stderr)
                    print(f"{pad}tried {tried}" + (", pinned with --client" if a.client else
                          f" ({ver}). A newer curl_cffi imitates newer browsers, which get refused "
                          f"less: pip install -U curl_cffi"), file=sys.stderr)
                    print(f"{pad}A refusal also moves with the IP's reputation. --proxy changes the "
                          f"IP, which helps with rate limits and bans but not with a challenge.",
                          file=sys.stderr)
                    worst = 1
                    continue
                if not saved:
                    why = "unreachable" if rep.get("status") is None else \
                        f"no scripts in a {rep.get('bytes', 0)} byte page"
                    print(f"{label}  {code}  {why}", file=sys.stderr)
                    worst = 1
                    continue

            # NOT filtered to the target's own host: a build's assets usually are not on it, and on
            # supabase.com/dashboard that filter threw away 59 of 224 endpoints, all of them the app's
            # own code on its own CDN. Each record still carries the host that served it.
            t_analyse = time.time()
            with quiet:
                records, paths = run(corpus, a.host, None)
                records = records + _graphql_operations(corpus)
            rep["analyse_seconds"] = round(time.time() - t_analyse, 1)
            if a.verbose:
                print(f"{pad}time: fetch {rep.get('fetch_seconds', 0)}s, analysis "
                      f"{rep['analyse_seconds']}s", file=sys.stderr)
            # NOTHING READABLE IS NOT AN EMPTY API. A folder with no JavaScript and no pages in it
            # would otherwise produce a valid, importable, EMPTY openapi.json, which reads as "this app
            # has no endpoints" to whatever imports it. Say so and write nothing.
            if not paths and not records:
                print(f"{label}  {code}  nothing readable in {corpus}", file=sys.stderr)
                worst = 1
                continue
            from .export.requests import requests_only
            requests = requests_only(records)

            # Where the files came from is only worth a word when it is more than one place.
            hosts = _hosts_fetched(corpus)
            where = f"{sum(hosts.values()) or len(paths)} files"
            if len(hosts) > 1:
                where += " (" + ", ".join(f"{n} from {h}" for h, n in hosts.most_common(2)) + ")"
            prof = rep.get("profile")
            note = f"  [as {prof}]" if prof and prof != _fetch.PROFILES[0] and not a.client else ""
            print(f"{label}  {code}  {where}  ->  {len(requests)} endpoints{note}", file=sys.stderr)

            # A CAP THAT IS HIT IS A HOLE IN THE MAP, so it is said out loud. At the old 400,
            # claude.ai stopped at 43% of the chunks it declares and nothing on screen said so.
            if rep.get("chunks_capped"):
                print(f"{pad}! stopped at {a.max_chunks} chunks: fetched {rep.get('chunks_fetched', 0)} of "
                      f"{rep.get('chunks_declared', 0)} declared. --max-chunks raises it.",
                      file=sys.stderr)
            if rep.get("chunks_unreachable"):
                print(f"{pad}{rep['chunks_unreachable']} declared chunk(s) could not be fetched",
                      file=sys.stderr)
            if rep.get("files_unscanned"):
                print(f"{pad}! {rep['files_unscanned']} file(s) could not be scanned for the chunks and "
                      f"imports they declare (a parse failure or a {_fetch.SCAN_TIMEOUT}s timeout), "
                      f"so whatever they lead to is missing", file=sys.stderr)
            for ln in log.getvalue().splitlines():
                if ln.lstrip().startswith(("!", "!!")):
                    print(f"{pad}{ln.strip()}", file=sys.stderr)
            if not requests:
                print(f"{pad}no requests in the scripts this page loads. If it builds its script tags "
                      f"at runtime, those requests are not in what was fetched.", file=sys.stderr)

            saved_at = _write_results(out_dir, records, name)
            print(f"{pad}-> {saved_at}  (also requests.jsonl, urls.txt, openapi.json)",
                  file=sys.stderr)
            printed.append((name, records))
    finally:
        _fetch.ON_REQUEST = None

    if not printed:
        return worst or 1
    print(file=sys.stderr)
    for name, records in printed:
        if len(printed) > 1:
            print(f"# {name}")
        print(_table.build(records))
    return worst


PRIVATE_HELP = ("allow URLs the target names in private address space (loopback, RFC1918, "
                "link-local); on by default when the target itself is private")


def _add_run_args(p):
    """The arguments of the one command."""
    p.add_argument("targets", nargs="*", metavar="TARGET",
                   help="a hostname, a URL, or a directory already on disk. "
                        "Several are fine; `-` or a pipe reads a list from stdin")
    p.add_argument("-o", "--out", metavar="DIR",
                   help="where results go (default: .jshound/<name>)")
    p.add_argument("-q", "--quiet", action="store_true", help="do not print each request")
    p.add_argument("-v", "--verbose", action="store_true", help="also print the analysis log")
    p.add_argument("--max-chunks", type=int, default=_MAX_CHUNKS, metavar="N",
                   help=f"stop after this many lazy chunks and imports (default {_MAX_CHUNKS})")
    p.add_argument("--no-chunks", action="store_true", help="do not follow lazy chunks")
    p.add_argument("--proxy", metavar="URL", help="send every request through this proxy "
                                                  "(or set JSHOUND_PROXY)")
    p.add_argument("--proxy-ca", metavar="FILE", help="CA cert for a proxy that intercepts TLS")
    p.add_argument("--client", metavar="PROFILE",
                   help="pin one browser profile instead of rotating when blocked")
    p.add_argument("--host", help=argparse.SUPPRESS)
    p.add_argument("--timeout", type=int, default=30, help=argparse.SUPPRESS)
    p.add_argument("--allow-private", action="store_true", help=PRIVATE_HELP)
    p.add_argument("--include-unresolved", action="store_true", help=argparse.SUPPRESS)


def build_parser():
    """One command, and the input decides what happens."""
    p = argparse.ArgumentParser(
        prog="jshound",
        description="Recover a web app's API from the JavaScript it ships.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  jshound app.example.com                  fetch it, follow every lazy chunk, list the requests
  jshound a.example.com b.example.com      several at once
  cat scope.txt | jshound                  a list, one per line
  jshound .jshound/app.example.com/js      re-read what was already downloaded, no network

results go to .jshound/<name>/: endpoints.txt  requests.jsonl  urls.txt  openapi.json""")
    p.add_argument("--version", action="version", version=f"jshound {__version__}")
    _add_run_args(p)
    p.set_defaults(fn=cmd_map, cmd="jshound")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv and sys.stdin.isatty():
        parser.print_help()
        return 0
    a = parser.parse_args(argv)
    # NODE_PATH is deliberately NOT defaulted here. Setting it to `/usr/local/lib/node_modules`
    # shadowed every module's own fallback to the checkout's `node_modules`, which is where Babel
    # actually is, and the only reason anything worked was node's upward walk from `jshound/js/`.
    # A CLI reports what went wrong rather than handing over a traceback: these are the failures a
    # correct invocation can still hit, and each one already carries a sentence saying which.
    try:
        return a.fn(a)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"jshound: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("jshound: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
