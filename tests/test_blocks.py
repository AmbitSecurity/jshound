"""Telling a refusal from a page, and getting past one where an HTTP client can.

On 2026-10-08, from one VPN exit, the old default (`impersonate="chrome"`, then Chrome 142) was refused
by claude.ai, lovable.dev and tesla.com, and Safari 17 got into all sixteen sites measured. A day later
the same IP had every one of those profiles challenged by claude.ai, while curl_cffi's newest Chrome
(150) got the real page 11 times in 12. What gets through moves, and a current browser version is
what held up, so the profiles are curl_cffi's newest rather than a list written down here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound.fetch as F   # noqa: E402

CF = ('<html><head><title>Just a moment...</title></head><body>'
      '<script>window._cf_chl_opt={cvId:"3"}</script>'
      '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>')
AKAMAI = ('<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don\'t have permission. '
          'Reference&#32;&#35;18&#46;8b24c317 https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18'
          '</BODY></HTML>').replace("&#46;", ".").replace("&#58;&#47;&#47;", "://").replace("&#47;", "/")
# Trimmed from what tesla.com served on 2026-10-08, with a 200, to a client that got past the 403s.
AKAMAI_CHALLENGE = ('<!DOCTYPE html><html><body><script type="text/javascript" src="/sKGwF/7Iby/SykW/'
                    'akXG/YXK/S0LaX4La9cOi/PWEJAg/ez8EBQh/EBBsV?v=ffe9&amp;t=732532568"></script>'
                    '<div id="sec-if-cpt-container" role="main" style="display: none">'
                    '<div class="behavioral-content"><div id="sec-bc-text-container"></div></div>'
                    '<p class ="scf-akamai-protected-by">Powered and protected by</p></div>'
                    '</body></html>')
REAL = "<html><head><title>App</title></head><body>" + "<p>x</p>" * 3000 + \
       '<script src="/assets/app.js"></script></body></html>'


def test_a_cloudflare_challenge_is_a_refusal():
    assert F.blocked_by(403, CF) == "Cloudflare challenge"


def test_an_akamai_denial_is_a_refusal():
    assert F.blocked_by(403, AKAMAI) == "Akamai"


def test_an_akamai_challenge_is_a_refusal_even_with_a_200():
    """It answers 200, so the status says nothing. Reading its sensor scripts as the application
    reported tesla.com as an app with no requests instead of an app we were not shown."""
    assert F.blocked_by(200, AKAMAI_CHALLENGE) == "Akamai challenge"


def test_a_small_spa_shell_carrying_a_vendors_ordinary_script_is_not_a_refusal():
    """demo.home-assistant.io is a 6.5 KB shell, and Cloudflare injects its detection script into it.
    That script is on ordinary pages, so on its own it says nothing about being refused, and
    reading it as a challenge threw the whole app away. Same for DataDome's and HUMAN's tags."""
    shell = ('<!DOCTYPE html><html><head><title>Home Assistant Demo</title></head><body>'
             '<home-assistant></home-assistant><script src="/frontend_latest/app.js"></script>'
             '<script>(function(){var a=document.createElement("script");'
             'a.src="/cdn-cgi/challenge-platform/scripts/precursor/main.js";'
             'document.head.appendChild(a);})();</script></body></html>')
    assert F.blocked_by(200, shell) is None
    assert F.blocked_by(200, shell.replace("precursor/main.js", "x.js") +
                        '<script src="https://js.datadome.co/tags.js"></script>') is None
    assert F.blocked_by(200, shell + "<script>window._pxAppId='PX1';</script>") is None
    # ...while the same script on a refusal still names who refused
    assert F.blocked_by(403, shell) == "Cloudflare challenge"


def test_a_real_page_is_not_a_refusal():
    assert F.blocked_by(200, REAL) is None


def test_a_real_app_that_loads_a_vendors_script_is_not_a_refusal():
    """A marker only counts on an error or on a page the size of an interstitial. Plenty of real apps
    load Cloudflare's or DataDome's own scripts, and calling those blocked would throw them away."""
    assert F.blocked_by(200, REAL.replace("</body>", '<script src="/cdn-cgi/challenge-platform/x.js">'
                                                     "</script></body>")) is None


def test_the_profiles_are_curl_cffis_newest_chrome_firefox_and_safari():
    """Not a list in this file: a written-down list is how jshound ended up trying only browsers a
    year out of date, and every one of them was challenged."""
    import pytest
    imp = pytest.importorskip("curl_cffi.requests.impersonate")
    assert F.PROFILES == (imp.DEFAULT_CHROME, imp.DEFAULT_FIREFOX, imp.DEFAULT_SAFARI)


def test_a_block_names_the_profiles_it_tried(tmp_path, monkeypatch, capsys):
    """The run that failed on 2026-10-09 said "tried 5 client profiles". Naming them is what shows
    they are out of date, and the line says what brings newer ones."""
    from jshound import cli
    _wire(monkeypatch, refused_by=set(F.PROFILES))
    a = cli.build_parser().parse_args(["https://app.test/", "-o", str(tmp_path / "out"), "-q"])
    assert a.fn(a) == 1
    err = capsys.readouterr().err
    assert "blocked by Cloudflare challenge" in err
    assert "tried " + ", ".join(F.PROFILES) in err
    assert "pip install -U curl_cffi" in err


def _wire(monkeypatch, refused_by):
    """A target that refuses some profiles and serves the rest."""
    seen = []

    def fake(url, timeout=30, with_url=False, max_bytes=F.MAX_BODY):
        seen.append((F.IMPERSONATE, url))
        if url.endswith("/") and F.IMPERSONATE in refused_by:
            body, st = CF, 403
        elif url.endswith("/"):
            body, st = REAL, 200
        else:
            body, st = "var a=1;\nfetch('/v1/ping');\n", 200
        return (st, body, url) if with_url else (st, body)

    monkeypatch.setattr(F, "http_get", fake)
    monkeypatch.setattr(F, "host_is_private", lambda h: False)
    F.set_client(None)
    return seen


def test_a_refused_document_is_retried_with_the_next_profile(tmp_path, monkeypatch):
    seen = _wire(monkeypatch, refused_by={F.PROFILES[0]})
    rep = {}
    F.collect("https://app.test/", str(tmp_path), with_chunks=False, report=rep)
    assert rep["blocked"] is None and rep["status"] == 200
    assert rep["profile"] == F.PROFILES[1]
    assert rep["tried"] == list(F.PROFILES[:2])


def test_the_profile_that_got_in_is_kept_for_the_rest_of_the_target(tmp_path, monkeypatch):
    """A challenge cookie is bound to the client that earned it, and switching fingerprints
    mid-session is itself a signal, so every script after the document uses the same profile."""
    seen = _wire(monkeypatch, refused_by={F.PROFILES[0]})
    F.collect("https://app.test/", str(tmp_path), with_chunks=False, report={})
    after_document = [p for p, u in seen if not u.endswith("/")]
    assert after_document and set(after_document) == {F.PROFILES[1]}


def test_when_every_profile_is_refused_the_first_refusal_is_reported(tmp_path, monkeypatch):
    _wire(monkeypatch, refused_by=set(F.PROFILES))
    rep = {}
    assert F.collect("https://app.test/", str(tmp_path), with_chunks=False, report=rep) == 0
    assert rep["blocked"] == "Cloudflare challenge" and rep["status"] == 403
    assert len(rep["tried"]) == len(F.PROFILES)


def test_a_pinned_profile_is_not_rotated(tmp_path, monkeypatch):
    seen = _wire(monkeypatch, refused_by={"firefox144"})
    F.set_client("firefox144")
    rep = {}
    F.collect("https://app.test/", str(tmp_path), with_chunks=False, report=rep)
    F.set_client(None)
    assert rep["tried"] == ["firefox144"] and rep["blocked"]
