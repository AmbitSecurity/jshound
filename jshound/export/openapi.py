"""openapi.py: emit the extracted contracts as an OpenAPI 3.1 document.

OpenAPI is the natural target because it is the only common format that carries what this tool
actually recovers. A URL list loses the method; a HAR loses the schema; OpenAPI holds method, path,
path parameters, query parameters, content type and a typed request body at once, which is the whole
output, not a projection of it. Burp's OpenAPI Parser and Caido both ingest it, so the spec is also
the shortest path from a bundle to a proxy's site map.

PROVENANCE SURVIVES THE EXPORT. Every operation carries `x-jshound`, which records the page that
loaded the code, the file it came from, the sink that made the call, and, the field that matters,
whether the path or body was fully resolved. An unresolved path is a SHAPE, and a consumer that
replays it without substituting a value will request a literal `{param1}`. Dropping that distinction
to make the spec look cleaner would be the one edit that makes this output dangerous.
"""
import json
import os
import re
from collections import defaultdict

from .paths import looks_like_source_file, normalize, split_origin, split_query

_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_JSON = "application/json"


# JSON Schema's own type vocabulary. Anything else the extractor puts in a value slot is PROVENANCE
#, `computed:trim`, `state:href`, `file-upload`, which says where the value comes from, not what
# type it is. Writing those into `type` produces a document that is not valid OpenAPI, and Burp's
# parser is entitled to reject the whole file over one of them.
_JSON_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}


def _prop(value):
    if isinstance(value, str) and value in _JSON_TYPES:
        return {"type": value}
    if isinstance(value, str) and value and value != "?":
        return {"type": "string", "description": f"source: {value}"}
    return {}


def _schema_from(rec, zod_by_name):
    """Best available typed schema for a request body, or None."""
    sch = rec.get("body_schema")
    if isinstance(sch, dict) and sch:
        return {"type": "object", "properties": {k: _prop(v) for k, v in sch.items()}}
    # A zod validator sitting on the same body is the only place real TYPES survive minification.
    name = rec.get("body_schema") if isinstance(rec.get("body_schema"), str) else None
    if name and name in zod_by_name:
        fields = zod_by_name[name]
        return {"type": "object",
                "properties": {f["key"]: {"type": f.get("type") or "string"} for f in fields},
                "required": [f["key"] for f in fields if not f.get("optional")]}
    shape = rec.get("body_shape") or rec.get("body_src")
    if isinstance(shape, dict) and shape:
        return {"type": "object", "properties": {k: _prop(v) for k, v in shape.items()}}
    # A body that exists but whose field names are a compile-time type is not a bodyless request.
    # Emitting nothing at all would tell a reader to send no payload, which is wrong; emitting an
    # empty object schema with the expression in the description says what is actually known.
    if rec.get("body_param"):
        return {"type": "object",
                "description": f"body is `{rec['body_param']}`, field names are a compile-time "
                               f"type and are not present in the shipped JavaScript"}
    keys = rec.get("body_keys")
    if isinstance(keys, list) and keys:
        # Names without types. Say so rather than inventing `string` silently.
        return {"type": "object", "properties": {k: {} for k in keys},
                "x-jshound-note": "field names recovered; types not present in the shipped code"}
    return None


# An event handler is not an API operation. The enclosing function name is usually the client's own
# method (`subscribe`, `getUser`) and makes a far better operationId than anything synthesized, but in
# component code it is just as often `onClick` or `handleSubmit`, which says nothing and collides.
_NOT_AN_OP = re.compile(r"^(on[A-Z]|handle[A-Z]|use[A-Z]|render[A-Z]?|componentDid|_)|^(cb|fn|f)$")


def _operation_id(rec, method, path, used):
    """A unique, meaningful operationId. OpenAPI requires global uniqueness and Burp's parser enforces
    it, two operations called `onClick` is a document that fails to import at all."""
    name = rec.get("op")
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(name)) or _NOT_AN_OP.match(str(name)):
        name = f"{method}_{path}".replace("/", "_").replace("{", "").replace("}", "")
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip("_") or "operation"
    if name in used:
        n = 2
        while f"{name}_{n}" in used:
            n += 1
        name = f"{name}_{n}"
    used.add(name)
    return name


def _unknown_base(where):
    """A server for an operation whose base URL the bundle does not contain.

    `api.post("/platform/...")` and `${this.baseUrl}/v1/...` are requests to a host the code decides
    at runtime. Under the document's servers they would read as requests to the page's host, and a
    proxy importing this would send them there: on supabase that is supabase.com for calls that go
    to the API host, which can be a host out of scope. So the base is a variable, named after the
    code's own name for it where it has one, and its default is a reserved name that resolves
    nowhere (RFC 2606), so nothing is sent anywhere until someone fills it in.
    """
    m = re.match(r"^\{([^}]+)\}", where or "")
    name = re.sub(r"[^A-Za-z0-9_]", "_", m.group(1)).strip("_") if m else ""
    name = name or "base"
    note = (f"Not in the bundle: the code sets this base at runtime"
            + (f", as {m.group(1)}" if m else ", in the client that sends this request")
            + ". Replace it with the real host. The default resolves nowhere, so nothing is sent by "
            "accident.")
    return [{"url": "{" + name + "}",
             "variables": {name: {"default": "https://base.invalid", "description": note}}}]


def _graphql_example(op, rec):
    """Add one GraphQL operation to `op` as a request body example."""
    name = rec["op"]
    kind = rec.get("op_type") or "operation"
    variables = (rec.get("body_shape") or {}).get("variables") or {}
    value = {"operationName": name, "variables": {k: f"<{t}>" for k, t in variables.items()}}
    if rec.get("document"):
        value["query"] = rec["document"]
    body = op.setdefault("requestBody", {"required": True, "content": {}})
    media = body.setdefault("content", {}).setdefault(_JSON, {})
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{kind}.{name}")
    media.setdefault("examples", {}).setdefault(key, {"summary": f"{kind} {name}", "value": value})


def _server(url):
    """A document server. A slot in the host (`https://{projectRef}.supabase.co`) is declared as a
    variable whose default is empty, which leaves the hostname invalid until it is filled in. Under
    a wildcard DNS name any made-up default would resolve, quite possibly to someone else's project."""
    slots = re.findall(r"\{([^}]+)\}", url)
    if not slots:
        return {"url": url}
    variables = {}
    for raw in slots:
        name = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_") or "slot"
        url = url.replace("{" + raw + "}", "{" + name + "}")
        variables[name] = {"default": "", "description": (
            f"Not in the bundle ({raw}). Fill it in before sending; left empty, the host is invalid "
            "and nothing is sent.")}
    return {"url": url, "variables": variables}


def build(records, title="Recovered API", version="0.0.0"):
    zod_by_name = {r["name"]: r.get("zod_fields") or []
                   for r in records if r.get("_kind") == "zod" and r.get("name")}
    headers_seen = sorted({r["name"] for r in records
                           if r.get("_kind") == "client-header" and r.get("name")})

    ops = defaultdict(dict)        # path -> method -> operation
    servers, unresolved, used_ids = {}, 0, set()

    for rec in records:
        kind = rec.get("_kind") or "endpoint"
        if kind not in ("endpoint", "route"):
            continue
        tmpl = rec.get("template") or rec.get("url")
        if not tmpl:
            continue
        method = (rec.get("method") or "GET").lower()
        if method not in _METHODS:
            continue

        # The server is where the request goes, as the table resolves it, never the host that served
        # the file: supabase's bundle comes off a CDN that answers no API call. A request whose base
        # the code only sets at runtime gets a server of its own instead of the document's, see
        # `_unknown_base`, or importing this into a proxy would aim it at the wrong host.
        from .requests import _url
        where = _url(rec)
        origin, _ = split_origin(where)
        op_servers = None
        if origin and "://" in origin:
            servers[origin] = True
        else:
            op_servers = _unknown_base(where)

        path, params, resolved = normalize(tmpl)
        if looks_like_source_file(path):
            continue
        if not resolved:
            unresolved += 1

        op = {
            "operationId": _operation_id(rec, method, path, used_ids),
            "responses": {"default": {"description": "not observed; recovered from client code"}},
        }
        if params:
            op["parameters"] = [{
                "name": p["name"], "in": "path", "required": True,
                "schema": {"type": "string"},
                "description": ("recovered name" if p["resolved"] else
                                "UNRESOLVED in the shipped code, substitute a real value"),
            } for p in params]
        # Query names come from three places: the template's own `?a=b`, a `query_schema` the
        # extractor built, and a plain `query` list. Merge and de-duplicate.
        _, qnames = split_query(split_origin(tmpl)[1])
        for q in (rec.get("query_schema") or []):
            n = q.get("name") if isinstance(q, dict) else q
            if n and n not in qnames:
                qnames.append(n)
        for q in (rec.get("query") or []):
            if q and q not in qnames:
                qnames.append(q)
        for q in qnames:
            op.setdefault("parameters", []).append(
                {"name": str(q), "in": "query", "required": False, "schema": {"type": "string"}})

        # REQUEST HEADERS ARE TESTABLE SURFACE and belong in the spec, not only in a note. Grafana
        # sends x-grafana-alerting-datasource-uid and friends on specific operations; a reader
        # importing the document should see them on the operation that sends them. The names come
        # through whether or not the value resolved, a name with no value is still what you have
        # to send. content-type is skipped: it is carried by requestBody's media type already.
        rh = rec.get("req_headers")
        hnames = list(rh) if isinstance(rh, (list, tuple, dict)) else []
        for hn in hnames:
            if not hn or str(hn).lower() in ("content-type", "accept"):
                continue
            val = rh.get(hn) if isinstance(rh, dict) else None
            param = {"name": str(hn), "in": "header", "required": False,
                     "schema": {"type": "string"}}
            if isinstance(val, str) and val:
                param["description"] = f"observed value: {val}"
            op.setdefault("parameters", []).append(param)

        if method not in ("get", "head"):
            schema = _schema_from(rec, zod_by_name)
            ctype = rec.get("content_type") or (_JSON if rec.get("body_encoding") == "json" else None)
            if schema or ctype:
                body = {"required": False,
                        "content": {ctype or _JSON: ({"schema": schema} if schema else {})}}
                if rec.get("body_param"):
                    body["description"] = (
                        "A body IS sent. Its field names are a compile-time type, erased from the "
                        "bundle; the expression that produces it is in the schema description.")
                elif rec.get("body_partial"):
                    body["description"] = ("PARTIAL: part of this body is assembled from a value the "
                                           "analysis could not resolve. The fields named are real; "
                                           "the list is incomplete.")
                op["requestBody"] = body

        if op_servers:
            op["servers"] = op_servers
        # The file by its name only. The full path is the path on the machine that ran this, which
        # is that person's username and folder layout in a document made to be shared.
        prov = {k[1:]: rec[k] for k in ("_page", "_host") if rec.get(k)}
        if rec.get("_file"):
            prov["file"] = os.path.basename(str(rec["_file"]))
        prov.update({k: rec[k] for k in ("sink", "dynamic", "body_partial", "body_encoding_basis")
                     if rec.get(k) is not None})
        prov["path_fully_resolved"] = resolved
        op["x-jshound"] = prov

        # GRAPHQL OPERATIONS SHARE ONE PATH AND ONE METHOD, and OpenAPI holds one operation per pair,
        # so 966 of railway.com's operations collapsed into its first. Each one is a named example on
        # that operation's request body instead, with its query text, which is how OpenAPI tools
        # import a set of bodies for one endpoint.
        if rec.get("kind") == "graphql" and rec.get("op"):
            if method not in ops[path]:
                ops[path][method] = op
            _graphql_example(ops[path][method], rec)
            continue

        # Two call sites for one operation is normal (a helper called from two pages). Keep the first
        # and record that it happened, rather than silently dropping provenance.
        if method in ops[path]:
            ops[path][method]["x-jshound"].setdefault("also_seen", []).append(
                prov.get("page") or prov.get("file"))
        else:
            ops[path][method] = op

    for methods in ops.values():
        for op in methods.values():
            ex = ((op.get("requestBody") or {}).get("content") or {}).get(_JSON, {}).get("examples")
            if ex:
                op["summary"] = f"GraphQL: {len(ex)} operation(s), one example each"

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": title, "version": version,
            "description": (
                "Recovered by jshound from shipped JavaScript. Nothing here was observed on the "
                "wire: these are the requests the client is built to make. Operations whose "
                "x-jshound.path_fully_resolved is false contain placeholders that must be "
                f"substituted before replay ({unresolved} of them)."),
        },
        "servers": [_server(u) for u in sorted(servers)] or [{"url": "/"}],
        "paths": {p: ops[p] for p in sorted(ops)},
    }
    if headers_seen:
        doc["info"]["x-jshound-client-headers"] = headers_seen
    return doc


def dumps(records, **kw):
    return json.dumps(build(records, **kw), indent=2)
