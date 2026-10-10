// A relative base, applied: `client.get("/users")` is `/api/v2/users`. It used to be refused, because a
// client was found by its receiver's name and a minified name collides across modules; on Grafana that
// glued a k8s resource base onto the legacy REST API. Found by binding, it cannot collide.
import axios from "axios";
const client = axios.create({ baseURL: "/api/v2" });
export function users() { return client.get("/users"); }
