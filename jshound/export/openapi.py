"""openapi.py — emit the extracted contracts as an OpenAPI 3.1 document.

OpenAPI is the natural target because it is the only common format that carries what this tool
actually recovers. A URL list loses the method; a HAR loses the schema; OpenAPI holds method, path,
path parameters, query parameters, content type and a typed request body at once — which is the whole
output, not a projection of it. Burp's OpenAPI Parser and Caido both ingest it, so the spec is also
the shortest path from a bundle to a proxy's site map.

PROVENANCE SURVIVES THE EXPORT. Every operation carries `x-jshound`, which records the page that
loaded the code, the file it came from, the sink that made the call, and — the field that matters —
whether the path or body was fully resolved. An unresolved path is a SHAPE, and a consumer that
replays it without substituting a value will request a literal `{param1}`. Dropping that distinction
to make the spec look cleaner would be the one edit that makes this output dangerous.
"""
import json
import re
from collections import defaultdict

from .paths import looks_like_source_file, normalize, split_origin, split_query

_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_JSON = "application/json"


# JSON Schema's own type vocabulary. Anything else the extractor puts in a value slot is PROVENANCE
# — `computed:trim`, `state:href`, `file-upload` — which says where the value comes from, not what
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
                "description": f"body is `{rec['body_param']}` — field names are a compile-time "
                               f"type and are not present in the shipped JavaScript"}
    keys = rec.get("body_keys")
    if isinstance(keys, list) and keys:
        # Names without types. Say so rather than inventing `string` silently.
        return {"type": "object", "properties": {k: {} for k in keys},
                "x-jshound-note": "field names recovered; types not present in the shipped code"}
    return None


# An event handler is not an API operation. The enclosing function name is usually the client's own
# method (`subscribe`, `getUser`) and makes a far better operationId than anything synthesized — but in
# component code it is just as often `onClick` or `handleSubmit`, which says nothing and collides.
_NOT_AN_OP = re.compile(r"^(on[A-Z]|handle[A-Z]|use[A-Z]|render[A-Z]?|componentDid|_)|^(cb|fn|f)$")


def _operation_id(rec, method, path, used):
    """A unique, meaningful operationId. OpenAPI requires global uniqueness and Burp's parser enforces
    it — two operations called `onClick` is a document that fails to import at all."""
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

        origin, _ = split_origin(tmpl)
        host = rec.get("_host")
        if origin and "://" in origin:
            servers[origin] = True
        elif host:
            servers[f"https://{host}"] = True

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
                                "UNRESOLVED in the shipped code — substitute a real value"),
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
        # through whether or not the value resolved — a name with no value is still what you have
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

        prov = {k[1:]: rec[k] for k in ("_page", "_file", "_host") if rec.get(k)}
        prov.update({k: rec[k] for k in ("sink", "dynamic", "body_partial", "body_encoding_basis")
                     if rec.get(k) is not None})
        prov["path_fully_resolved"] = resolved
        op["x-jshound"] = prov

        # Two call sites for one operation is normal (a helper called from two pages). Keep the first
        # and record that it happened, rather than silently dropping provenance.
        if method in ops[path]:
            ops[path][method]["x-jshound"].setdefault("also_seen", []).append(
                prov.get("page") or prov.get("file"))
        else:
            ops[path][method] = op

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
        "servers": [{"url": u} for u in sorted(servers)] or [{"url": "/"}],
        "paths": {p: ops[p] for p in sorted(ops)},
    }
    if headers_seen:
        doc["info"]["x-jshound-client-headers"] = headers_seen
    return doc


def dumps(records, **kw):
    return json.dumps(build(records, **kw), indent=2)
