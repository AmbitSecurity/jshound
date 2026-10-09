"""The export is only useful if the consumer accepts it. Burp's OpenAPI Parser validates before it
imports, so an invalid document is not a cosmetic problem, it is the whole feature failing at the
last step. Skipped when the validator is not installed; CI installs it."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.export import openapi   # noqa: E402

validate = pytest.importorskip("openapi_spec_validator").validate

HARD = [
    {"method": "POST", "template": "{baseUrl}/v1/subscribe", "_host": "app.example.com",
     "op": "subscribe", "body_schema": {"email": "computed:trim", "n": "number"},
     "content_type": "application/json"},
    {"method": "GET", "template": "/api/flags?env={__ENV}&v=2", "_host": "app.example.com"},
    {"method": "GET", "template": "/v1/users/{EXPR}/profile", "_host": "app.example.com"},
    {"method": "POST", "template": "/v1/media", "_host": "app.example.com",
     "body_encoding": "multipart", "body_src": {"file": "file-upload"}},
    {"method": "DELETE", "template": "/v1/sessions/current", "_host": "app.example.com"},
    {"_kind": "route", "method": "GET", "template": "/src/x.ts", "_host": "app.example.com"},
]


def test_generated_spec_is_valid_openapi_31():
    validate(openapi.build(HARD, title="app.example.com"))


def test_empty_input_still_produces_a_valid_document():
    validate(openapi.build([]))
