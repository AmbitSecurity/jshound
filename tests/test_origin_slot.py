"""A URL that starts with the page's own location is on the page's origin.

`${window.location.origin}/api/x` was read as a relative path and the page put in front of it,
`https://lovable.dev/{window_location_origin}/...`. A bare `{origin}` could be any object's
`.origin`, a parsed URL, a message event, so it is kept as written rather than filled in or glued on.
"""
from jshound.export.requests import _url, display_url

PAGE = "https://app.example.test/dash/home"


def _u(template, sink="fetch", page=PAGE):
    return _url({"template": template, "sink": sink, "_page": page})


def test_the_pages_origin_slot_is_the_pages_origin():
    assert _u("{window.location.origin}/api/x") == "https://app.example.test/api/x"
    assert _u("{location.origin}/api/x", sink="?.post") == "https://app.example.test/api/x"
    assert _u("{window.location.origin}{pathname}{search}") == "https://app.example.test{pathname}{search}"


def test_the_pages_href_slot_is_the_page():
    assert _u("{location.href}/export") == "https://app.example.test/dash/home/export"


def test_a_bare_origin_slot_stays_a_slot():
    assert _u("{origin}/v1/token") == "{origin}/v1/token"


def test_with_no_page_on_record_the_slot_stays():
    assert _u("{window.location.origin}/api/x", page="") == "{window.location.origin}/api/x"


def test_a_slot_where_the_origin_goes_is_shown():
    """hex.tech's `{origin}/orgs` was printed as a plain `/orgs`."""
    rec = {"template": "{origin}/orgs", "sink": "fetch", "_page": PAGE}
    assert display_url(rec) == "{origin}/orgs"
    rec = {"template": "{window.location.origin}/api/x", "sink": "?.get", "_page": PAGE}
    assert display_url(rec) == "https://app.example.test/api/x"
