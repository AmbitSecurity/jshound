// real tRPC
const trpc = createTRPCReact();
function A(){ const {data} = trpc.alertRule.getById.useQuery({ruleUid:"x"}); return data; }
function B(){ const [m] = trpc.alertRule.upsert.useMutation(); return m; }
function C(){ return trpc.workspace.members.list.useQuery({orgId:1}); }
// RTK Query codegen — same member-chain shape, NOT trpc
const api = createApi({});
function D(){ const [f] = api.endpoints.upsertRuleGroupForNamespace.useMutation(); return f; }
function E(){ return api.endpoints.getAlertRule.useQuery(u, {refetchOnMountOrArgChange:true}); }
