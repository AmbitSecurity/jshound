"""An endpoint whose base did not resolve has no address, and must not be given one.

`{BASE_PATH}/api/incident-status` is a real endpoint. The analysis could not follow its base, so the
only honest answer to "what URL is that" is "unknown". The URL exporter used to answer it with the
host that served the FILE, and on supabase.com/dashboard the files come off a CDN that answers no
API call, so 45 endpoints were printed as `https://frontend-assets.supabase.com/{BASE_PATH}/api/...`.

A real-looking route pointed at a server the application never calls is the worst output this tool
produces, and it is worse than printing nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound                                   # noqa: E402
from jshound.export.paths import UNRESOLVED_ORIGIN, split_origin   # noqa: E402

CDN = [{"method": "GET", "template": "{BASE_PATH}/api/incident-status",
        "_host": "frontend-assets.supabase.com"}]


def test_a_leading_slot_is_an_unresolved_origin_not_a_relative_path():
    origin, path = split_origin("{BASE_PATH}/api/incident-status")
    assert origin == UNRESOLVED_ORIGIN
    assert path == "/api/incident-status"


def test_a_real_absolute_url_still_resolves():
    origin, path = split_origin("https://api.mycorp.io/v2/accounts")
    assert origin == "https://api.mycorp.io" and path == "/v2/accounts"


def test_a_genuinely_relative_path_is_still_relative():
    """`None` means "relative to the document", which is a different answer from "unknown"."""
    assert split_origin("/v1/ping") == (None, "/v1/ping")


def test_the_documents_own_origin_is_not_a_guess():
    """`{location.origin}` means this document's origin BY DEFINITION, so the document host is the
    right answer there rather than a substitution."""
    origin, path = split_origin("{location.origin}/v1/ping")
    assert origin and origin != UNRESOLVED_ORIGIN and path == "/v1/ping"


def test_an_assigned_base_is_not_the_documents_origin():
    """`{baseUrl}` is a value the app assigned, and the demo fixture assigns it to
    `https://api.demo-app.test`, which is not the host the document is on."""
    assert split_origin("{baseUrl}/v1/subscribe")[0] == UNRESOLVED_ORIGIN


def test_no_url_is_emitted_with_the_serving_host_as_the_base():
    assert jshound.to_urls(CDN, with_method=True) == []


def test_asking_for_them_shows_the_slot_rather_than_a_host():
    out = jshound.to_urls(CDN, with_method=True, include_unresolved=True)
    assert out == ["GET {?}/api/incident-status"]
    assert not [u for u in out if "frontend-assets" in u]


def test_the_har_entry_keeps_the_slot_visible():
    har = jshound.to_har(CDN)
    urls = [e["request"]["url"] for e in har["log"]["entries"]]
    assert not [u for u in urls if "frontend-assets" in u], urls
    assert not [u for u in urls if u.startswith("https://{")], urls
