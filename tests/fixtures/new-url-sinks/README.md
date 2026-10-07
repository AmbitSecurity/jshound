`new URL(path, base)` IS the URL, and every fetch-shaped sink accepts the object itself, not only
`.href`. Only the `fetch` branch understood it, so the same call through an axios-style client or
`new Request` resolved to nothing. tldraw builds its telemetry as
`fetch(new URL("/v1/event", this.#cfg.endpoint), …)`.
