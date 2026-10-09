// A KNOWN LIMITATION, and a deliberate one. This base is real and relative, and it is NOT applied:
// `client.get("/users")` is reported as `/users`, not `/api/v2/users`.
//
// Accepting it was tried and measured. It fixed this file and made Grafana worse: four paths like
//   /apis/dashboard.grafana.app/v0alpha1/namespaces/default/api/datasources/uid/{P}/correlations
// appeared, gluing Grafana's k8s-style resource base onto its legacy REST API, and four correct
// records were replaced by wrong ones. The base reached those calls because `applyBase` looks a
// client up by receiver NAME, and a minified name collides across modules. An origin value masks
// that, a wrong origin is conspicuous, while a relative one yields a real-looking route pointed at
// the wrong API. Attribution by the receiver's BINDING is the fix; until then, origin-only.
import axios from "axios";
const client = axios.create({ baseURL: "/api/v2" });
export function users() { return client.get("/users"); }
