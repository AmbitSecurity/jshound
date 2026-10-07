import axios from "axios";
// `url`/`uri`/`endpoint` are in the base-key FAMILY but do not say "base". A relative value under one
// of them is a request or a setting, and prefixing calls with it manufactures `/users/orders`.
const c1 = axios.create({ url: "/users", timeout: 1000 });
export function a() { return c1.get("/orders"); }
const c3 = axios.create({ uri: "/graphql" });
export function d() { return c3.post("/invoices", {}); }
