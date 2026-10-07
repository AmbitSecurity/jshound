// amcharts: an ORIGIN under a key that is not a base. Value-shape alone cannot tell this from an API
// base, and treating it as one produced GET http://www.amcharts.com/lib/3/images/environment/list/names
// on four unrelated clients.
const c4 = mk({ pathToImages: "http://www.amcharts.com/lib/3/images/" });
export function e() { return c4.get("/environment/list/names"); }
