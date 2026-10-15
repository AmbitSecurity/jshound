"""A request for a static file is not an API request.

A relative path naming an image, a font or a stylesheet was already turned away, but a full URL
skipped the check, so the benchmark reported Discord's `.mp4` CDN assets, HubSpot's
`bundle.production.js` and an `.svg` icon on claude.ai as endpoints.
"""
import jshound
from jshound.export.requests import to_lines


def test_static_files_are_not_api_requests(tmp_path):
    d = tmp_path / "js"
    d.mkdir()
    (d / "app.js").write_text(
        'fetch("https://cdn.example.test/assets/content/intro.mp4");'
        'fetch("https://app.example.test/static-1.2/bundle.production.js");'
        'fetch("https://app.example.test/images/icons/glyphs.svg");'
        'fetch("/static/map/rtl-text.js");'
        'fetch("https://api.example.test/v1/users",{method:"POST",body:JSON.stringify({a:1})});'
        'fetch("https://api.example.test/v1/export.json");')
    urls = {(r["method"], r["url"]) for r in to_lines(jshound.analyze(str(d)))}
    assert urls == {("POST", "https://api.example.test/v1/users"),
                    ("GET", "https://api.example.test/v1/export.json")}, urls


def test_a_route_naming_a_script_is_not_a_request():
    """HubSpot's route table lists its own `bundle.production.js` files."""
    from jshound.export.requests import requests_only
    recs = [{"_kind": "route", "template": "/head-dlb/static-1.5007/bundle.production.js"},
            {"_kind": "route", "template": "/settings/billing"}]
    assert [r["template"] for r in requests_only(recs)] == ["/settings/billing"]
