"""Server state serialised into an HTML attribute.

`_LOOKS_URL` rejects any value beginning `{` before any other gate, so a JSON document in an attribute
reaches nothing else here — and on the corpora measured, what it carries is the application's declared
auth surface. Every noise case below is a real one from those corpora and must stay out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.analyze import doc_state_records          # noqa: E402
from jshound.export import urls as urls_export          # noqa: E402
from jshound.export import openapi as openapi_export    # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "doc-state")


def _recs():
    r = doc_state_records(FIX)
    assert r, "no records — the fixture or the collector is broken, and every assertion below would pass"
    return r


def _templates():
    return {x["template"] for x in _recs()}


# ---- the three carriers, each the one its framework actually uses ------------------------------

def test_a_hidden_input_declares_the_auth_surface():
    """Datadog's shape. The document has no `<form>` at all, so `markup_records`' hidden-field path
    never sees it, and `_LOOKS_URL` rejects the value for starting with a brace."""
    got = _templates()
    assert "/account/login?" in got
    assert "/account/oauth2?provider=google" in got
    assert "/account/forgot_password" in got
    assert "/account/login/recaptcha?" in got


def test_data_props_declares_oauth_entry_points():
    """Slack's shape, and the reason a parse is required rather than a search: in the real corpus
    `grep -rF 'signin/oauth'` finds these in ZERO files, because JSON escapes `/` as `\\/`."""
    got = _templates()
    assert "https://example.com/signin/oauth/apple/start?email_first=1" in got
    assert "https://example.com/signin/oauth/google/start?email_first=1" in got


def test_meta_content_is_read_whatever_its_length():
    """Asana's shape. `config_values()` does read `<meta>` but caps content at 400 chars, so a 17 KB
    login blob is dropped whole and never JSON-parsed."""
    assert "https://app.example.com/-/oauth" in _templates()


def test_the_json_path_is_kept_as_provenance():
    """`auth_settings.login_url` says where the value came from and what the application calls it,
    which is what makes the record readable instead of a loose URL with no account of itself."""
    by_t = {x["template"]: x["state_path"] for x in _recs()}
    assert by_t["/account/forgot_password"] == "auth_settings.forgot_password_url"
    assert by_t["https://app.example.com/-/oauth"] == \
        "window.loginConfig.oauthProviders.google.oauthActionUrl"


# ---- the noise, every case taken from a real corpus --------------------------------------------

def test_a_rendered_code_sample_is_not_surface():
    """deno.com renders a `deno.json` snippet into a `code` attribute. `./api`, `./core` and `./web`
    are workspace DIRECTORY names that satisfy every URL-shape test. This is why the gate is the
    attribute NAME: no test on the value could reject them."""
    got = _templates()
    assert not any(t in got for t in ("./api", "./core", "./web"))


def test_serialised_navigation_is_not_surface():
    """GitLab serialises its sidebar menu into `data-sidebar`. Following navigation is the crawler's
    channel, which `markup_records` already declines to duplicate."""
    assert not any("/explore/" in t for t in _templates())


def test_a_bare_word_in_an_allowlisted_attribute_is_not_a_document():
    """`data-state="closed"` is Radix UI component state. The attribute is allowlisted; the value has
    to parse as an object or an array, and a bare word is neither."""
    assert "closed" not in _templates()


def test_a_third_party_bundle_url_is_not_an_endpoint():
    """A `bundle_url` leaf is a CDN script. `.js` is excluded on this path only — elsewhere a `.js`
    URL is the thing this tool exists to fetch."""
    assert not any(t.endswith(".js") for t in _templates())


def test_a_bare_origin_is_not_an_endpoint():
    """Slack's `geoRootUrl` is `https://slack.com/`. The scoreboard refuses to count `/` as an
    endpoint and so does this."""
    assert "https://example.com/" not in _templates()


# ---- where the records are allowed to go -------------------------------------------------------

def test_they_reach_the_url_list_because_that_is_what_it_is_for():
    """A declared request target is exactly what a URL list consumes."""
    recs = [dict(r, _host="app.example.com") for r in _recs()]
    lines = urls_export.build(recs)
    assert any("/account/forgot_password" in x for x in lines), lines


def test_they_do_not_reach_openapi_because_they_carry_no_verb():
    """A path with no method would have to invent one, and a fabricated method is the thing this
    repo is most careful about."""
    spec = openapi_export.build([dict(r, _host="app.example.com") for r in _recs()])
    assert not any("forgot_password" in p for p in (spec.get("paths") or {})), spec.get("paths")
