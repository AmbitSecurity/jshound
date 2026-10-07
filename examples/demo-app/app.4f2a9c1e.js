(()=>{var r={};r.p="/_assets/";
r.u=e=>"static/chunks/"+({12:"settings",34:"billing",56:"admin"}[e]||e)+"."+{12:"a1b2c3d4",34:"e5f6a7b8",56:"c9d0e1f2"}[e]+".js";
var n={
418:(e,t,a)=>{a.d(t,{Api:()=>s});class s{constructor(o){this.baseUrl=o.apiBase}
async subscribe(o){return fetch(`${this.baseUrl}/v1/subscribe`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email:o.email.trim(),pageUri:location.href})})}
async getUser(o){return fetch(`${this.baseUrl}/v1/users/${o}/profile`,{method:"GET"})}
async upload(o){const d=new FormData;return d.append("file",o),fetch(`${this.baseUrl}/v1/media`,{method:"POST",body:d})}}},
902:(e,t,a)=>{const s=a(418);new XMLHttpRequest().open("DELETE","/v1/sessions/current");
fetch("/api/feature-flags?env="+window.__ENV,{method:"GET"});
navigator.sendBeacon("/t/events",JSON.stringify({sid:window.sid,evt:"pageview"}))}};
var i={apiBase:"https://api.demo-app.test"};n[418]({},{},e=>i),n[902]({},{},e=>n[e])})();
