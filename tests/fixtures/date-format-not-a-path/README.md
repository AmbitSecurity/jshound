moment/dayjs ship a locale table, `{LTS:"h:mm:ss A", LT:"h:mm A", L:"MM/DD/YYYY"}`, and `L`
reached a client-verb sink, so Notion produced an endpoint whose URL was `MM/DD/YYYY`.

Requiring a leading slash would have caught it and would also have discarded real relative paths
(`api/datasources/uid/…`, `s3/multipart`), so the test is the shape of a format string instead.
`/api/v1/MM/items` must survive it.
