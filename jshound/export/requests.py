"""requests.py: the requests, and nothing else.

The extractor emits about twenty kinds of record, and most of them are its own working-out: strings
that look like paths, arguments seen at call sites, validation schemas, how a client serialises a
body. On supabase.com/dashboard that is 2,242 records, of which about 227 are requests. All of that
is what `analyze()` returns, and the library keeps returning it, because pentest's ingest reads the
working-out to build its own store.

What a person running the tool wants is the requests. This is the one place that decides what those
are, so the table, the URL list, the OpenAPI document and the JSON lines cannot disagree:

  endpoint   a call site the extractor resolved: REST, the common case
  route      a route the app's own router or build declares. A route answers a GET, so it is one
  relay      a GraphQL operation. Every operation POSTs to one endpoint, so the operation's name,
             type and variables are the request, and they are carried as its body

A GraphQL operation's URL is `{graphql}` unless the code ties it to one. Guessing is the alternative,
and the obvious guess is wrong as often as it is right: the one GraphQL URL in supabase's dashboard
is a GraphiQL screen, while its two operations come from a third-party consent manager posting
somewhere else entirely.
"""
import re


def _graphql_request(op, op_type, variables, r, uri=None, document=None):
    return {"_kind": "endpoint", "kind": "graphql", "method": "POST",
            "template": uri or "{graphql}", "op": op, "op_type": op_type,
            "body_shape": {"operationName": op, "variables": variables},
            "content_type": "application/json", "document": document,
            "_page": r.get("_page"), "_host": r.get("_host"), "_file": r.get("_file")}


# A path that names a static file: an image, a font, a stylesheet, media, or a script. The extractor
# drops these from what it reports as requests; this is the same rule for the routes it reports.
_STATIC_FILE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|avif|ico|bmp|woff2?|ttf|otf|eot|css|mp4|webm|mov|m4v|"
                          r"mp3|wav|ogg|m?js|map)$", re.I)


def requests_only(records):
    """-> endpoint-shaped records, one per request, in the order the extractor found them.

    GraphQL operations come from two places: `relay` records, which the extractor ties to a call site
    and sometimes to an endpoint, and `gql-operation` records from the document scanner, which reads
    every operation the client ships. On railway.com the first found 1 and the second 966, so both are
    read, and an operation both found is listed once, the extractor's version first.
    """
    out = []
    have = {}
    for r in records:
        kind = r.get("_kind") or "endpoint"
        if kind == "endpoint":
            out.append(r)
        elif kind == "route":
            # A route table can name a script: HubSpot's lists its own `bundle.production.js` files,
            # which are not pages anyone navigates to, and not API requests.
            if _STATIC_FILE.search(str(r.get("template") or "").split("?")[0].split("#")[0]):
                continue
            m = r.get("method")
            out.append({**r, "_kind": "endpoint",
                        "method": m if m and m != "UNKNOWN" else "GET"})
        elif kind == "relay" and r.get("op"):
            variables = {v.get("name"): v.get("type") for v in (r.get("variables") or [])
                         if isinstance(v, dict) and v.get("name")}
            key = (r["op"], r.get("opkind"))
            if key in have:
                # One operation called from five places is one request: supabase's ErrorCodeQuery was
                # listed five times. The first keeps its place; a later one can only add an endpoint.
                if not have[key].get("template") or have[key]["template"] == "{graphql}":
                    have[key]["template"] = r.get("uri") or r.get("template") or have[key]["template"]
                continue
            req = _graphql_request(r["op"], r.get("opkind"), variables, r,
                                   uri=r.get("uri") or r.get("template"))
            have[key] = req
            out.append(req)
        elif kind == "gql-operation" and r.get("operation"):
            key = (r["operation"], r.get("operation_type"))
            if key in have:
                # Already listed by the extractor, which knows the call site; the scanner knows the
                # text, and the text is what makes it replayable, so it is added, not lost.
                if r.get("document") and not have[key].get("document"):
                    have[key]["document"] = r["document"]
                continue
            variables = {v.get("name"): v.get("type") for v in (r.get("variables") or [])
                         if isinstance(v, dict) and v.get("name")}
            have[key] = _graphql_request(r["operation"], r.get("operation_type"), variables, r,
                                         document=r.get("document"))
            out.append(have[key])
    return out


_MEDIA = {"json": "application/json", "multipart": "multipart/form-data",
          "form": "application/x-www-form-urlencoded", "urlencoded": "application/x-www-form-urlencoded",
          "text": "text/plain"}


# The sinks that resolve a bare path against the page, because the browser does the resolving.
# Everything else is a wrapper (`api.post("/platform/...")`, `this.client.request({path})`) that
# puts its own base URL in front, set somewhere the analysis did not follow. On supabase.com/dashboard
# 168 of 226 requests are like that, and their base is the API host, not supabase.com, so pinning
# them to the page would print a URL the app never requests.
# A Connect transport hands its `baseUrl` to fetch as written, so a relative one is the page's too.
_PAGE_RELATIVE = {"fetch", "XHR.open", "sendBeacon", "EventSource", "WebSocket", "Request",
                  "axios", "route-const", "connect-descriptor", None}


_LOCATION_ORIGIN = re.compile(r"^\{(?:window\.)?location\.(?:origin|host)\}$", re.I)
_LOCATION_HREF = re.compile(r"^\{(?:window\.)?location\.href\}$", re.I)


def _url(r):
    """The absolute URL when the base is known, the path as found when it is not.

    A relative path from a browser sink is requested against the PAGE it ran on, not against the
    host that served the file: supabase's bundle comes off frontend-assets.supabase.com, and its
    relative fetches go to supabase.com. A path from a wrapper, or one whose base is a slot the
    analysis could not resolve, is left as it is.
    """
    from urllib.parse import urljoin, urlsplit

    from .paths import UNRESOLVED_ORIGIN, split_origin
    t = r.get("template") or r.get("url") or ""
    origin, path = split_origin(t)
    if origin == UNRESOLVED_ORIGIN or not t:
        return t
    if origin and "://" in origin:
        return origin + path
    page = r.get("_page") or (f"https://{r['_host']}/" if r.get("_host") else "")
    if origin:
        # A SLOT NAMING THE PAGE'S OWN LOCATION is the page's origin, whatever made the request.
        # Read as a path, the page was put in front of it: `https://lovable.dev/{window_location_origin}/...`.
        # A bare `{origin}` could be any object's `.origin`, a parsed URL or a message event, so it stays
        # a slot, written where it stands, and so does any of these with no page on record.
        pu = urlsplit(page)
        if not pu.netloc:
            return t
        if _LOCATION_ORIGIN.match(origin):
            base = f"{pu.scheme}://{pu.netloc}"
        elif _LOCATION_HREF.match(origin):
            base = page.split("#")[0].rstrip("/")
        else:
            return t
        return base + (path if path.startswith(("/", "{", "?")) else "/" + path)
    sink = r.get("sink")
    # A URL the server wrote into the document is resolved against the document, like a link.
    # A wrapper that hands the path to fetch is a fetch: claude.ai's `/api/...` calls go through
    # `fetchAsDocumentAccount`, and fetch resolves a relative path against the page.
    if (sink not in _PAGE_RELATIVE and not str(sink).startswith("jsx:")
            and r.get("_kind") != "doc-state" and not r.get("via_fetch")):
        return t
    # Resolved the way the browser resolves it: `/x` from the host, `x` from the page's directory.
    # Gluing the path onto the host printed `https://play.grafana.orgapis/...` for a path with no slash.
    # No page on record (a folder with no origin map) leaves the host that served the file as the
    # only stand-in for it.
    return urljoin(page, t) if urlsplit(page).netloc else t


def to_lines(records):
    """One JSON object per request: what to send, and where the code that sends it lives."""
    import os
    out = []
    for r in requests_only(records):
        body = r.get("body_shape") or ({k: None for k in r["body_keys"]} if r.get("body_keys") else None)
        ctype = r.get("content_type") or r.get("media_type") or _MEDIA.get(r.get("body_encoding") or "")
        line = {"method": r.get("method"), "url": display_url(r)}
        if body:
            line["body"] = body
            if r.get("body_partial"):
                line["body_partial"] = True     # the names given are real; the list is incomplete
        if ctype:
            line["content_type"] = ctype
            if str(r.get("body_encoding_basis") or "").startswith("INFERRED"):
                line["content_type_inferred"] = True
        if r.get("req_headers"):
            line["headers"] = r["req_headers"]
        if r.get("kind") == "graphql":
            line["graphql"] = {"operation": r.get("op"), "type": r.get("op_type")}
            if r.get("document"):
                line["graphql"]["query"] = r["document"]      # what makes it replayable
        if r.get("rpc"):
            line["rpc"] = r["rpc"]                            # service, method, and whether it streams
        if r.get("_page"):
            line["page"] = r["_page"]
        if r.get("_file"):
            line["declared_in"] = os.path.basename(r["_file"])
        out.append(line)
    return out


def display_url(r):
    """`_url`, with the path's slots named the way the OpenAPI and URL exports name them."""
    from .paths import UNRESOLVED_ORIGIN, normalize, split_origin
    u = _url(r)
    origin, _ = split_origin(u)
    path, _p, _ok = normalize(u)
    if origin and origin != UNRESOLVED_ORIGIN and "://" in origin:
        return origin + path
    if origin == UNRESOLVED_ORIGIN:
        lead = u[:u.index("}") + 1] if u.startswith("{") and "}" in u else ""
        return lead if path == "/" else lead + path
    # A slot standing where the origin goes, `{origin}`, or the page's location with no page on record,
    # is kept in front as written. Dropping it made hex.tech's `{origin}/orgs` read as a plain `/orgs`.
    if origin and "://" not in origin:
        return origin if path == "/" else origin + path
    return path
