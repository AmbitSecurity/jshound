"""What a target is allowed to make `fetch` do.

Only the first URL in a run is the user's. Script srcs, `import()` specifiers and every chunk URL
built out of a bundle's own runtime are named by the TARGET, and a chunk URL starting with `http` was
fetched verbatim — so a page naming the cloud metadata endpoint as one of its chunks had it read and
written into the corpus, from inside whoever's network ran the tool.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import jshound.fetch as F

DOC_URL = "https://app.example.com/"
DOC = ('<script src="https://app.example.com/main.js"></script>'
       '<script src="http://169.254.169.254/latest/meta-data/iam/security-credentials/"></script>')


@pytest.fixture
def asked(monkeypatch):
    """Every URL the fetcher tried, with the network replaced — and the resolver with it, so these
    assert the SCOPE RULE rather than how long an NXDOMAIN takes to come back. What
    `host_is_private` itself classifies is asserted separately, on literal addresses."""
    seen = []

    def fake_get(url, timeout=30, with_url=False, max_bytes=F.MAX_BODY):
        seen.append(url)
        body = DOC if url == DOC_URL else "var a = 1;\nfetch('/v1/thing');\n"
        return (200, body, url) if with_url else (200, body)

    monkeypatch.setattr(F, "http_get", fake_get)
    monkeypatch.setattr(F, "host_is_private", lambda h: h in {"169.254.169.254", "127.0.0.1"})
    return seen


def test_a_target_cannot_make_this_read_a_private_address(tmp_path, asked):
    F.collect(DOC_URL, str(tmp_path), with_chunks=False)
    assert "https://app.example.com/main.js" in asked          # the public one still read
    assert not [u for u in asked if "169.254" in u]


def test_allow_private_opts_back_in(tmp_path, asked):
    F.collect(DOC_URL, str(tmp_path), with_chunks=False, allow_private=True)
    assert [u for u in asked if "169.254" in u]


def test_a_private_target_puts_private_space_in_scope(tmp_path, monkeypatch):
    """Pointing this at a local or internal app is normal use, so the rule is about who CHOSE the
    address: when the target itself is private, nothing about the run is cross-boundary."""
    doc_url = "http://127.0.0.1:3000/"
    doc = '<script src="http://127.0.0.1:3000/main.js"></script>'
    seen = []

    def fake_get(url, timeout=30, with_url=False, max_bytes=F.MAX_BODY):
        seen.append(url)
        body = doc if url == doc_url else "var a = 1;\n"
        return (200, body, url) if with_url else (200, body)

    monkeypatch.setattr(F, "http_get", fake_get)
    monkeypatch.setattr(F, "host_is_private", lambda h: h in {"169.254.169.254", "127.0.0.1"})
    F.collect(doc_url, str(tmp_path), with_chunks=False)
    assert "http://127.0.0.1:3000/main.js" in seen


def test_the_refusal_is_reported_not_silent(tmp_path, asked, capsys):
    F.collect(DOC_URL, str(tmp_path), with_chunks=False)
    err = capsys.readouterr().err
    assert "refused 1 URL" in err and "169.254.169.254" in err and "--allow-private" in err


@pytest.mark.parametrize("host,private", [
    ("169.254.169.254", True),        # the cloud metadata endpoint on AWS, GCP and Azure alike
    ("127.0.0.1", True),
    ("10.1.2.3", True),
    ("192.168.1.1", True),
    ("172.16.0.1", True),
    ("::1", True),
    ("8.8.8.8", False),
    ("93.184.216.34", False),
])
def test_host_is_private_classifies_literal_addresses(host, private):
    """Literals only: `getaddrinfo` on an address does no DNS, so this asserts the classification
    without the suite depending on a resolver."""
    assert F.host_is_private(host) is private


# ---- how much of a response is read -------------------------------------------------------------

class _Endless(BaseHTTPRequestHandler):
    def do_GET(self):                                     # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.end_headers()
        block = b"x" * 65536
        try:
            for _ in range(400):                          # 26 MB if nothing stops it
                self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):                            # noqa: D102
        pass


def test_a_response_is_read_only_up_to_the_cap():
    """The size of a body is the target's choice. Uncapped, one URL took 1,032 MB of RSS and wrote
    524 MB into the corpus directory before anything looked at it."""
    srv = HTTPServer(("127.0.0.1", 0), _Endless)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/big.js"
        status, body = F.http_get(url, timeout=30, max_bytes=256 * 1024)
        assert status == 200
        assert 0 < len(body) <= 256 * 1024 + 65536        # the cap, plus at most one read block
    finally:
        srv.shutdown()
        srv.server_close()
