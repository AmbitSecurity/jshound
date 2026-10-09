"""tRPC and RTK Query generate the same call shape, and only one of them is a tRPC path. Inventing
an endpoint is worse than missing one — a missed endpoint costs coverage, an invented one sends
somebody at a URL that was never there."""
import json
import os
import subprocess
import sys
import _lib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "trpc-vs-rtk")


def _trpc():
    r = _lib.analyze(FIX)
    assert r.returncode == 0, r.stderr[-1200:]
    return {x.get("template") for x in json.loads(r.stdout) if x.get("sink") == "trpc"}


def test_real_trpc_chains_still_resolve():
    got = _trpc()
    for want in ("/api/trpc/alertRule.getById", "/api/trpc/alertRule.upsert",
                 "/api/trpc/workspace.members.list"):
        assert want in got, (want, got)


def test_rtk_query_hooks_are_not_trpc_endpoints():
    """Grafana produced 13 records at /api/trpc/hK.endpoints.<name> while '/api/trpc' appears zero
    times in its bundle."""
    got = _trpc()
    assert not [t for t in got if "endpoints" in t], got


def _endpoints(fixture):
    d = os.path.join(ROOT, "tests", "fixtures", fixture)
    r = _lib.analyze(d)
    assert r.returncode == 0, r.stderr[-1200:]
    return {str(x.get("template")) for x in json.loads(r.stdout)
            if (x.get("_kind") or "endpoint") == "endpoint"}


def test_a_date_format_is_not_a_path():
    """moment's locale table puts "MM/DD/YYYY" behind a key named L, and L.get(...) reached a
    client-verb sink — Notion produced an endpoint whose URL was a date format."""
    got = _endpoints("date-format-not-a-path")
    assert "MM/DD/YYYY" not in got and "YYYY-MM-DD HH:mm:ss" not in got, got


def test_real_paths_containing_date_tokens_survive():
    """Requiring a leading slash would have caught the format strings and also discarded these."""
    got = _endpoints("date-format-not-a-path")
    assert {"api/datasources/uid/x", "/api/v1/MM/items", "s3/multipart"} <= got, got


# ── CORROBORATION ───────────────────────────────────────────────────────────────────────────────
# This collector does not READ a URL, it manufactures one: `/api/trpc/` is a string in
# static_ast.js, not in the bundle being analysed. So it owes the bundle some evidence that tRPC is
# there at all, and what decides it is layered by how strong the evidence is — the bundle-level
# marker, then what built the chain's root, and only then the property-name blocklist that used to
# be the whole test.
CORR = os.path.join(ROOT, "tests", "fixtures", "trpc-corroboration")


def _trpc_in(case):
    """One case DIRECTORY at a time. `isTrpc` is computed over everything handed to the extractor at
    once, so analysing the no-tRPC case beside a real one would hand it the marker and the test would
    stop testing anything. `analyze` also takes a directory, not a file — pointing it at a file
    reports "0 scripts -> 0 records" and every assertion about an empty result passes vacuously."""
    d = os.path.join(CORR, case)
    assert os.path.isdir(d), d
    r = _lib.analyze(d)
    assert r.returncode == 0, r.stderr[-1200:]
    recs = json.loads(r.stdout)
    assert recs or case == "no-trpc", f"{case}: analyzed nothing"
    return {x.get("template") for x in recs if x.get("sink") == "trpc"}


def test_a_bundle_with_no_sign_of_trpc_yields_no_trpc_endpoints():
    """The general rule, and the one a name blocklist cannot express. This fixture holds RTK's
    `api.endpoints.X.useQuery()` AND `queries.workspace.members.useQuery()` — the same shape under a
    namespace that is not called `endpoints`. Measured before the rule: two fabricated endpoints,
    `/api/trpc/workspace.members` and `/api/trpc/billing.invoices`, in a file containing no tRPC.
    Grafana carries the marker in 0 of its 159 files and produced 13 records this way."""
    assert _trpc_in("no-trpc") == set()


def test_rtk_is_rejected_by_what_built_it_even_when_the_bundle_does_carry_the_marker():
    """A monorepo can ship both, so the marker alone cannot discriminate. `createApi({...})` and
    `<api>.injectEndpoints({...})` are RTK constructions; the tRPC call beside them still resolves."""
    got = _trpc_in("rtk-inject")
    assert got == {"/api/trpc/workspace.members.list"}
    assert not any("updateThing" in t for t in got)


def test_a_real_trpc_procedure_named_endpoints_is_not_thrown_away():
    """The false negative the blocklist cannot avoid: `endpoints` is a legal tRPC procedure name.
    A root proven to be `createTRPCReact()` skips the blocklist, so these survive."""
    assert _trpc_in("trpc-named-endpoints") == {
        "/api/trpc/admin.endpoints.list",
        "/api/trpc/admin.endpoints.create",
    }


def test_a_marker_that_appears_only_in_a_comment_is_not_evidence():
    """Comments are commentary, not code. The marker was a regex over raw source and could not tell
    the difference, so a file merely DISCUSSING `/api/trpc/` was treated as a tRPC app — which is how
    the fixture above first passed for the wrong reason. Asked of the AST instead, where a comment is
    not a node value."""
    assert _trpc_in("marker-in-a-comment") == set()


def test_the_verdict_is_a_property_of_the_corpus_not_of_a_file():
    """The regression this exists for, caught by measurement and not by any test at the time.

    The marker lives in ONE file of a real corpus — cal.com's is 1 of 79 — and the chains live in the
    others. Asking the question per file (or per batch, which is how the extractor is scheduled)
    rejected cal.com's 45 real tRPC endpoints down to 3. The verdict is settled once by the
    corpus-wide index pass, the same way the module index is, for the same reason: which files share
    a batch must not change the answer."""
    assert _trpc_in("marker-in-another-file") == {
        "/api/trpc/viewer.bookings.list",
        "/api/trpc/viewer.bookings.create",
    }
