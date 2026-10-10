"""Connect RPC services, read out of the descriptors the generated client ships.

A Connect call goes to `<baseUrl>/<package>.<Service>/<Method>`, a URL no client writes down: the
library builds it at call time from a service descriptor, and protobuf-es ships each descriptor as a
base64 serialised FileDescriptorProto. On claude.ai that hid about 150 methods, PerformAction's
service among them, from every string search. The descriptors here are encoded by hand, field by
field, the way protoc writes them, so the tests do not depend on a protobuf library.
"""
import base64
import json
import os
import subprocess
import sys

from jshound.rpc import file_descriptor, scan


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(num, payload):
    """A length-delimited field."""
    payload = payload.encode() if isinstance(payload, str) else payload
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _vi(num, value):
    return _varint(num << 3) + _varint(value)


def _field(name, number, ftype, type_name="", repeated=False):
    return _ld(2, _ld(1, name) + _vi(3, number) + _vi(4, 3 if repeated else 1) + _vi(5, ftype)
               + (_ld(6, type_name) if type_name else b""))


def _descriptor():
    """acme/v1/things.proto: ThingService with a unary GetThing and a server-streaming Watch."""
    filt = _ld(1, "Filter") + _field("tags", 1, 9, repeated=True)
    req = (_ld(1, "GetThingRequest") + _field("thing_id", 1, 9) + _field("limit", 2, 3)
           + _field("filter", 3, 11, ".acme.v1.GetThingRequest.Filter") + _field("kind", 4, 14, ".acme.v1.Kind")
           + _ld(3, filt))
    watch = _ld(1, "WatchRequest") + _field("org_uuid", 1, 9)
    svc = (_ld(1, "ThingService")
           + _ld(2, _ld(1, "GetThing") + _ld(2, ".acme.v1.GetThingRequest") + _ld(3, ".acme.v1.Thing"))
           + _ld(2, _ld(1, "Watch") + _ld(2, ".acme.v1.WatchRequest") + _ld(3, ".acme.v1.Event") + _vi(6, 1)))
    fd = _ld(1, "acme/v1/things.proto") + _ld(2, "acme.v1") + _ld(4, req) + _ld(4, watch) + _ld(6, svc)
    return base64.b64encode(fd).decode()


def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src)
    return str(p)


def test_a_descriptor_decodes_to_its_services_and_request_fields():
    fd = file_descriptor(_descriptor())
    assert fd["package"] == "acme.v1"
    [svc] = fd["services"]
    assert [(m["name"], m["streaming"]) for m in svc["methods"]] == [("GetThing", False), ("Watch", True)]
    assert "acme.v1.GetThingRequest.Filter" in fd["messages"]


def test_base64_that_is_not_a_descriptor_is_ignored():
    assert file_descriptor(base64.b64encode(b"\x0a\x05hello world, not a proto").decode()) is None
    assert file_descriptor("Cg" + "A" * 40) is None


def test_each_method_is_a_request_at_the_transports_base(tmp_path):
    """The claude.ai shape: binary protobuf, so unary methods are application/proto and carry
    Connect's protocol header, and streaming ones are application/connect+proto without it."""
    js = _write(tmp_path, "app.js",
                f'const f = fileDesc("{_descriptor()}", []);\n'
                'const t = createConnectTransport({baseUrl: "/rpc", useBinaryFormat: !0, interceptors: [a]});\n')
    recs = {r["op"]: r for r in scan([js])}
    get = recs["GetThing"]
    assert get["template"] == "/rpc/acme.v1.ThingService/GetThing"
    assert get["content_type"] == "application/proto"
    assert get["req_headers"] == {"connect-protocol-version": "1"}
    assert get["body_shape"] == {"thingId": "string", "limit": "int64",
                                 "filter": {"tags": ["string"]}, "kind": "enum:Kind"}
    watch = recs["Watch"]
    assert watch["content_type"] == "application/connect+proto" and "req_headers" not in watch
    assert watch["body_shape"] == {"orgUuid": "string"}


def test_a_minified_transport_is_known_by_its_options(tmp_path):
    """connectrpc.com/demo/ builds its client with `Ea({baseUrl: "https://demo.connectrpc.com"})`:
    minification renamed the factory, so only the options object says it is a transport. Pagefind's
    options on the same page hold a `baseUrl` too, among keys no transport takes."""
    js = _write(tmp_path, "app.js",
                f'const f = fileDesc("{_descriptor()}", []);\n'
                'function Ea(e){let t=e.useBinaryFormat??!1;return{unary(){}}}\n'
                'const c = Pi(svc, Ea({baseUrl: "https://api.acme.test"}));\n'
                'new PagefindUI({...e, element: "#search", baseUrl: "/", bundlePath: "/pagefind/"});\n')
    recs = scan([js])
    assert {r["template"] for r in recs} == {"https://api.acme.test/acme.v1.ThingService/GetThing",
                                             "https://api.acme.test/acme.v1.ThingService/Watch"}
    assert {r["content_type"] for r in recs} == {"application/json", "application/connect+json"}


def test_two_different_bases_are_left_as_a_slot(tmp_path):
    """Which client uses which transport is not something a string search can tell."""
    js = _write(tmp_path, "app.js",
                f'const f = fileDesc("{_descriptor()}", []);\n'
                'const a = createConnectTransport({baseUrl: "/one", useBinaryFormat: true});\n'
                'const b = createConnectTransport({baseUrl: "/two", useBinaryFormat: true});\n')
    assert {r["template"].split("/")[0] for r in scan([js])} == {"{connect}"}


def test_a_connect_es_v1_service_object_is_read(tmp_path):
    """Whatever the service is called: most of Dropbox's are `...ApiV2` or `...Client`. A message type
    has a `typeName` too, but never a `methods` object."""
    js = _write(tmp_path, "app.js",
                'const S = {typeName: "acme.v1.PingApiV2", methods: {ping: {name: "Ping", I: A, O: B, kind: 0},'
                ' sum: {name: "Sum", I: C, O: D, kind: 1}}};\n'
                'const M = {typeName: "acme.v1.PingRequest", fields: []};\n'
                'const G = {typeName: "google.protobuf.Struct", methods: {x: {name: "X"}}};\n')
    recs = {r["template"]: r["rpc"]["streaming"] for r in scan([js])}
    # `kind` as a bundler inlines MethodKind: 0 is unary, 1 server streaming.
    assert recs == {"{connect}/acme.v1.PingApiV2/Ping": False, "{connect}/acme.v1.PingApiV2/Sum": True}


def test_an_older_connect_web_service_over_grpc_web(tmp_path):
    """Dropbox's shape: `@bufbuild/connect-web` service objects with a base of their own between
    `typeName` and `methods`, message classes whose minified names repeat across modules, and
    gRPC-web as the only runtime."""
    js = _write(tmp_path, "app.js", (
        'class l extends r.Q{static{this.typeName="data_modules.Feature"}'
        'static{this.fields=s.C.util.newFieldList(()=>[{no:1,name:"feature_name",kind:"scalar",T:9}])}}\n'
        'class o extends r.Q{static{this.typeName="data_modules.Feature"}}\n'
        'class l extends r.Q{static{this.typeName="data_modules.StreamAssignmentsRequest"}'
        'static{this.fields=s.C.util.newFieldList(()=>[{no:1,name:"features",kind:"message",T:o,repeated:!0},'
        '{no:2,name:"labels",kind:"map",K:9,V:{kind:"scalar",T:9}}])}}\n'
        'let _={typeName:"data_modules.StormcrowService",grpcWebBaseUrl:"/web-grpc/edison/",methods:'
        '{streamAssignments:{name:"StreamAssignments",I:l,O:d,kind:n(89761).I.ServerStreaming},'
        'get:{name:"Get",I:x.v,O:d,kind:g.I.Unary}}};\n'
        'class l extends r.Q{static{this.typeName="google.protobuf.FloatValue"}'
        'static{this.fields=s.C.util.newFieldList(()=>[{no:1,name:"value",kind:"scalar",T:2}])}}\n'
        'n.set("Content-Type","application/grpc-web+proto"),n.set("X-Grpc-Web","1");\n'))
    recs = {r["op"]: r for r in scan([js])}
    stream = recs["StreamAssignments"]
    assert stream["template"] == "/web-grpc/edison/data_modules.StormcrowService/StreamAssignments"
    assert stream["rpc"]["streaming"] is True and recs["Get"]["rpc"]["streaming"] is False
    assert stream["body_shape"] == {"features": ["Feature"], "labels": "map"}
    assert {r["content_type"] for r in recs.values()} == {"application/grpc-web+proto"}
    assert stream["req_headers"] == {"x-grpc-web": "1"}


def test_the_cli_lists_them_and_once_only(tmp_path):
    """A method the app also calls at a literal URL is listed once, as the call site found it."""
    d = tmp_path / "js"
    d.mkdir()
    (d / "app.js").write_text(
        f'const f = fileDesc("{_descriptor()}", []);\n'
        'const t = createConnectTransport({baseUrl: "https://api.acme.test", useBinaryFormat: true});\n'
        'fetch("https://api.acme.test/acme.v1.ThingService/GetThing", {method: "POST"});\n')
    out = tmp_path / "out"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-m", "jshound.cli", str(d), "-o", str(out), "-q"],
                       capture_output=True, text=True, timeout=300, cwd=root)
    assert r.returncode == 0, r.stderr
    rows = [json.loads(l) for l in open(out / "requests.jsonl")]
    urls = [x["url"] for x in rows]
    assert urls.count("https://api.acme.test/acme.v1.ThingService/GetThing") == 1
    watch = next(x for x in rows if x["url"].endswith("/Watch"))
    assert watch["rpc"] == {"service": "acme.v1.ThingService", "method": "Watch", "streaming": True}
    assert watch["body"] == {"orgUuid": "string"}


def test_a_service_object_record_replaces_the_bare_one(tmp_path):
    """The extractor's own reading of a service object knows the method's name and nothing else, so
    the record read with its base, content type and fields takes its place rather than sitting beside
    it. A real call site is different: that is how the app calls the method, and it stays."""
    from jshound.cli import _with_rpc
    d = tmp_path / "js"
    d.mkdir()
    (d / "app.js").write_text(
        'class R extends M{static{this.typeName="acme.GetRequest"}'
        'static{this.fields=u.newFieldList(()=>[{no:1,name:"item_id",kind:"scalar",T:9}])}}\n'
        'const S={typeName:"acme.ItemsApi",grpcWebBaseUrl:"/web-grpc/",methods:{get:{name:"Get",I:R,O:Q,kind:0},'
        'put:{name:"Put",I:R,O:Q,kind:0}}};\n')
    bare = [{"_kind": "endpoint", "sink": "connect-rpc", "method": "POST", "template": "/acme.ItemsApi/Get"},
            {"_kind": "endpoint", "sink": "fetch", "method": "POST", "template": "{api}/acme.ItemsApi/Put",
             "body_keys": ["itemId"]}]
    out = _with_rpc(str(d), bare)
    assert [r["template"] for r in out] == ["{api}/acme.ItemsApi/Put", "/web-grpc/acme.ItemsApi/Get"]
    assert out[1]["body_shape"] == {"itemId": "string"}
