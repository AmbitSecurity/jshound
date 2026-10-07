// RTK Query's OTHER construction, in a bundle that DOES carry a tRPC marker (a monorepo shipping
// both). The marker alone cannot discriminate here; the chain's root has to be resolved.
import { createTRPCReact } from "@trpc/react-query";
const trpc = createTRPCReact();
const base = createApi({ reducerPath: "x", baseQuery: fetchBaseQuery({ baseUrl: "/api" }), endpoints: () => ({}) });
const extended = base.injectEndpoints({ endpoints: () => ({}) });
function G() { const [m] = extended.endpoints.updateThing.useMutation(); return m; }
function H() { return trpc.workspace.members.list.useQuery({ orgId: 1 }); }
