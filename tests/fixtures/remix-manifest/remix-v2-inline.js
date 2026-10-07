// Remix v2's shape: the manifest is assigned inside a `<script type="module">` that ALSO carries ESM
// import statements. Running the whole script in a vm throws "Cannot use import statement outside a
// module" before reaching the assignment, which is why only the assigned expression is evaluated.
import * as route0 from "https://cdn.example.com/assets/root-CSnehLZw.js";
import * as route1 from "https://cdn.example.com/assets/(_locale)._index-By8tNEDq.js";
window.__remixManifest = {
  "entry": { "module": "https://cdn.example.com/assets/entry.client-C8PL9TPQ.js", "imports": ["https://cdn.example.com/assets/components-Cd6skYRt.js"] },
  "routes": {
    "root": { "id": "root", "path": "", "hasAction": false, "hasLoader": true, "hasErrorBoundary": true, "module": "https://cdn.example.com/assets/root-CSnehLZw.js", "imports": ["https://cdn.example.com/assets/Cart-CVozpZzD.js"] },
    "routes/($locale)._index": { "id": "routes/($locale)._index", "parentId": "root", "path": ":locale?", "index": true, "hasAction": false, "hasLoader": true, "module": "https://cdn.example.com/assets/(_locale)._index-By8tNEDq.js", "imports": [] },
    "routes/account.login": { "id": "routes/account.login", "parentId": "root", "path": "account/login", "hasAction": true, "hasLoader": false, "module": "https://cdn.example.com/assets/account.login-X1.js", "imports": [] }
  },
  "url": "https://cdn.example.com/assets/manifest-9c45bfe8.js",
  "version": "9c45bfe8"
};
