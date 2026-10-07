# tRPC corroboration fixtures

One directory per case, and that matters: `isTrpc` is computed over the whole source handed to the
extractor at once, so a bundle with no tRPC placed beside one that has it would inherit the marker and
the no-tRPC case would stop testing anything. Each case is analysed alone.

| directory | what it holds | expected |
|---|---|---|
| `no-trpc/` | RTK's `api.endpoints.X.useQuery()` and `queries.workspace.members.useQuery()`, the same shape under a namespace not called `endpoints`, in a file with no trace of tRPC | no tRPC endpoints at all |
| `rtk-inject/` | `createApi({...})` and `<api>.injectEndpoints({...})` beside a real `createTRPCReact()` call, so the bundle-level marker is present and cannot discriminate | only the real tRPC chain |
| `trpc-named-endpoints/` | a real tRPC router with a procedure legitimately called `endpoints` | both chains, kept |
| `marker-in-a-comment/` | the same shape, in a file that only MENTIONS `/api/trpc/` and `createTRPCReact` in a comment | no tRPC endpoints at all |

Measured before the corroboration rule, `no-trpc/` produced `/api/trpc/workspace.members` and
`/api/trpc/billing.invoices`, two endpoints at a URL that appears nowhere in the file.

`marker-in-a-comment/` exists because of a mistake worth keeping: the marker was a regex over raw
source, so the first version of `no-trpc/app.js` turned it on with its own explanatory comment,
the fixture written to prove corroboration worked proved nothing instead, and passed. The marker is
now read from the AST, where a comment is not a node value, and the two concerns are separate
fixtures so neither can mask the other again.
