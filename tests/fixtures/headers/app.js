const base="https://api.hd.test";
function a(t){ return fetch("/v1/a", {method:"POST", headers:{"Authorization":"Bearer "+t,"X-Org-Id":"42","content-type":"application/json"}, body:JSON.stringify({x:1})}); }
function b(t){ const h=new Headers(); h.set("X-Api-Key", t); h.append("X-Trace", "1"); return fetch("/v1/b", {method:"GET", headers:h}); }
const client={post:(u,b,c)=>0};
function c(t){ return client.post("/v1/c", {y:2}, {headers:{"X-Tenant": t, "Accept":"application/json"}}); }
function d(t){ const xhr=new XMLHttpRequest(); xhr.open("PUT","/v1/d"); xhr.setRequestHeader("X-CSRF-Token", t); xhr.send("{}"); }
