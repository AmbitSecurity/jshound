// `basePath` and `prefixUrl` say "base" outright, but the original key family matched neither, so an
// origin under either was ignored and these calls were reported as `/things` and `/publish`.
const c2 = mkClient({ basePath: "https://svc.mycorp.io/v1" });
export function things() { return c2.get("/things"); }
const c3 = ky({ prefixUrl: "https://edge.mycorp.io" });
export function edge() { return c3.post("/publish", {}); }
