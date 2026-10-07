import axios from "axios";
const client = axios.create({ baseURL: "https://api.mycorp.io/v2" });
export function users() { return client.get("/accounts"); }
