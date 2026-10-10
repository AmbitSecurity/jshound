"""rpc.py: the Connect RPC services a client is generated for, read out of their descriptors.

A Connect call goes to `<baseUrl>/<package>.<Service>/<Method>`, and no client writes that URL down:
the library assembles it at call time from a service descriptor. protobuf-es v2, what Buf generates
today, ships each .proto file as a serialised FileDescriptorProto in a base64 string,

    const file_x = fileDesc("CgxhcGkvdjEvYS5wcm90bxIG...", [file_dep]);

so the services, their methods and every request message's fields are in the bundle, encoded where
no string search can see them. On claude.ai that was 44 descriptors, about 20 services and 150
methods, of which the output listed only the few the app also calls by a literal URL.

This decodes them with a small reader of the protobuf wire format, enough of descriptor.proto for
names, packages, messages and services. The base URL comes from the transport the client is made
with, `createConnectTransport({baseUrl: "/claudeai-rpc", useBinaryFormat: true})`, when the bundle
names one; otherwise it is left as `{connect}`.

connect-es v1, and `@bufbuild/connect-web` before it, wrote a service as a plain object,
`{typeName, methods: {say: {name: "Say", I: SayRequest, kind: MethodKind.Unary}}}`, and each
message as a class with a field list. Those are read too, the request's fields from the class the
method names, or failing that from `<package>.<Method>Request`, the name Buf's linter requires.
Dropbox writes a base into each of its service objects, `grpcWebBaseUrl: "/web-grpc/edison/"`, and
that is used for the service when it is there.

A bundle whose only RPC runtime is gRPC-web gets gRPC-web's content types; that is how Dropbox's
client talks.
"""
import base64
import os
import re

# A FileDescriptorProto opens with field 1 (its file name), tag 0x0A, so its base64 opens "C" and then
# one of "g" to "v" depending on the name's length.
_DESCRIPTOR = re.compile(r'["\'`](C[g-v][A-Za-z0-9+/]{38,}={0,2})["\'`]')
_BASE_URL = re.compile(r'baseUrl\s*:\s*["\'`]([^"\'`\s]{1,300})["\'`]')
# Any dotted type name: Dropbox's services are `CodingAgentApiV2` and `JanusOAuthClient` as often as
# `ViewerService`. What makes it a service is the `methods` object beside it, which no message has.
_V1_SERVICE = re.compile(r'typeName\s*:\s*["\'`]([A-Za-z_][\w.]*\.[A-Za-z_]\w*)["\'`]')
_V1_NAME = re.compile(r'\bname\s*:\s*["\'`]([A-Za-z_]\w*)["\'`]')
# MethodKind: Unary 0, ServerStreaming 1, ClientStreaming 2, BiDiStreaming 3, by name or inlined.
_V1_KIND = re.compile(r'\bkind\s*:\s*(?:[^,{}]*?\b(Unary|ServerStreaming|ClientStreaming|BiDiStreaming)\b|([0-3])\b)')
_V1_INPUT = re.compile(r'\bI\s*:\s*([\w$]+(?:\.[\w$]+)?)')
_V1_OWN_BASE = re.compile(r'\b\w*[bB]aseUrl\s*:\s*["\'`]([^"\'`\s]{1,300})["\'`]')
_V1_MESSAGE = re.compile(r'typeName\s*=\s*["\'`]([A-Za-z_][\w.]*)["\'`]')
_V1_FIELDS = re.compile(r'newFieldList\(\s*\(\s*\)\s*=>\s*\[')
_V1_CLASS = re.compile(r'class\s+([\w$]+)\s+extends\b')

# FieldDescriptorProto.Type, as the label a body field gets. 64-bit integers are strings in Connect's
# JSON mapping, which is the form a tester replays.
_TYPES = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64", 7: "fixed32",
          8: "bool", 9: "string", 10: "group", 11: "message", 12: "bytes", 13: "uint32", 14: "enum",
          15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64"}


def _varint(b, i):
    shift = val = 0
    while True:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(b):
    """(number, wire type, value) for each field of one message, in order."""
    i, out = 0, []
    while i < len(b):
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise ValueError("length past end")
            v, i = b[i:i + n], i + n
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.append((num, wt, v))
    return out


def _s(v):
    return v.decode("utf-8", "replace")


def _camel(name):
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _message(b, prefix, into):
    """A DescriptorProto, and the messages nested in it, into `into` by full name."""
    name, fields, nested = "", [], []
    for num, wt, v in _fields(b):
        if num == 1 and wt == 2:
            name = _s(v)
        elif num == 2 and wt == 2:
            f = {"name": "", "json": "", "type": 0, "type_name": "", "repeated": False}
            for n2, w2, v2 in _fields(v):
                if n2 == 1 and w2 == 2:
                    f["name"] = _s(v2)
                elif n2 == 4 and w2 == 0:
                    f["repeated"] = v2 == 3
                elif n2 == 5 and w2 == 0:
                    f["type"] = v2
                elif n2 == 6 and w2 == 2:
                    f["type_name"] = _s(v2)
                elif n2 == 10 and w2 == 2:
                    f["json"] = _s(v2)
            f["json"] = f["json"] or _camel(f["name"])
            fields.append(f)
        elif num == 3 and wt == 2:
            nested.append(v)
    full = f"{prefix}.{name}" if prefix else name
    into[full] = fields
    for nb in nested:
        _message(nb, full, into)


def file_descriptor(raw):
    """A base64 FileDescriptorProto as {name, package, services, messages}, or None if it is not one."""
    try:
        b = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
        top = _fields(b)
    except Exception:                                      # noqa: BLE001, most base64 is not a descriptor
        return None
    fd = {"name": "", "package": "", "services": [], "messages": {}}
    try:
        for num, wt, v in top:
            if num == 1 and wt == 2:
                fd["name"] = _s(v)
            elif num == 2 and wt == 2:
                fd["package"] = _s(v)
        if not fd["name"].endswith(".proto"):
            return None
        for num, wt, v in top:
            if num == 4 and wt == 2:
                _message(v, fd["package"], fd["messages"])
            elif num == 6 and wt == 2:
                svc = {"name": "", "methods": []}
                for n2, w2, v2 in _fields(v):
                    if n2 == 1 and w2 == 2:
                        svc["name"] = _s(v2)
                    elif n2 == 2 and w2 == 2:
                        m = {"name": "", "input": "", "output": "", "streaming": False}
                        for n3, w3, v3 in _fields(v2):
                            if n3 == 1 and w3 == 2:
                                m["name"] = _s(v3)
                            elif n3 == 2 and w3 == 2:
                                m["input"] = _s(v3).lstrip(".")
                            elif n3 == 3 and w3 == 2:
                                m["output"] = _s(v3).lstrip(".")
                            elif n3 in (5, 6) and w3 == 0 and v3:
                                m["streaming"] = True
                        svc["methods"].append(m)
                fd["services"].append(svc)
    except Exception:                                      # noqa: BLE001
        return None
    return fd


def _shape(full, messages, depth=0):
    """A request message as the JSON body Connect would send: field name -> what it holds."""
    out = {}
    for f in messages.get(full, []):
        if f["type"] == 11:
            inner = f["type_name"].lstrip(".")
            label = _shape(inner, messages, depth + 1) if depth < 2 and inner in messages else inner.rsplit(".", 1)[-1]
        elif f["type"] == 14:
            label = "enum:" + f["type_name"].lstrip(".").rsplit(".", 1)[-1]
        else:
            label = _TYPES.get(f["type"], "?")
        out[f["json"]] = [label] if f["repeated"] else label
    return out


def _close(src, i):
    """The index of the bracket closing the one at `src[i]`, skipping strings, or -1."""
    depth, j = 0, i
    while j < len(src) and j - i < 400_000:
        c = src[j]
        if c in "\"'`":
            q, j = c, j + 1
            while j < len(src) and src[j] != q:
                j += 2 if src[j] == "\\" else 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if not depth:
                return j
        j += 1
    return -1


def _opening(src, at):
    """The index of the `{` that position `at` sits directly inside, or -1."""
    depth, i = 0, at
    while i > 0 and at - i < 4000:
        i -= 1
        c = src[i]
        if c in ")]}":
            depth += 1
        elif c in "([{":
            if not depth:
                return i if c == "{" else -1
            depth -= 1
    return -1


def _v1_classes(src):
    """{class name: [(where, message type)]} for the v1 message classes in this file.

    A minified name is reused by every module in the file, so a name means the class declared
    nearest BEFORE the reference: on Dropbox, `I: l` in the Stormcrow service is the `l` declared
    2,121 characters earlier, StreamAssignmentsRequest, and not the eleven other `l`s, the last of
    which is google.protobuf.FloatValue."""
    out, marks = {}, [(m.start(), m.group(1)) for m in _V1_CLASS.finditer(src)]
    for k, (at, name) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else len(src)
        m = _V1_MESSAGE.search(src, at, min(stop, at + 4000))
        if m:
            out.setdefault(name, []).append((at, m.group(1)))
    return out


def _resolve(classes, name, at):
    """The message type of the class `name` declared nearest before position `at`, or None."""
    best = None
    for pos, typ in classes.get(name, ()):
        if pos < at:
            best = typ
    return best


def _v1_messages(src, classes):
    """{message type: {json field name: label}} from v1's `newFieldList(() => [...])` classes."""
    out = {}
    for m in _V1_MESSAGE.finditer(src):
        f = _V1_FIELDS.search(src, m.end(), m.end() + 600)
        if not f:
            continue
        end = _close(src, f.end() - 1)
        if end < 0:
            continue
        body, j = {}, f.end()
        while j < end:
            if src[j] != "{":
                j += 1
                continue
            k = _close(src, j)
            if k < 0:
                break
            obj = src[j + 1:k]
            j = k + 1
            name = _V1_NAME.search(obj)
            if not name:
                continue
            kind = re.search(r'\bkind\s*:\s*["\'`](\w+)', obj)
            kind = kind.group(1) if kind else ""
            if kind == "scalar":
                t = re.search(r'\bT\s*:\s*(\d+)', obj)
                label = _TYPES.get(int(t.group(1)), "?") if t else "?"
            elif kind == "message":
                t = re.search(r'\bT\s*:\s*([\w$]+)', obj)
                label = ((t and _resolve(classes, t.group(1), m.start())) or "message").rsplit(".", 1)[-1]
            else:
                label = kind or "?"
            js = re.search(r'\bjsonName\s*:\s*["\'`](\w+)', obj)
            key = js.group(1) if js else _camel(name.group(1))
            body[key] = [label] if re.search(r'\brepeated\s*:\s*(!0|true)', obj) else label
        out.setdefault(m.group(1), body)
    return out


def _v1_services(src, classes):
    """v1's plain service objects: [(service, own base or None, [(method, streaming, request type)])]."""
    out = []
    for m in _V1_SERVICE.finditer(src):
        if m.group(1).startswith("google.protobuf."):
            continue                                       # the runtime's own types
        start = _opening(src, m.start())
        end = _close(src, start) if start >= 0 else -1
        if end < 0:
            continue
        obj = src[start:end + 1]
        meths = re.search(r'\bmethods\s*:\s*\{', obj)
        if not meths:
            continue
        mend = _close(obj, meths.end() - 1)
        if mend < 0:
            continue
        own = _V1_OWN_BASE.search(obj[:meths.start()] + obj[mend:])
        pkg = m.group(1).rsplit(".", 1)[0]
        methods, j = [], meths.end()
        while j < mend:
            if obj[j] != "{":
                j += 1
                continue
            k = _close(obj, j)
            if k < 0:
                break
            mo, j = obj[j + 1:k], k + 1
            name = _V1_NAME.search(mo)
            if not name or not name.group(1)[:1].isupper():
                continue
            kind = _V1_KIND.search(mo)
            streaming = bool(kind) and (kind.group(1) or kind.group(2)) not in ("Unary", "0")
            inp = _V1_INPUT.search(mo)
            req = _resolve(classes, inp.group(1), m.start()) if inp else None
            methods.append((name.group(1), streaming, req or f"{pkg}.{name.group(1)}Request"))
        if methods:
            out.append((m.group(1), _slots(own.group(1).rstrip("/")) if own else None, methods))
    return out


# The options a Connect, gRPC-web or gRPC transport takes. Minification renames the factory, so the
# options are what is left to know a transport by: connectrpc.com/demo/ builds its client with
# `Ea({baseUrl: "https://demo..."})`, and nothing near that call says "transport" any more.
# Pagefind's `{element, baseUrl, bundlePath, ...}` on the same page holds keys no transport takes.
_TRANSPORT_KEYS = {"baseUrl", "useBinaryFormat", "interceptors", "fetch", "defaultTimeoutMs",
                   "jsonOptions", "binaryOptions", "useHttpGet", "credentials", "httpVersion",
                   "nodeOptions", "acceptCompression", "sendCompression", "compressMinBytes",
                   "readMaxBytes", "writeMaxBytes"}
_TRANSPORT_NEAR = re.compile(r"useBinaryFormat|interceptors|ConnectTransport|GrpcWebTransport")
# Code that is Connect's own runtime, wherever the bundler put it.
_RUNTIME = re.compile(r"useBinaryFormat|[Cc]onnect-[Pp]rotocol-[Vv]ersion|application/connect\+")
_KEY = re.compile(r"[A-Za-z_$][\w$]*")


def _object_keys(src, at):
    """(keys, complete) for the object literal that position `at` sits in, or None when there is no
    enclosing `{` close by. Not complete when it spreads another object, whose keys could be
    anything, though the keys it does show still count."""
    i = _opening(src, at)
    if i < 0:
        return None
    keys, complete, depth, j, expect = set(), True, 0, i + 1, True
    while j < len(src) and j - i < 4000:
        c = src[j]
        if c in "\"'`":
            q, j = c, j + 1
            while j < len(src) and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            j += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if not depth:
                return keys, complete
            depth -= 1
        elif depth == 0:
            if c == ",":
                expect = True
            elif expect and not c.isspace():
                expect = False
                if src.startswith("...", j):
                    complete = False
                    j += 3
                    continue
                m = _KEY.match(src, j)
                if m:
                    keys.add(m.group(0))
                    j = m.end()
                    continue
        j += 1
    return None


def _slots(base):
    """A template literal base, written the way jshound writes slots: the page's own origin is
    dropped, so the rest resolves against the page as the browser resolves it; `${a.b}` is `{b}`;
    a bare `${n}` past the start is `{param_n}`."""
    if "${" not in base:
        return base

    def one(m):
        expr = m.group(1).strip()
        if m.start() == 0 and re.fullmatch(r"(?:(?:window|self|globalThis)\.)?location\.origin", expr):
            return ""
        ids = _KEY.findall(expr)
        name = ids[-1] if ids else "param"
        return "{" + name + "}" if m.start() == 0 or "." in expr else "{param_" + name + "}"
    return re.sub(r"\$\{([^{}]*)\}", one, base)


def _transports(src):
    """[(base URL, binary or None when unsaid)] for each Connect transport `src` builds with a literal
    `baseUrl`.

    The options object decides first: a key no transport takes means it is something else, whatever
    is written beside it. Every key a transport's is still not enough on its own, because
    openapi-fetch takes `{baseUrl, fetch}` too: Lovable's `{baseUrl: `${Pe}/projects/...`, fetch}`
    is one, in a file with no Connect code in it. So it counts when a transport keyword is beside it
    or the file carries Connect's runtime or a service descriptor. An object that could not be read,
    or spreads in keys it does not show, counts on the keyword alone."""
    out = []
    connect_file = None
    for m in _BASE_URL.finditer(src):
        near = bool(_TRANSPORT_NEAR.search(src, max(0, m.start() - 400), m.end() + 400))
        seen = _object_keys(src, m.start())
        if seen is not None and not seen[0] <= _TRANSPORT_KEYS:
            continue
        if not near:
            if seen is None or not seen[1]:
                continue
            if connect_file is None:
                connect_file = bool(_RUNTIME.search(src) or _DESCRIPTOR.search(src)
                                    or _V1_SERVICE.search(src))
            if not connect_file:
                continue
        window = src[max(0, m.start() - 400): m.end() + 400]
        said = re.search(r"useBinaryFormat\s*:\s*(!0|!1|true|false)", window)
        out.append((m.group(1).rstrip("/"), said.group(1) in ("!0", "true") if said else None))
    return out


def scan(files):
    """Endpoint records for every RPC method the given JavaScript files are generated for."""
    descriptors, transports, v1, v1_messages, runtimes = {}, [], {}, {}, set()
    for fp in files:
        try:
            src = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _DESCRIPTOR.finditer(src):
            fd = file_descriptor(m.group(1))
            if fd:
                descriptors.setdefault(fd["name"], (fd, fp))
        if "baseUrl" in src:
            transports += _transports(src)
        if "typeName" in src:
            classes = _v1_classes(src)
            for name, own, methods in _v1_services(src, classes):
                v1.setdefault(name, (own, methods, fp))
            for name, body in _v1_messages(src, classes).items():
                v1_messages.setdefault(name, body)
        if "application/grpc-web" in src:
            runtimes.add("grpc-web")
        if "application/connect+" in src:
            runtimes.add("connect")
    if not descriptors and not v1:
        return []
    grpc_web = runtimes == {"grpc-web"}
    # The base is filled in only when it is the one the bundle names. Two different ones, and which
    # client uses which is not something a string search can tell, so it is left as a slot.
    bases = {b for b, _ in transports}
    base = _slots(bases.pop()) if len(bases) == 1 else "{connect}"
    # Binary when the transport says so. Unsaid, each protocol's own default: JSON for Connect,
    # protobuf for gRPC-web.
    stated = [bin_ for b, bin_ in transports if _slots(b) == base and bin_ is not None]
    binary = all(stated) if stated else grpc_web
    messages = {}
    for fd, _fp in descriptors.values():
        messages.update(fd["messages"])

    def record(service, method, streaming, body, fp, basis, at=None):
        rec = {"_kind": "endpoint", "sink": "connect-descriptor", "kind": "rpc", "method": "POST",
               "template": f"{at or base}/{service}/{method}", "op": method,
               "rpc": {"service": service, "method": method, "streaming": streaming},
               "body_encoding_basis": basis, "_file": fp}
        if grpc_web:
            # Every gRPC-web call, unary or not, is one enveloped message of this type.
            rec["content_type"] = "application/grpc-web+proto" if binary else "application/grpc-web+json"
            rec["req_headers"] = {"x-grpc-web": "1"}
        else:
            if binary:
                rec["content_type"] = "application/connect+proto" if streaming else "application/proto"
            else:
                rec["content_type"] = "application/connect+json" if streaming else "application/json"
            if not streaming:
                rec["req_headers"] = {"connect-protocol-version": "1"}
        if body:
            rec["body_shape"] = body
            rec["body_keys"] = list(body)
        return rec

    out = []
    for fd, fp in sorted(descriptors.values(), key=lambda x: x[0]["name"]):
        for svc in fd["services"]:
            full = f"{fd['package']}.{svc['name']}" if fd["package"] else svc["name"]
            for m in svc["methods"]:
                out.append(record(full, m["name"], m["streaming"], _shape(m["input"], messages), fp,
                                  "read from the service's descriptor; content type from the transport"))
    seen = {r["rpc"]["service"] for r in out}
    for name, (own, methods, fp) in sorted(v1.items()):
        if name in seen:
            continue
        for meth, streaming, req in methods:
            out.append(record(name, meth, streaming, v1_messages.get(req), fp,
                              "read from the service object; content type from the transport", own))
    return out


def scan_corpus(corpus):
    """scan() over every script file under a corpus directory."""
    files = []
    for root, _dirs, names in os.walk(corpus):
        for n in sorted(names):
            if n.endswith((".js", ".mjs", ".cjs")):
                files.append(os.path.join(root, n))
    return scan(files)
