const base="https://api.eb.test";
async function a(){ return fetch(new URL("/v1/alpha", base), {method:"GET"}); }
async function b(x){ return fetch("/api/v1/beta", {method:"POST", body:JSON.stringify({x, kind:"eb"})}); }
async function c(){ const m = await import('./lazy-Bh7QEFKr.js'); return m.go(); }
document.addEventListener("click",()=>{a();b(1);c();});
