"""GraphQL operation recovery. On a GraphQL app there is one path and the surface is the operation
set, so these cover both halves: finding real documents, and not inventing them out of JavaScript
that happens to read like one."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.graphqlops import extract   # noqa: E402


def test_operation_variables_keep_their_graphql_types():
    """A mutation taking `$orgId: ID!` is an authorization test waiting to be written, and the type
    is the part minification cannot destroy."""
    ops = extract('const M=`mutation UpdateProfile($input: ProfileInput!, $dry: Boolean) '
                  '{ updateProfile(input: $input) { id } }`;')
    assert len(ops) == 1
    o = ops[0]
    assert (o["operation_type"], o["operation"]) == ("mutation", "UpdateProfile")
    assert o["variables"] == [
        {"name": "input", "type": "ProfileInput!", "required": True},
        {"name": "dry", "type": "Boolean", "required": False}]
    assert o["selection"] == ["updateProfile"]


def test_escaped_newlines_inside_a_string_literal():
    """Documents reach a bundle inside string literals, where a newline is the two characters \\ and
    n. Splitting on real newlines saw one long line and matched no field, silently dropping three of
    four operations on a real target."""
    src = r'var q = "query PartnerOfferDetails($slug: String!) {\n  partnerOfferDetails(slug: $slug) {\n    token\n  }\n}";'
    ops = extract(src)
    assert len(ops) == 1 and ops[0]["selection"] == ["partnerOfferDetails"]


def test_javascript_that_reads_like_graphql_is_not_an_operation():
    """`query by(...){...}` is a method named `by`; both of these came out of real bundles as
    operations before the guards existed."""
    assert extract("const r = x.query by(a) { return a }") == []
    assert extract("subscription for (let i=0;i<3;i++) { go() }") == []


def test_anonymous_and_empty_documents_are_skipped():
    assert extract("const q = `{ me { id } }`") == []
    assert extract("const q = `query Nothing { }`") == []


def test_several_operations_in_one_file_and_duplicates_collapse():
    src = ("`query A($i: ID!){ a { x } }` `mutation B($j: Int){ b { y } }` "
           "`query A($i: ID!){ a { x } }`")
    ops = extract(src)
    assert [(o["operation_type"], o["operation"]) for o in ops] == [("query", "A"), ("mutation", "B")]


def test_har_export_is_replayable():
    """A list of operation names is a finding you still have to hand-translate into requests. The
    document and the variable types are already recovered, so the request can be built."""
    import json

    from jshound.graphqlops import to_har
    ops = extract('const M=`mutation GrantCredits($workspaceId: UUID!, $amount: Float!) '
                  '{ grantCredits(workspaceId: $workspaceId) { ok } }`;')
    doc = to_har(ops, "https://api.example.com/graphql")
    assert doc["log"]["version"] == "1.2"
    e = doc["log"]["entries"][0]
    assert e["request"]["method"] == "POST"
    assert e["request"]["url"] == "https://api.example.com/graphql"
    assert e["response"]["status"] == 0          # never claim a response happened
    body = json.loads(e["request"]["postData"]["text"])
    assert body["operationName"] == "GrantCredits"
    assert "grantCredits" in body["query"]
    # the variable skeleton shows the TYPE, so a replay knows what the slot wants
    assert body["variables"] == {"workspaceId": "UUID!", "amount": "Float!"}


def test_document_is_captured_for_replay():
    ops = extract('const Q=`query A($i: ID!){ a { x } }`;')
    assert "query A($i: ID!)" in ops[0]["document"]
