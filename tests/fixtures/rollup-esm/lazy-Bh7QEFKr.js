function go(){ return fetch("/api/v1/gamma", {method:"PATCH", body:JSON.stringify({on:1})}); }

export { go };
