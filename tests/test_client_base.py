"""A request made through an API client goes to the client's base plus the call's path, so the base
is a value like any other and is resolved like one.

It used to be looked up by the receiver's NAME among clients created in the same file, and kept only
when it was an absolute origin. A client built in another module, returned from a function, or
configured with a relative base lost it, and the request came out as a bare path with nothing to say
a base had been there: 6,170 of 13,003 requests across the 48-app benchmark. supabase's
`/platform/auth/{ref}/otp` is `https://api.supabase.com/platform/auth/{ref}/otp`, and every step
between the two is a binding or a module export.

Each test is one shape seen in a real bundle, reduced to a few lines.
"""
import os

import jshound
from jshound.export.requests import to_lines


def _urls(tmp_path, files):
    d = tmp_path / "js"
    d.mkdir()
    for name, src in files.items():
        (d / name).write_text(src)
    return {(r["method"], r["url"]) for r in to_lines(jshound.analyze(str(d)))}


def test_turbopack_client_exported_from_another_module(tmp_path):
    """supabase.com/dashboard: the base is another module's constant, the client is openapi-fetch,
    its methods are destructured and exported, and the call site imports them by module id."""
    urls = _urls(tmp_path, {
        "a.js": '(globalThis.TURBOPACK=globalThis.TURBOPACK||[]).push([document.currentScript,101,e=>{"use strict";'
                'e.s(["API_URL",0,"https://api.example.test/platform","BASE_PATH",0,"/dashboard"],101)}]);',
        "b.js": '(globalThis.TURBOPACK=globalThis.TURBOPACK||[]).push([document.currentScript,202,e=>{"use strict";'
                'var a=e.i(303),d=e.i(101);let h=(0,a.default)({fetch:y,baseUrl:d.API_URL?.replace("/platform",""),'
                'credentials:"include"});let{GET:_,POST:v}=h;e.s(["get",0,_,"post",0,v],202)}]);',
        "c.js": '(globalThis.TURBOPACK=globalThis.TURBOPACK||[]).push([document.currentScript,404,e=>{"use strict";'
                'var A=e.i(202);async function L({projectRef:e,user:t}){let{data:a}=await (0,A.post)('
                '"/platform/auth/{ref}/otp",{params:{path:{ref:e}},body:{phone:t.phone}});return a}'
                'e.s(["L",0,L],404)}]);',
    })
    assert ("POST", "https://api.example.test/platform/auth/{ref}/otp") in urls, urls


def test_webpack_client_exported_from_another_module(tmp_path):
    urls = _urls(tmp_path, {
        "a.js": '(self.webpackChunk=self.webpackChunk||[]).push([[1],{5:(e,t,n)=>{"use strict";n.d(t,{Z:()=>o});'
                'var r=n(9);const o=r.Z.create({baseURL:"https://api.example.test/v2"})}}]);',
        "b.js": '(self.webpackChunk=self.webpackChunk||[]).push([[2],{7:(e,t,n)=>{"use strict";var i=n(5);'
                'function s(e){return i.Z.post("/orders",{id:e})}}}]);',
    })
    assert ("POST", "https://api.example.test/v2/orders") in urls, urls


def test_esm_getter_that_returns_the_client(tmp_path):
    """lovable.dev's shape: `import {AM as cs}` from a shared chunk, where AM returns the client."""
    urls = _urls(tmp_path, {
        "shared.js": 'import axios from "axios";const nW=axios.create({baseURL:"https://api.example.test"});'
                     'function M(){return nW}export{M as AM};',
        "page.js": 'import{AM as cs}from"./shared.js";async function f(o){return cs().post(`/orgs/${o}/members`,{role:"x"})}',
    })
    assert ("POST", "https://api.example.test/orgs/{param_o}/members") in urls, urls


def test_relative_base_on_a_local_client(tmp_path):
    """Resolved by binding, a relative base is as certain as an absolute one."""
    urls = _urls(tmp_path, {
        "app.js": 'import axios from "axios";const c=axios.create({baseURL:"/api/v2"});'
                  'export function u(){return c.get("/users")}',
    })
    assert ("GET", "/api/v2/users") in urls, urls


def test_an_unresolved_base_is_named_not_dropped(tmp_path):
    urls = _urls(tmp_path, {
        "app.js": 'import axios from "axios";const c=axios.create({baseURL:window.__CONFIG__.apiBaseUrl});'
                  'export function u(){return c.get("/users")}',
    })
    # Written as the code writes it. A name jshound picked would be a guess about which part matters.
    assert ("GET", "{window.__CONFIG__.apiBaseUrl}/users") in urls, urls


def test_a_base_the_page_sets_is_read_from_the_page(tmp_path):
    """`window.__CONFIG__` is set by an inline script in the page, which is saved with the bundle."""
    urls = _urls(tmp_path, {
        "inline_0_aaaa.js": 'window.__CONFIG__={apiBaseUrl:"https://api.example.test/v1",sentryDsn:"x"};',
        "app.js": 'import axios from "axios";const c=axios.create({baseURL:window.__CONFIG__.apiBaseUrl});'
                  'export function u(){return c.get("/users")}',
    })
    assert ("GET", "https://api.example.test/v1/users") in urls, urls


def test_rtk_query_takes_the_base_from_its_own_base_query(tmp_path):
    urls = _urls(tmp_path, {
        "app.js": 'import{createApi as c,fetchBaseQuery as f}from"@reduxjs/toolkit/query/react";'
                  'export const api=c({reducerPath:"x",baseQuery:f({baseUrl:"/api/v3"}),'
                  'endpoints:b=>({list:b.query({query:()=>"/widgets"}),'
                  'add:b.mutation({query:w=>({url:"/widgets",method:"POST",body:w})})})});',
    })
    assert ("GET", "/api/v3/widgets") in urls, urls
    assert ("POST", "/api/v3/widgets") in urls, urls


def test_a_generated_wrapper_with_a_default_base(tmp_path):
    """supabase's management API client: the base is a parameter's override, then an environment
    variable, then a literal default, assigned inside a comma expression."""
    urls = _urls(tmp_path, {
        "api.js": 'var c="https://api.example.test",g=async(n,a)=>{let t,r,{baseUrl:i,...p}=a??{},'
                  's=(t=i??e.default.env.API_BASE_URL??c,new URL(`${t}${n}`).toString()),y=await fetch(s,p);'
                  'return y.json()},o=async(n,e)=>g(`/v1/projects/${n}/advisors/performance`,{...e,method:"GET"});',
    })
    assert ("GET", "https://api.example.test/v1/projects/{param_n}/advisors/performance") in urls, urls


def test_a_wrapper_is_not_an_endpoint_of_its_own(tmp_path):
    """The wrapper's internal `fetch(BASE + p)` came out as `https://api.example.test{param:p}/`."""
    urls = _urls(tmp_path, {
        "app.js": 'const BASE="https://api.example.test";const req=(p,o)=>fetch(BASE+p,o);'
                  'export function users(){return req("/users",{method:"GET"})}',
    })
    assert urls == {("GET", "https://api.example.test/users")}, urls


def test_a_wrapper_around_a_wrapper_keeps_the_inner_base(tmp_path):
    urls = _urls(tmp_path, {
        "api.js": 'var c="https://api.example.test",g=async(n,a)=>{let t,{baseUrl:i,...p}=a??{},'
                  's=(t=i??c,new URL(`${t}${n}`).toString());return(await fetch(s,p)).json()},'
                  'o=async(n,e)=>g(`/v1/projects/${n}/jit-access`,{...e,method:"GET"});'
                  'export async function use(r){return o(r)}',
    })
    assert urls == {("GET", "https://api.example.test/v1/projects/{param_n}/jit-access")}, urls


def _urls_on(tmp_path, page, files):
    """As `_urls`, with each file recorded as loaded by `page`, so page-relative paths resolve."""
    import json
    d = tmp_path / "js"
    d.mkdir()
    with open(d / "_origin.jsonl", "w") as fh:
        for name, src in files.items():
            (d / name).write_text(src)
            fh.write(json.dumps({"f": name, "u": page + "assets/" + name, "d": page}) + "\n")
    return {(r["method"], r["url"]) for r in to_lines(jshound.analyze(str(d)))}


def test_a_wrapper_from_another_module_that_ends_in_fetch(tmp_path):
    """claude.ai: `w(e,t){return c(e,t)}`, `c` imported from a shared chunk where it is
    `fetchAsDocumentAccount`, which hands the path to `fetch`. So the path is the page's, like any
    other `fetch("/api/...")`, and 548 of claude.ai's requests were printed without a host."""
    urls = _urls_on(tmp_path, "https://app.example.test/", {
        "shared.js": 'async function wU(e,t={}){if(!e)return fetch(e,t);'
                     'return fetch(e,{...t,credentials:"include"})}export{wU as gt};',
        "page.js": 'import{gt as c}from"./shared.js";function w(e,t){return c(e,t)}'
                   'export async function up(n){return w(`/api/organizations/${n}/dxt/upload`,{method:"POST"})}'
                   'export async function rm(n){return c(`/api/organizations/${n}/dxt`,{method:"DELETE"})}',
    })
    assert ("POST", "https://app.example.test/api/organizations/{param_n}/dxt/upload") in urls, urls
    assert ("DELETE", "https://app.example.test/api/organizations/{param_n}/dxt") in urls, urls


def test_a_thin_function_around_a_client_is_that_client(tmp_path):
    urls = _urls(tmp_path, {
        "app.js": 'import axios from "axios";const api=axios.create({baseURL:"https://api.example.test"});'
                  'const get=(p,o)=>api.get(p,o);export function users(){return get("/users",{params:{a:1}})}',
    })
    assert ("GET", "https://api.example.test/users") in urls, urls


def test_a_comma_expression_argument_is_its_last_part(tmp_path):
    """supabase builds a query string before the path: `g((t = new URLSearchParams, ..., `/v1/...?${t}`))`.
    The value of a comma expression is its last part; read as a whole it printed `{sequence}`."""
    urls = _urls(tmp_path, {
        "api.js": 'var c="https://api.example.test",g=async(n,a)=>{let t,{baseUrl:i,...p}=a??{},'
                  's=(t=i??c,new URL(`${t}${n}`).toString());return(await fetch(s,p)).json()},'
                  'o=async(n,e,a)=>{let t;return g((t=new URLSearchParams,Object.entries(e||{}).forEach(([n,e])=>'
                  't.append(n,e)),`/v1/projects/${n}/logs?${t}`),{...a,method:"GET"})};'
                  'export async function use(r,q){return o(r,q)}',
    })
    assert urls == {("GET", "https://api.example.test/v1/projects/{param_n}/logs")}, urls
