tRPC and RTK Query generate the same call shape — a member chain ending in `useQuery`/`useMutation`
— and only one of them means `/api/trpc/<router>.<procedure>`.

Measured on Grafana: the duck-type matched RTK Query's `api.endpoints.<name>.useMutation()` and
produced 13 records at paths like `/api/trpc/hK.endpoints.upsertRuleGroupForNamespace`, while the
string `/api/trpc` appears **zero** times in its entire bundle. A fabricated endpoint is worse than a
missed one: someone sends requests to a URL that was never there.

`endpoints` is RTK Query's own namespace and the reliable tell.
