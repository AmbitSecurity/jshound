// A REAL tRPC router with a procedure legitimately named `endpoints`. Blocking any chain containing
// the property name `endpoints` throws this away — a false negative the name check cannot avoid.
import { createTRPCReact } from "@trpc/react-query";
const trpc = createTRPCReact();
function I() { return trpc.admin.endpoints.list.useQuery({ orgId: 1 }); }
function J() { const [m] = trpc.admin.endpoints.create.useMutation({ url: "/x" }); return m; }
