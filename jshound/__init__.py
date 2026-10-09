"""jshound, recover a SPA's API contracts from the JavaScript it ships.

Library use mirrors the CLI, because the CLI is a thin shell over exactly these calls:

    import jshound

    records = jshound.analyze("./bodies", host="app.example.com")
    spec    = jshound.to_openapi(records, title="app.example.com")
    har     = jshound.to_har(records)
    lines   = jshound.to_urls(records, with_method=True)

    chunks  = jshound.chunk_urls(open("main.js").read())   # lazy chunks, read out of the builder
    detail  = jshound.chunk_map(open("main.js").read())    # ...with coverage stats
    tree    = jshound.unbundle("./out", ["main.js"])        # webpack registry -> module tree

`analyze` reads a directory of already-downloaded bodies and makes NO network requests. That is a
deliberate split, not an omission: fetching belongs to whatever already does it well for you, a
crawler, a proxy export, curl, and duplicating a worse copy of it inside the analyser is how the
original tool got blocked by three hosts' edge protection. `jshound.fetch` is a convenience for the standalone
case and is the only part that touches the network.
"""
__version__ = "0.1.0"

from .analyze import analyze as _analyze
from .export import har as _har
from .export import openapi as _openapi
from .export import urls as _urls

__all__ = ["analyze", "to_openapi", "to_har", "to_urls", "chunk_urls", "chunk_map", "unbundle",
           "read_records", "unfetched", "analyze_with_paths",
           "residual", "graphql_operations", "__version__"]


def analyze(bodies, host=None, manifest=None):
    """Directory of saved JS/HTML -> list of contract records. Never touches the network."""
    records, _paths = _analyze(bodies, host, manifest)
    return records


def to_openapi(records, title="Recovered API", version="0.0.0"):
    """-> OpenAPI 3.1 document as a dict. Burp's OpenAPI Parser and Caido both ingest it."""
    return _openapi.build(records, title=title, version=version)


def to_har(records, title="jshound"):
    """-> HAR 1.2 document as a dict, for Burp's Site Map, Caido, Postman, DevTools."""
    return _har.build(records, title=title)


def to_urls(records, include_unresolved=False, with_method=False):
    """-> sorted list of absolute URLs, for nuclei / ffuf / httpx."""
    return _urls.build(records, include_unresolved=include_unresolved, with_method=with_method)


def read_records(path):
    """Records from a file written by `analyze -f jsonl`, or any earlier JSON array of them.

    `-f jsonl` emits one object per line, as the name says. It used to emit a pretty-printed JSON
    ARRAY, so files written before that are still read here rather than rejected, and an OpenAPI or
    HAR document is not records, so saying so beats a KeyError three frames down.
    """
    import json
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if text.lstrip().startswith("["):
        data = json.loads(text)
    else:
        try:
            data = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        except json.JSONDecodeError:
            doc = json.loads(text)                  # a single object spanning many lines
            if isinstance(doc, dict) and {"log", "openapi", "paths"} & set(doc):
                raise ValueError(
                    f"{path} is an exported document (OpenAPI or HAR), not records: read_records takes "
                    "what jshound.analyze() returns, saved as JSON or JSON lines") from None
            data = doc
    if not isinstance(data, list):
        raise ValueError(f"{path} does not hold a list of records")
    # Checked on what was PARSED, not on how the file was laid out. An OpenAPI document written on one
    # line is valid JSON Lines (one line, one object), so the layout test above let it straight
    # through as a single "record", and nothing downstream could tell.
    if any(isinstance(r, dict) and ("openapi" in r or "log" in r) for r in data):
        raise ValueError(f"{path} is an exported document (OpenAPI or HAR), not records")
    return data


# How long one file may take to scan for what it declares. The largest real chunk measured is under
# 12 MB and scans in seconds; this only stops a file built to hang the parser, and a stop is counted.
SCAN_TIMEOUT = 600


def chunk_map(js_source):
    """Full chunk analysis: {publicPath, chunks, speculative, fallback, stats}.

    `stats` is the part worth reading. A caller that cannot tell "this bundle declares no chunks"
    from "it declares 40 and we built 12" cannot report a hole it does not know it has.
    """
    import json
    import os
    import subprocess

    from .nodeenv import node_env, node_failure
    here = os.path.dirname(os.path.abspath(__file__))
    env = node_env()
    env.setdefault("NODE_PATH", os.path.join(os.path.dirname(here), "node_modules"))
    try:
        r = subprocess.run(["node", os.path.join(here, "js", "chunkmap.js")],
                           input=js_source, capture_output=True, text=True,
                           env=env, timeout=SCAN_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError("node is not on PATH. The extractors are JavaScript, so this needs "
                           "Node 18 or newer") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the chunk extractor did not finish within {SCAN_TIMEOUT}s on this bundle") from None
    # `json.loads(r.stdout or "{}")` is what this used to be, and it turned every failure into a
    # bundle that declares no chunks: a missing `@babel/parser` printed `{}` with exit 0 and an empty
    # stderr. The docstring above promises the caller can tell those two apart, so the exit code and
    # stderr have to be read rather than dropped.
    why = node_failure(r, need_output=True)
    if why:
        raise RuntimeError("the chunk extractor failed: " + why)
    return json.loads(r.stdout)


def unfetched(bodies, manifest=None):
    """-> (chunk URLs the corpus declares but does not contain, count that could not be resolved).

    For a caller that has its own fetcher. `jshound.fetch.collect` does this and the downloading in
    one pass; this is the half to use when something else, a crawler with a session, a browser,
    should make the requests.
    """
    from .fetch import unfetched as _u
    return _u(bodies, manifest)


def analyze_with_paths(bodies, host=None, manifest=None):
    """-> (records, the files that were read). `analyze` returns only the records.

    The file list is what distinguishes "this corpus holds no requests" from "nothing was read", so a
    caller that reports coverage needs it.
    """
    return _analyze(bodies, host, manifest)


def chunk_urls(js_source):
    """Just the lazy-chunk URLs the runtime's own builder produces."""
    return chunk_map(js_source).get("chunks") or []


def unbundle(outdir, paths):
    """Webpack bundle -> module tree on disk. Returns the summary dict."""
    from .modules import unbundle as _u
    return _u(outdir, paths)


def graphql_operations(bodies):
    """GraphQL operations the client ships: type, name, typed variables, top-level selection.

    On a GraphQL app there is one path and the surface is the operation set, so reporting
    `POST /graphql` and stopping finds the endpoint and tells you nothing.
    """
    from .graphqlops import scan
    return scan(bodies)


def residual(bodies, records_json):
    """What the extractor MISSED, bucketed. The only honest way to score a recon tool."""
    from .residual import classify
    return classify(bodies, records_json)
