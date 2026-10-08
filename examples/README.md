# examples/demo-app

A tiny webpack-shaped bundle that exercises the things string extractors cannot do. Nothing here
touches the network, so you can run it in thirty seconds and check the claims yourself.

```bash
jshound chunks examples/demo-app/app.4f2a9c1e.js
```

```json
{"publicPath": "/_assets/",
 "chunks": ["static/chunks/settings.a1b2c3d4.js",
            "static/chunks/billing.e5f6a7b8.js",
            "static/chunks/admin.c9d0e1f2.js"],
 "stats": {"builders": 1, "idsSeen": 3, "idsResolved": 3, "speculative": 0}}
```

**Grep the bundle for `settings.a1b2c3d4.js`. It is not there.** The runtime builds that URL from an
id-to-name map and an id-to-hash map at `import()` time, so the string only exists after the function
runs. jshound recovers the builder and runs it.

```bash
jshound analyze examples/demo-app --host demo-app.test -f table
```

```
GET    /api/feature-flags
POST   /t/events                  {"sid":"?","evt":"?"}
POST   /v1/media                  {"file":"file-upload"}                multipart/form-data
DELETE /v1/sessions/current
POST   /v1/subscribe              {"email":"computed:trim","pageUri":"state:href"}  application/json
GET    /v1/users/{param_o}/profile
```

Six endpoints across four different sinks (`fetch`, `XMLHttpRequest.open`, `navigator.sendBeacon`
and a `FormData` upload) with the verb on every one. Three of them are written in the source as
`` `${this.baseUrl}/v1/...` ``, where `baseUrl` is set in a *different module* from the call, so the
path only resolves by following the property across the module boundary.

Those same three have no ADDRESS, and `-f urls` says so by leaving them out:

```bash
jshound analyze examples/demo-app --host demo-app.test -f urls
```

```
DELETE https://demo-app.test/v1/sessions/current
GET https://demo-app.test/api/feature-flags
POST https://demo-app.test/t/events
```

The bundle assigns `apiBase: "https://api.demo-app.test"`, which is not the host the document is on.
So substituting `--host` over `{baseUrl}` would print three real-looking routes pointed at a server
the application never calls, which is the worst output this tool can produce. A URL list is a list of
things you can request; `-f table` above still shows all six, and `--include-unresolved` puts them in
the URL list with the slot visible.

And the contract, not just the path:

```bash
jshound analyze examples/demo-app --host demo-app.test -f openapi
```

```json
"/v1/subscribe": { "post": {
  "operationId": "subscribe",
  "requestBody": { "content": { "application/json": { "schema": {
    "type": "object",
    "properties": {
      "email":   { "type": "string", "description": "source: computed:trim" },
      "pageUri": { "type": "string", "description": "source: state:href" } } } } },
  "x-jshound": { "page": "https://demo-app.test/", "sink": "fetch",
                   "body_encoding_basis": "read from the call site",
                   "path_fully_resolved": true } } }
```

`/api/feature-flags?env=...` appears in the URL list as `/api/feature-flags`: OpenAPI forbids a
query string in a path key, so `env` is emitted as a query *parameter* instead. The HAR export
carries it in `queryString` for the same reason.

`operationId` is the client's own method name, read out of the bundle. Each property records where
the value comes from. The document validates as OpenAPI 3.1, asserted by a test, so it
imports into Burp's OpenAPI Parser or Caido directly.
