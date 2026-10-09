// A bundle with RTK Query's generated hooks and no tRPC dependency, client factory or endpoint
// string anywhere, this is Grafana. The member chains are shape-identical to a tRPC call, so an
// ungated duck-type manufactures endpoints under a URL that is not in this file at all.
const api = buildApi({});
function D() { const [f] = api.endpoints.upsertRuleGroupForNamespace.useMutation(); return f; }
function E() { return api.endpoints.getAlertRule.useQuery(u, { refetchOnMountOrArgChange: true }); }
// And the same shape from a namespace that is NOT called `endpoints`, which no name check can catch:
// three levels deep, so it clears the >=2-part arity rule.
const queries = buildNamespace();
function F() { return queries.workspace.members.useQuery({ orgId: 1 }); }
function G() { const [m] = queries.billing.invoices.useMutation({ id: 2 }); return m; }
