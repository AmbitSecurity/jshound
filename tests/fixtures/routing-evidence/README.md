# Hash routing or path routing — what counts as evidence

The distinction matters more than most fields here: a hash route never reaches the server, so it
answers 404 to any request, while a path route returns the app shell. Mislabelling path as hash turns
a map into a work list and makes someone skip real routes.

It used to be a regex over the chunk's source. Measured over 11 corpora holding 557 route records,
that produced exactly **two** labels — Grafana's and Slack's — and **both were wrong**. Each app is
path-routed and each was called hash-routed.

| directory | what it holds | expected |
|---|---|---|
| `app-calls-hash/` | the app imports `createHashRouter` and calls it | `hash` |
| `library-defines-both/` | react-router's shape: both history factories defined here and called from its own `HashRouter`/`BrowserRouter` bodies | no label |
| `name-in-string-and-export-map/` | Grafana's shape: `createHashRouter` only inside an error string, `HashRouter` only as an export-map entry | no label |
| `router-in-another-file/` | the router is constructed in `router.js`, the route table lives in `routes.js` | `hash` on both routes |

The rule is **who is calling**. A call whose callee binds to a function declared in the same file is
the library calling itself; a call whose callee is imported is code reaching into the router from
outside, and only that is evidence. A corpus with evidence for both sides reports nothing.

None of the 21 real corpora in this repo produce a positive, which is why the only true positive here
is a fixture — and why Grafana's single label disappearing is the measured result.
