"""What may be borrowed from the cross-file property index.

The index is what lets `c.baseUrl` resolve when the assignment is in another module. It is also how one
object literal fabricated twenty addresses: Grafana's `{type: "mixed", uid: "-- Mixed --"}` was indexed
as the value of `uid`, and every `${x.uid}` slot in the bundle took it.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "borrowed-value")


def _templates():
    r = subprocess.run([sys.executable, "-m", "jshound.cli", "analyze", FIX],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-900:]
    recs = json.loads(r.stdout)
    assert recs, "analyzed nothing"
    return {str(x.get("template")) for x in recs if x.get("sink")}


def test_a_sentinel_from_another_module_is_not_substituted_into_a_path():
    """`-- Mixed --` is a UI label. A raw space cannot appear in a URL path, so the value cannot be an
    address however distinctive the property name is."""
    got = _templates()
    assert not any("Mixed" in t for t in got), got


def test_the_named_slot_survives_instead():
    """Refusing the substitution has to leave the endpoint, not drop it — the honest answer is `{uid}`,
    which is also the correct template. On Grafana this is why endpoints went up, not down."""
    got = _templates()
    assert "/api/datasources/uid/{uid}/health" in got
    assert "/api/datasources/uid/{uid}/lbac/teams" in got
