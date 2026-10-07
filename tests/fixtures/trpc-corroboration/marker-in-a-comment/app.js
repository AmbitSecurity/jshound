// This file is NOT a tRPC app. It only talks about one: /api/trpc/ and createTRPCReact appear here
// in a comment, which is commentary, not code. The marker used to be a regex over raw source text
// and could not tell the difference — the fixture written to prove corroboration worked turned the
// marker on with its own explanatory comment and so proved nothing.
const q = buildNamespace();
function F() { return q.workspace.members.useQuery({ orgId: 1 }); }
