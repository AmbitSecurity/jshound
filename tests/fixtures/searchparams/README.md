Query parameters set through `URL.searchParams`, in the three spellings that reach a sink:
`fetch(u)`, `fetch(u.href)` and `fetch(u.toString())`. The `.href` one fell through — the other two
resolved their parameters while it reported none, which reads as "this endpoint takes no query
parameters" rather than as a miss. Also covers a conditional `if (c) u.searchParams.set(...)` and a
literal query string in the path.
