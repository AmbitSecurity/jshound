"""har.py — emit the extracted contracts as HAR 1.2.

HAR is the lowest common denominator that still carries a REQUEST rather than a URL: Burp's HAR
Importer drops entries straight into the Site Map, Caido imports it natively, and so do Fiddler,
Postman and Chrome DevTools. OpenAPI is the better format when the consumer understands schemas;
HAR is the one that works everywhere.

A HAR normally records traffic that happened. Nothing here happened — these are requests the client
is BUILT to make, recovered from its code — so every entry carries `_jshound` provenance and a
response with status 0. Writing a plausible 200 would make the file read like observed traffic, and
nothing downstream could then tell the difference between evidence and inference.
"""
import json
from datetime import datetime, timezone

from .paths import looks_like_source_file, normalize, split_origin, split_query

_SKIP_BODY = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _url(rec):
    tmpl = rec.get("template") or rec.get("url") or ""
    origin, _ = split_origin(tmpl)
    path, _params, resolved = normalize(tmpl)
    if origin and "://" in origin:
        base = origin
    elif rec.get("_host"):
        base = f"https://{rec['_host']}"
    else:
        base = ""
    return base + path, resolved


def _headers(h):
    """req_headers is a dict when the values are known and a LIST OF NAMES when only the names are —
    rtk-query reports the second shape for `headers:{…}` whose values it could not resolve. Assuming
    the dict crashed the whole export on Grafana with AttributeError, so nothing was written at all.
    A name with no value is still the useful half: it is what a tester has to send."""
    if isinstance(h, dict):
        return [{"name": k, "value": v if isinstance(v, str) else ""} for k, v in h.items()]
    if isinstance(h, (list, tuple)):
        return [{"name": str(k), "value": ""} for k in h if k]
    return []


def _query_names(rec):
    _, names = split_query(split_origin(rec.get("template") or rec.get("url") or "")[1])
    for q in (rec.get("query_schema") or []):
        n = q.get("name") if isinstance(q, dict) else q
        if n and n not in names:
            names.append(n)
    for q in (rec.get("query") or []):
        if q and q not in names:
            names.append(q)
    return [{"name": str(n), "value": ""} for n in names]


def _post_data(rec):
    ctype = rec.get("content_type") or ("application/json"
                                        if rec.get("body_encoding") == "json" else None)
    keys = rec.get("body_keys")
    schema = rec.get("body_schema")
    if isinstance(schema, dict) and schema:
        text = json.dumps(schema, indent=2)
    elif isinstance(keys, list) and keys:
        text = json.dumps({k: "" for k in keys}, indent=2)
    elif ctype:
        text = ""
    else:
        return None
    return {"mimeType": ctype or "application/json", "text": text, "params": []}


def build(records, title="jshound"):
    now = datetime.now(timezone.utc).isoformat()
    entries = []
    for rec in records:
        if (rec.get("_kind") or "endpoint") not in ("endpoint", "route"):
            continue
        url, resolved = _url(rec)
        if not url or looks_like_source_file(normalize(rec.get("template") or rec.get("url") or "")[0]):
            continue
        method = (rec.get("method") or "GET").upper()
        headers = _headers(rec.get("req_headers"))
        req = {
            "method": method, "url": url, "httpVersion": "HTTP/1.1",
            "cookies": [], "headers": headers, "queryString": _query_names(rec),
            "headersSize": -1, "bodySize": -1,
        }
        if method not in _SKIP_BODY:
            pd = _post_data(rec)
            if pd:
                req["postData"] = pd
        entries.append({
            "startedDateTime": now, "time": 0, "request": req,
            "response": {"status": 0, "statusText": "", "httpVersion": "HTTP/1.1",
                         "cookies": [], "headers": [],
                         "content": {"size": 0, "mimeType": ""},
                         "redirectURL": "", "headersSize": -1, "bodySize": -1},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            "_jshound": {
                "page": rec.get("_page"), "file": rec.get("_file"), "sink": rec.get("sink"),
                "path_fully_resolved": resolved, "body_partial": bool(rec.get("body_partial")),
                "note": "recovered from shipped JavaScript; never sent",
            },
        })
    return {"log": {"version": "1.2",
                    "creator": {"name": "jshound", "version": "0.1.0", "comment": title},
                    "entries": entries}}


def dumps(records, **kw):
    return json.dumps(build(records, **kw), indent=2)
