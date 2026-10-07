# A borrowed value has to be able to be a URL component

The cross-file property index answers "what value does this property usually hold", which is how
`c.baseUrl` resolves when the assignment is three modules away. `_GENERIC_PROP` already stops a value
being borrowed under a name too common to mean anything (`url`, `id`, `name`).

This fixture is the other half: a name that is distinctive enough, holding a value that is not an
address. Grafana defines its mixed-datasource sentinel as one object literal, `{type: "mixed",
uid: "-- Mixed --"}`, and the index then filled every `${x.uid}` slot in the bundle with it, **20
fabricated endpoints** like `/datasources/uid/-- Mixed --/health`, where the real path is
`/datasources/uid/{uid}/health`.

A raw space cannot appear in a URL path or query; it has to be percent-encoded. So a borrowed value
carrying whitespace is a label, not an address, and the named slot is the honest answer.

Measured across the 21 corpora: **27 templates held whitespace in their skeleton and all 27 were this
one sentinel**, no legitimate case to lose. On Grafana the fix removed those 27 and the correct
named-slot forms took their place: endpoints went 886 → 888.

The test is on the VALUE rather than another name added to `_GENERIC_PROP`, because the next sentinel
will have a different property name and the same shape.
