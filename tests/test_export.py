"""Exporter tests. These cover the decisions that make an export safe to replay — the places where
a tidier-looking output would quietly become a dangerous one."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jshound.export import har, openapi, urls                      # noqa: E402
from jshound.export.paths import looks_like_source_file, normalize  # noqa: E402

REC = [
    {"sink": "fetch", "method": "POST", "template": "/api/seen/subscribe",
     "body_keys": ["email", "pageUri"], "body_encoding": "json",
     "body_schema": {"email": "computed:trim", "pageUri": "state:href"},
     "req_headers": {"content-type": "application/json"},
     "_host": "app.example.com", "_page": "https://app.example.com/pricing"},
    {"sink": "fetch", "method": "GET", "template": "/api/users/{EXPR}/posts",
     "_host": "app.example.com"},
    {"_kind": "route", "method": "GET", "template": "/src/foo.command-menu-item.ts",
     "_host": "app.example.com"},
    {"_kind": "zod", "name": "Sub", "zod_fields": [{"key": "email", "type": "string"},
                                                   {"key": "opt", "type": "number",
                                                    "optional": True}]},
]


def test_named_and_unresolved_placeholders_differ():
    path, params, resolved = normalize("/a/{user.id}/b/{EXPR}")
    assert path == "/a/{user_id}/b/{param1}"
    assert [p["resolved"] for p in params] == [True, False]
    assert resolved is False


def test_origin_is_lifted_out_of_the_path():
    assert normalize("{location.origin}/api/x")[0] == "/api/x"
    assert normalize("https://api.x.com/v2/{id}")[0] == "/v2/{id}"


def test_source_files_are_not_routes():
    assert looks_like_source_file("/src/foo.command-menu-item.ts")
    assert looks_like_source_file("/components/Button.tsx")
    assert not looks_like_source_file("/api/v1/users")


def test_openapi_drops_build_artifacts_but_keeps_endpoints():
    doc = openapi.build(REC)
    assert "/src/foo.command-menu-item.ts" not in doc["paths"]
    assert "/api/seen/subscribe" in doc["paths"]


def test_openapi_marks_unresolved_paths():
    doc = openapi.build(REC)
    op = doc["paths"]["/api/users/{param1}/posts"]["get"]
    assert op["x-jshound"]["path_fully_resolved"] is False
    assert "UNRESOLVED" in op["parameters"][0]["description"]
    # ...and the resolved one is not falsely flagged
    assert doc["paths"]["/api/seen/subscribe"]["post"]["x-jshound"]["path_fully_resolved"] is True


def test_openapi_carries_the_body_contract():
    body = openapi.build(REC)["paths"]["/api/seen/subscribe"]["post"]["requestBody"]
    assert "application/json" in body["content"]
    assert set(body["content"]["application/json"]["schema"]["properties"]) == {"email", "pageUri"}


def test_har_never_claims_a_response_happened():
    doc = har.build(REC)
    assert doc["log"]["version"] == "1.2"
    for e in doc["log"]["entries"]:
        assert e["response"]["status"] == 0
        assert e["_jshound"]["note"].startswith("recovered")


def test_har_entry_is_absolute_and_has_a_body():
    e = [x for x in har.build(REC)["log"]["entries"]
         if x["request"]["url"].endswith("/api/seen/subscribe")][0]
    assert e["request"]["method"] == "POST"
    assert "postData" in e["request"]
    assert ("content-type", "application/json") in [
        (h["name"], h["value"]) for h in e["request"]["headers"]]


def test_urls_omit_unresolved_unless_asked():
    assert urls.build(REC) == ["https://app.example.com/api/seen/subscribe"]
    assert any("{param1}" in u for u in urls.build(REC, include_unresolved=True))


def test_urls_with_method():
    assert "POST https://app.example.com/api/seen/subscribe" in urls.build(REC, with_method=True)


def test_provenance_never_lands_in_a_json_schema_type():
    """`computed:trim` says where a value came from. It is not a type, and writing it into one
    produces a document Burp's parser may reject outright."""
    rec = [{"method": "POST", "template": "/x", "_host": "h",
            "body_schema": {"email": "computed:trim", "n": "number"}}]
    props = openapi.build(rec)["paths"]["/x"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    valid = {"string", "number", "integer", "boolean", "object", "array", "null"}
    for p in props.values():
        assert p.get("type", "string") in valid
    assert props["email"]["description"] == "source: computed:trim"
    assert props["n"]["type"] == "number"


def test_query_string_never_appears_in_an_openapi_path():
    rec = [{"method": "GET", "template": "/api/flags?env={__ENV}&v=2", "_host": "h"}]
    doc = openapi.build(rec)
    assert list(doc["paths"]) == ["/api/flags"]
    names = {p["name"] for p in doc["paths"]["/api/flags"]["get"]["parameters"]}
    assert names == {"env", "v"}


def test_operation_id_prefers_the_clients_own_name():
    rec = [{"method": "POST", "template": "/v1/subscribe", "_host": "h", "op": "subscribe"}]
    assert openapi.build(rec)["paths"]["/v1/subscribe"]["post"]["operationId"] == "subscribe"


def test_rails_style_colon_params_become_placeholders():
    """`/api/:version/projects/:id` must not reach a spec with literal colons — `:id` would be
    requested as four characters. A colon only marks a slot when it owns the segment."""
    path, params, _ = normalize("/api/:version/projects/:id/releases/:tag_name")
    assert path == "/api/{version}/projects/{id}/releases/{tag_name}"
    assert [p["name"] for p in params] == ["version", "id", "tag_name"]


def test_google_style_custom_method_is_not_a_param():
    assert normalize("/v1/accounts:signUp")[0] == "/v1/accounts:signUp"


def test_operation_ids_are_unique_and_meaningful():
    """OpenAPI requires globally unique operationIds and Burp's parser enforces it — two operations
    named `onClick` is a document that fails to import at all."""
    rec = [{"method": "GET", "template": "/a", "_host": "h", "op": "onClick"},
           {"method": "GET", "template": "/b", "_host": "h", "op": "onClick"},
           {"method": "GET", "template": "/c", "_host": "h", "op": "listUsers"},
           {"method": "GET", "template": "/d", "_host": "h", "op": "listUsers"}]
    doc = openapi.build(rec)
    ids = [doc["paths"][p]["get"]["operationId"] for p in ("/a", "/b", "/c", "/d")]
    assert len(set(ids)) == 4, ids
    assert not any(i.startswith("onClick") for i in ids)      # event handler, not an operation
    assert ids[2] == "listUsers" and ids[3] == "listUsers_2"  # real name kept, collision suffixed


def test_ui_strings_substituted_into_a_path_become_slots():
    """Grafana's `-- Mixed --` is a dropdown placeholder that resolution dropped into 20 paths. No
    server routes on a segment containing a space, so the literal was the error and a slot is the
    correct recovery."""
    path, params, resolved = normalize("/cloudmigration/migration/-- Mixed --/snapshot")
    assert path == "/cloudmigration/migration/{param1}/snapshot"
    assert resolved is False and params[0]["resolved"] is False


def test_punctuation_only_segment_is_not_a_path_segment():
    assert normalize("/--/filterData")[0] == "/{param1}/filterData"


def test_normal_paths_are_untouched():
    for p in ("/api/v1/users", "/api/{version}/projects/{id}", "/a-b/c_d/e.json"):
        assert normalize(p)[0] == p


def test_an_opaque_body_is_not_a_bodyless_request():
    """RTK Query writes `body: $.check`, where `check` is a TypeScript type erased at compile time.
    The field names are genuinely not in the JavaScript, but the fact that a body is sent IS — and
    `body_keys: null` said both. Measured on Grafana: 485 body-bearing endpoints, 39 resolved, and
    the other 446 indistinguishable from sending nothing."""
    rec = [{"method": "POST", "template": "/checks", "_host": "h", "body_param": "$.check"},
           {"method": "POST", "template": "/rotate", "_host": "h"}]
    doc = openapi.build(rec)
    withbody = doc["paths"]["/checks"]["post"]
    assert "requestBody" in withbody
    assert "$.check" in withbody["requestBody"]["content"]["application/json"]["schema"]["description"]
    # ...and a genuinely bodyless POST still carries no schema claiming otherwise
    bodyless = doc["paths"]["/rotate"]["post"]
    assert "schema" not in bodyless.get("requestBody", {}).get("content", {}).get(
        "application/json", {})
