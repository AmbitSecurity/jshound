"""jshound — recover a SPA's API contracts from the JavaScript it ships.

Library use mirrors the CLI, because the CLI is a thin shell over exactly these calls:

    import jshound

    records = jshound.analyze("./bodies", host="app.example.com")
    spec    = jshound.to_openapi(records, title="app.example.com")
    har     = jshound.to_har(records)
    lines   = jshound.to_urls(records, with_method=True)

    chunks  = jshound.chunk_urls(open("main.js").read())   # lazy chunks, by evaluating the builder
    detail  = jshound.chunk_map(open("main.js").read())    # ...with coverage stats
    tree    = jshound.unbundle("./out", ["main.js"])        # webpack registry -> module tree

`analyze` reads a directory of already-downloaded bodies and makes NO network requests. That is a
deliberate split, not an omission: fetching belongs to whatever already does it well for you — a
crawler, a proxy export, curl — and duplicating a worse copy of it inside the analyser is how the
original tool got three hosts firewall-banned. `jshound.fetch` is a convenience for the standalone
case and is the only part that touches the network.
"""
__version__ = "0.1.0"

from .analyze import analyze as _analyze
from .export import har as _har
from .export import openapi as _openapi
from .export import urls as _urls

__all__ = ["analyze", "to_openapi", "to_har", "to_urls", "chunk_urls", "chunk_map", "unbundle",
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


def chunk_map(js_source):
    """Full chunk analysis: {publicPath, chunks, speculative, fallback, stats}.

    `stats` is the part worth reading. A caller that cannot tell "this bundle declares no chunks"
    from "it declares 40 and we built 12" cannot report a hole it does not know it has.
    """
    import json
    import os
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "js", "chunkmap.js")],
                       input=js_source, capture_output=True, text=True,
                       env={**os.environ, "NODE_PATH": os.environ.get(
                           "NODE_PATH", "/usr/local/lib/node_modules")})
    return json.loads(r.stdout or "{}")


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
