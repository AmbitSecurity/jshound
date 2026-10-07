# Server state serialised into an attribute

`_LOOKS_URL` rejects any value beginning `{` before any other gate, so a JSON **document** carried in
an attribute is read by nothing in this tool, and what it carries is routinely the application's
declared auth surface.

| shape | real example | what it declares |
|---|---|---|
| hidden `<input value>` | Datadog | `login_url`, `openid_login_url`, `forgot_password_url`, `recaptcha_login_url`, TOTP and MFA-recovery URLs, `allowed_login_methods`, a CSRF token, in a document with **zero** `<form>` tags |
| `data-props` | Slack | three OAuth entry points, `https://slack.com/signin/oauth/{apple,google,microsoft}/start?...` |
| `<meta content>` | Asana | its own `/-/oauth` endpoint and the IdP authorize URLs it accepts, in a 17 KB blob that `config_values()` drops because it caps meta content at 400 chars |

**The Slack case is why a parse is required rather than a search.** `grep -rF 'signin/oauth'` over that
corpus, the document and all 121 scripts, finds those URLs in **zero** files, because JSON escapes
`/` as `\/`. No text-based tool reaches them.

## Why the gate is the attribute NAME

Measured, not preferred. No test on the VALUE can separate serialised state from a rendered code
sample: deno.com puts a `deno.json` snippet in a `code` attribute whose `workspaces:
["./api","./core","./web"]` are directory names that satisfy every URL-shape test and are not
requests. Taking any JSON-valued attribute yields 71 leaves across 6 sites; the allowlist keeps 24,
and all 19 dropped are noise or navigation, GitLab's serialised sidebar (13), Hex's 100 KB of
LaunchDarkly flags (3 URLs, one already in the bundle), that deno code sample (3).

The noise cases in this fixture are each a real one from the corpora, and each must stay out.

## What these records are not

They are emitted as `doc-state` and deliberately **not** fed into the value-resolution map that
`--config` builds. That map is keyed by leaf property name and consumed bundle-wide as
`_CONFIG.get(node.property.name)`, so one wrong entry silently rewrites unrelated URLs, the failure
already recorded there, where a borrowed `url` put reddit's ActionCable socket at a Zendesk
documentation page. Simulated over these blobs it would add 246 keys, 25 of them ambiguous inside a
single document. Reporting a value is safe; resolving other code with it is not.

They reach the `urls` export, because a declared request target is what a URL list is for, and not the
OpenAPI export, where a path with no verb would have to invent one.
