// A testimonial wall: each entry names the component that renders it AND an absolute link out.
// That sibling is exactly what promotes a path literal to a "client-side route", so Supabase's
// marketing bundle produced 51 of its 53 routes as other people's tweets.
var TESTIMONIALS = [
  { component: TweetCard, href: "https://x.com/someone/status/1924425289959928039" },
  { component: TweetCard, href: "https://twitter.com/another/status/1373677301905362948" }
];
// A real client-side route in the same shape, which must survive.
var ROUTES = [
  { component: SettingsView, path: "/account/settings" },
  { component: BillingView, path: "/org/{slug}/billing" }
];
fetch("/v1/ping", { method: "POST" });
