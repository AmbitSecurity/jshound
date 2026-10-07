const base = "https://api.demo.test";
const http = { post: (u, b) => 0, get: (u, c) => 0 };

// axios-style client, URL object passed directly
function a(x){ return http.post(new URL("/v2/axios-style", base), {name:x}) }
// ...and via .href
function b(x){ return http.get(new URL("/v2/axios-get", base).href, {params:{x}}) }
// new Request(new URL(...))
function c(x){ return new Request(new URL("/v2/request-obj", base), {method:"DELETE"}) }
// plain fetch with a URL object
function d(x){ return fetch(new URL("/v1/plain", base), {method:"PUT", body:JSON.stringify({x})}) }
// base held in a private class field — tldraw's telemetry shape
class Telemetry {
  #cfg = { endpoint: "https://api.demo.test" };
  event(t){ return fetch(new URL("/v1/event", this.#cfg.endpoint), {method:"POST", body:JSON.stringify({events:t})}) }
  logs(i){ return fetch(new URL("/v1/logs", this.#cfg.endpoint), {method:"POST", body:JSON.stringify({logs:i})}) }
}
