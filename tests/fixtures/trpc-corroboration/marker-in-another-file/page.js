// No marker here at all: just the proxy chain, in a different file from the client that proves tRPC.
// This is where a per-file or per-batch verdict breaks, the marker is somewhere else in the corpus.
import { trpc } from "./client.js";
function A() { return trpc.viewer.bookings.list.useQuery({ orgId: 1 }); }
function B() { const [m] = trpc.viewer.bookings.create.useMutation({ id: 2 }); return m; }
