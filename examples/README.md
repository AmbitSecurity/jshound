# examples/demo-app

A five-line webpack bundle and the page that loads it, already on disk, so nothing here touches the
network. It exercises the things a string extractor cannot do, and you can check every claim below
in under a minute.

```console
$ jshound examples/demo-app
GET    https://demo-app.test/api/feature-flags
POST   https://demo-app.test/t/events                                  {"sid":"?","evt":"?"}
DELETE https://demo-app.test/v1/sessions/current
POST   {baseUrl}/v1/media                         multipart/form-data  {"file":"file-upload"}
POST   {baseUrl}/v1/subscribe                     application/json     {"email":"computed:trim","pageUri":"state:href"}
GET    {baseUrl}/v1/users/{param_o}/profile
```

Six requests. Three come from the browser's own sinks (`fetch`, `XMLHttpRequest.open` and
`navigator.sendBeacon`) and three from a class whose methods wrap `fetch`, one of them uploading a
`FormData`. Every one has its verb, and each body has its field names and where each value comes
from: `computed:trim` is the result of a `.trim()` call, `state:href` is `location.href`.

## Why three of them say `{baseUrl}`

The class sets `this.baseUrl = o.apiBase` from its constructor argument, and nothing in the bundle
constructs it, so the base is not in the code. The page's config does carry
`apiBase: "https://api.demo-app.test"`, which is the likely answer, but it is a guess the analysis
does not make. The page's own host would be a worse guess, because it is wrong: these calls go to
the API host. A request aimed at the wrong host is the worst output this tool can produce, so the
base is left as the code names it.

The same rule decides what goes in `urls.txt`, which holds only requests whose host is known:

```console
$ cat .jshound/demo-app/urls.txt
DELETE https://demo-app.test/v1/sessions/current
GET https://demo-app.test/api/feature-flags
POST https://demo-app.test/t/events
```

And `openapi.json`, which imports into Burp, Caido or Postman, gives each of those three operations
a server of its own. Its default is a reserved name that resolves nowhere, so an import cannot send
them to the wrong host by accident. Trimmed to the parts that matter here:

```json
"/v1/subscribe": {
  "post": {
    "operationId": "subscribe",
    "requestBody": {
      "content": {
        "application/json": {
          "schema": {
            "type": "object",
            "properties": {
              "email":   { "type": "string", "description": "source: computed:trim" },
              "pageUri": { "type": "string", "description": "source: state:href" }
            }
          }
        }
      }
    },
    "servers": [
      { "url": "{baseUrl}",
        "variables": { "baseUrl": { "default": "https://base.invalid" } } }
    ]
  }
}
```

`operationId` is the client's own method name, read out of the bundle.

## The chunks are not in the bundle either

The bundle lazy-loads three more files, and their URLs appear nowhere in it:

```console
$ grep -c "settings.a1b2c3d4.js" examples/demo-app/app.4f2a9c1e.js
0
```

The webpack runtime builds them from two id maps when the user navigates:

```js
r.u = e => "static/chunks/" + ({12:"settings",34:"billing",56:"admin"}[e] || e)
         + "." + {12:"a1b2c3d4",34:"e5f6a7b8",56:"c9d0e1f2"}[e] + ".js"
```

Against a live site, jshound evaluates that builder out of the syntax tree, without running the
bundle, and fetches every URL it produces. A folder on disk has nothing to fetch, but the library
shows what it would ask for:

```console
$ python3 -c 'import jshound; print("\n".join(jshound.chunk_urls(open("examples/demo-app/app.4f2a9c1e.js").read())))'
static/chunks/settings.a1b2c3d4.js
static/chunks/billing.e5f6a7b8.js
static/chunks/admin.c9d0e1f2.js
```
