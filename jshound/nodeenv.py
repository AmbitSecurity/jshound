"""The environment the node helpers run with.

These helpers are handed JavaScript off the open internet. An allowlist is cheaper than a reason why
each variable is harmless, so `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` and the rest of
a developer's shell simply are not in scope for a process whose whole job is parsing a stranger's
bundle.

What stays, and why each is load-bearing:

  PATH            `execvpe` resolves `node` out of the environment it is GIVEN, not the parent's.
  NODE_PATH       how the Babel packages are found. Deliberately not defaulted here: every caller
                  falls back to its own repo-local `node_modules` with `setdefault`, and filling it
                  in here would shadow that with `/usr/local/lib/node_modules`.
  NODE_OPTIONS    the user's own node configuration.
  HOME, TMPDIR    node's own temp and cache resolution.
  LANG, LC_*      decoding of the paths it is handed.
  JSHOUND_*, BP_* the debug switches `static_ast.js` reads.
"""
import os

_KEEP = (
    "PATH", "NODE_PATH", "NODE_OPTIONS",
    "HOME", "TMPDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "LC_CTYPE",
    "JSHOUND_OPTS_DEBUG", "JSHOUND_BODY_DEBUG", "BP_DEBUG",
)


def node_env():
    """A fresh dict of the allowed variables. Callers add NODE_PATH as they already do."""
    return {k: os.environ[k] for k in _KEEP if k in os.environ}


def node_failure(r, need_output=False):
    """A one-line reason when a node helper did not succeed, else None.

    `json.loads(r.stdout or b"{}")` was the shape at every one of these call sites, and it turns a
    crashed helper into an empty result rather than an error: a missing `@babel/parser` exits 1 with
    nothing on stdout, and was reported as an app whose build declares no routes. The exit code is
    the only thing that separates those two, so it is read here rather than at five call sites that
    each forgot. `need_output` additionally rejects a clean exit that printed nothing.
    """
    out = r.stdout or b""
    if isinstance(out, str):
        out = out.encode()
    if r.returncode == 0 and not (need_output and not out.strip()):
        return None
    err = r.stderr or b""
    if isinstance(err, bytes):
        err = err.decode(errors="replace")
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    # Node prints the throw, then a stack, then its own version banner, so the last line is the
    # banner and the useful one is the first that names an error.
    why = next((ln for ln in lines if "Error" in ln or "Cannot find module" in ln),
               lines[-1] if lines else f"node exited {r.returncode} without output")
    if "Cannot find module" in err:
        why += "  (run `npm install` in the jshound checkout)"
    return why
