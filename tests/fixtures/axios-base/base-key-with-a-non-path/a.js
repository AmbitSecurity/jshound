import axios from "axios";
// The key says base, but the value is a word, not a path or an origin. Neither kind of base.
const c5 = axios.create({ baseURL: "production" });
export function f() { return c5.get("/orders"); }
const c6 = axios.create({ baseURL: "?v=2" });
export function g() { return c6.get("/invoices"); }
