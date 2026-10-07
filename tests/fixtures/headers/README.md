Request headers in the four spellings that reach a sink: an inline object, a `new Headers()` binding
assembled with `.set()`/`.append()`, a client config object, and `XMLHttpRequest.setRequestHeader`.

The `new Headers()` binding was the hole. `new Headers({…})` already worked because the names sit
inside the wrapper, but the assembled form returned no headers at all, while the names were plainly
visible two statements above the call. Same shape as `searchParams.set` on a URL binding.
