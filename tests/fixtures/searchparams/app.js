const base="https://api.sp.test";
function search(q, page){
  const u = new URL("/v1/search", base);
  u.searchParams.set("q", q);
  u.searchParams.set("page", page);
  u.searchParams.append("includeArchived", "true");
  return fetch(u, {method:"GET"});
}
function listing(cursor){
  const u = new URL("/v1/items", base);
  if (cursor) u.searchParams.set("cursor", cursor);
  return fetch(u.href);
}
function direct(q){ return fetch(`/v1/direct?term=${q}&limit=50`, {method:"GET"}); }
