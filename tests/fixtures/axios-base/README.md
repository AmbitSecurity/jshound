# What counts as a client's base URL

A wrong base is the worst output this tool produces: a real-looking route pointed somewhere the
application never calls. So the rule has to be wrong in neither direction, and both directions have
cost real bugs.

| directory | what it holds | expected |
|---|---|---|
| `basepath-and-prefixurl/` | `{basePath: "https://svc.mycorp.io/v1"}`, `{prefixUrl: "https://edge.mycorp.io"}` | prefixed |
| `absolute-base/` | `{baseURL: "https://api.mycorp.io/v2"}` | prefixed |
| `relative-base-is-not-applied/` | `{baseURL: "/api/v2"}`, a real base, deliberately ignored | **not** prefixed |
| `generic-key-is-not-a-base/` | `{url: "/users"}`, `{uri: "/graphql"}`, in the key family, but they do not say "base" | **not** prefixed |
| `origin-under-a-non-base-key/` | amcharts' `{pathToImages: "http://www.amcharts.com/lib/3/images/"}` | **not** prefixed |
| `base-key-with-a-non-path/` | `{baseURL: "production"}`, `{baseURL: "?v=2"}` | **not** prefixed |

## What changed, and what was reverted

Two things were wrong. One is fixed; the other was fixed, measured, and put back.

**Fixed: the collector was gated on an origin literal appearing in the chunk**, on the reasoning that
a file with no origin "can carry no base". That gate also blocked a base built from a template
(`` `https://${host}/v1` `` has no `//host.tld` in its source). Its cost was measured before removing
it, Grafana's ten largest chunks, **93.4s gated vs 94.4s ungated for identical output**, about 1% and
inside the noise, because any chunk big enough to matter contains an origin literal anyway.

**Fixed: `basePath` and `prefixUrl` were not base keys at all.** The family matched `baseUrl`,
`apiBase`, `origin`, `endpoint` and friends but neither of those two, so an origin under either was
ignored.

**Reverted: accepting a RELATIVE base.** `axios.create({baseURL: "/api/v2"})` is a real base and this
tool does not apply it. Accepting it under an explicit base key fixed the fixture and made Grafana
worse, four records like

    /apis/dashboard.grafana.app/v0alpha1/namespaces/default/api/datasources/uid/{P}/correlations

appeared, gluing Grafana's k8s-style resource base onto its legacy REST API, and four correct records
(`DELETE /api/datasources/uid/{P}/correlations/{b}`) were displaced. The base is genuine and belongs
to an RTK Query client whose own paths are `/correlations`; it reached these calls because `applyBase`
looks a client up by receiver **name**, and a minified name collides across modules. An origin value
masks that weakness, a wrong origin is conspicuous and rare, while a relative one silently yields a
real-looking route pointed at the wrong API.

The real fix is to attribute a base by the receiver's **binding** rather than its name, the same way
`receiverIsNotHttpClient` resolves a receiver. Until that exists, this stays origin-only, and
`relative-base-is-not-applied/` pins the limitation so it is not changed back by accident.
