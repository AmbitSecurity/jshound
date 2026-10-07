// react-router's own shape: both factories defined here AND called from its own component bodies.
function createHashHistory(o = {}) { return getUrlBasedHistory(o); }
function createBrowserHistory(o = {}) { return getUrlBasedHistory(o); }
function HashRouter({ window: w }) { const Y = useRef(); Y.current = createHashHistory({ window: w }); return Y; }
function BrowserRouter({ window: w }) { const Y = useRef(); Y.current = createBrowserHistory({ window: w }); return Y; }
const routes = [{ path: "/dash/home", element: null, loader: () => null }];
export { HashRouter, BrowserRouter, routes };
