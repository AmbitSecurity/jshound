#!/usr/bin/env python3
"""graphqlops.py: recover the GraphQL operations an app ships.

    jshound graphql ./bodies

On a REST app the attack surface is a list of paths. On a GraphQL app there is ONE path, `/graphql`,
and the surface is the set of operations that can be sent to it. A tool that reports
`POST /graphql` and stops has technically found the endpoint and told you nothing: the question is
which of `DeleteOrganization`, `AdminImpersonate` and `ExportAllUsers` this client knows how to ask
for, and which arguments each takes.

Those documents ship in the bundle as ordinary strings, because the client has to send them. So this
reads them out: operation type and name, the variables from the signature with their GraphQL types
(including which are `!` non-null), and the top-level selection. Variable types are the part worth
having, a mutation taking `$orgId: ID!` is an authorization test waiting to be written, and the
type is right there where minification cannot reach it.

NOT AN AST. GraphQL documents are self-identifying in text and are usually inside template literals
by the time they reach a bundle, so a brace-matched scan reads them more reliably than walking the
JavaScript around them. Anonymous operations (`{ me { id } }`) are skipped: they carry no name to
report and the shape alone is not worth the false positives.
"""
import argparse
import json
import os
import re
import sys

# `query Name(...)  {` , the name is required here; see the module docstring on anonymous ops.
_OP = re.compile(r"\b(query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]{0,2000}\))?\s*\{")
_VAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^,$)]+)")
_FIELD = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*:\s*)?([A-Za-z_][A-Za-z0-9_]*)")


def _match_braces(text, start):
    """Index just past the `{...}` block beginning at `start`, or None if it never closes."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c in "\"'":                      # a string inside the document, skip it wholesale
            q, i = c, i + 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        i += 1
    return None


# A document inside a string literal has ESCAPED whitespace: the two characters `\` and `n`, not a
# newline. Splitting on real newlines then sees one long line beginning with a backslash and matches
# no field at all, which silently dropped three of Linear's four operations.
_ESCAPED_WS = re.compile(r"\\[nrt]")


def _top_level_fields(body):
    """The selection one level inside the operation, what it actually asks the server for."""
    body = _ESCAPED_WS.sub("\n", body)
    out, depth, cur = [], 0, []
    for ch in body:
        if ch == "{":
            depth += 1
            if depth == 1:
                cur = []
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        if depth == 1:
            cur.append(ch)
    for line in re.split(r"[\n,]", "".join(cur)):
        m = _FIELD.match(line)
        if m and m.group(1) not in ("__typename",):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


# JavaScript that reads like GraphQL. `query by(...){...}` is a method named `by` on something
# called `query`, and `subscription for(...)` is the same shape, both were extracted as operations
# from real bundles before these guards. A GraphQL operation is never a property access and never
# names itself after a JS keyword.
_JS_KEYWORD = {"by", "for", "if", "of", "in", "do", "new", "function", "return", "this", "class",
               "const", "let", "var", "await", "async", "get", "set", "then", "catch", "map",
               "filter", "forEach", "on", "to", "as", "is", "at"}


def extract(source, origin=None):
    """-> list of operation records found in one JavaScript source."""
    found, seen = [], set()
    for m in _OP.finditer(source):
        kind, name, sig = m.group(1), m.group(2), m.group(3) or ""
        # `x.query Name {` is a property access, not a document.
        before = source[max(0, m.start() - 1):m.start()]
        if before in (".", "?"):
            continue
        if name in _JS_KEYWORD or name.lower() in _JS_KEYWORD:
            continue
        end = _match_braces(source, m.end() - 1)
        if end is None:
            continue
        body = source[m.end() - 1:end]
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        variables = [{"name": v, "type": t.strip(), "required": t.strip().endswith("!")}
                     for v, t in _VAR.findall(sig)]
        selection = _top_level_fields(body)[:1000]
        # A document that asks the server for nothing is not a document.
        if not selection:
            continue
        document = _ESCAPED_WS.sub("\n", source[m.start():end])
        rec = {"_kind": "gql-operation", "operation_type": kind, "operation": name,
               "variables": variables, "selection": selection,
               # Only a generated blob is this long; a real operation's text is what makes it
               # replayable, and it used to be dropped past 8,000 characters.
               "document": document if len(document) <= 1_000_000 else None,
               "document_chars": end - m.start()}
        if origin:
            rec["_file"] = origin
        found.append(rec)
    return found


def scan(bodies):
    """-> operation records across every .js/.html file in a bodies directory."""
    from jshound.analyze import require_corpus
    require_corpus("graphql", bodies)
    out, seen = [], set()
    for root, _d, files in os.walk(bodies):
        for f in sorted(files):
            if not f.endswith((".js", ".mjs", ".cjs", ".html", ".ts")):
                continue
            path = os.path.join(root, f)
            try:
                src = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for rec in extract(src, origin=path):
                key = (rec["operation_type"], rec["operation"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


def to_har(ops, endpoint, title="jshound graphql"):
    """Operations -> HAR 1.2, one POST per operation, so they land in Burp's Site Map replayable.

    A list of operation names is a finding you still have to hand-translate into requests. The
    document, the variable names and their types are all recovered already, so the request can be
    built: variables are emitted as a skeleton keyed by name with the GraphQL type as the value, so
    whoever replays it can see that `$workspaceId` wants a `UUID!` without reading the bundle.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    entries = []
    for o in ops:
        body = {"operationName": o["operation"],
                "query": o.get("document") or f"# {o['operation_type']} {o['operation']}",
                "variables": {v["name"]: v["type"] for v in o["variables"]}}
        entries.append({
            "startedDateTime": now, "time": 0,
            "request": {"method": "POST", "url": endpoint, "httpVersion": "HTTP/1.1",
                        "cookies": [], "queryString": [],
                        "headers": [{"name": "content-type", "value": "application/json"}],
                        "postData": {"mimeType": "application/json",
                                     "text": json.dumps(body, indent=2), "params": []},
                        "headersSize": -1, "bodySize": -1},
            "response": {"status": 0, "statusText": "", "httpVersion": "HTTP/1.1", "cookies": [],
                         "headers": [], "content": {"size": 0, "mimeType": ""},
                         "redirectURL": "", "headersSize": -1, "bodySize": -1},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            "_jshound": {"operation_type": o["operation_type"],
                           "note": "recovered from shipped JavaScript; never sent"},
        })
    return {"log": {"version": "1.2",
                    "creator": {"name": "jshound", "version": "0.1.0", "comment": title},
                    "entries": entries}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound graphql")
    ap.add_argument("bodies")
    ap.add_argument("-o", "--out")
    ap.add_argument("--summary", action="store_true", help="one line per operation")
    a = ap.parse_args(argv)
    ops = scan(a.bodies)
    if a.summary:
        for r in ops:
            v = ", ".join(f"${x['name']}: {x['type']}" for x in r["variables"])
            print(f"{r['operation_type']:<13}{r['operation']:<40}({v})")
    else:
        payload = json.dumps(ops, indent=2)
        if a.out:
            open(a.out, "w").write(payload)
        else:
            print(payload)
    print(f"{len(ops)} operation(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
