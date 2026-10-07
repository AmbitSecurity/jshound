import axios from "axios";

// `.get` is an HTTP verb and also a method on several built-ins. These two were emitted as endpoints:
// the argument is a perfectly good path, and the argument was never the problem.
const cache = new Map();
function a() { return cache.get("user/profile"); }

const qs = new URLSearchParams(location.search);
function b() { return qs.get("redirect/next"); }

const hdrs = new Headers();
function c() { return hdrs.get("content/type"); }

const fd = new FormData();
function d() { return fd.get("upload/file"); }

const seen = new WeakMap();
function e() { return seen.get("org/members"); }

// REAL clients, which must survive. The check only rejects a receiver it can PROVE is not one.
const client = axios.create({ baseURL: "/api" });
function f() { return client.get("/v1/users"); }

const unresolvable = makeClient();
function g() { return unresolvable.get("/v2/orders"); }

function h() { return axios.get("/v3/invoices"); }
