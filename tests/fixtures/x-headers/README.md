# Lowercase `x-` header names, and the DOM attribute that looks exactly like one

Header names are recognised by the NAME, because the receiver is whatever the minifier called it. That
rule rejects lowercase-dashed names so a CSS-ish map (`s["font-size"] = ...`) does not register as a
header, and it costs real ones. Surveyed across the corpora, **273 distinct `x-*` lowercase-dashed
keys** appear and nearly all are genuine: `x-grafana-alerting-folder-uid`, `x-algolia-api-key`,
`x-airtable-page-load-id`, `x-b3-traceid`, `x-2fa-code`, `x-sentry-rate-limits`.

Accepting them recovers `x-amz-user-agent` on Grafana and `x-deployment-id` on Clerk, and invents one
thing: Popper.js writes `e.attributes["x-out-of-boundaries"] = ""` onto a tooltip element. That is
markup, not a request header, and no test on the NAME can tell the difference.

So the receiver decides, and it can: `attributes`, `dataset`, `style` and `classList` are property
names fixed by the DOM, not chosen by the application, so a minifier cannot rename them, the same
reason this file anchors on wire constants everywhere else. A write into one of those is never a
header. Measured with the exclusion: **2 recovered, 0 invented.**

`x-axis-title` and `x-axis-dropzone` are also in these bundles and would match the name test, but they
never reach it, a chart config is an object literal and this path requires a member assignment.
