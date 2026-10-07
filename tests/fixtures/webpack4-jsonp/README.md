webpack 4 builds chunk URLs in a named function (`jsonpScriptSrc`) rather than assigning
`__webpack_require__.u`, so a detector keyed on the assignment finds nothing. GitLab is webpack 4
and reported 0 chunks from a runtime declaring over a thousand.

Matched on shape, not name: one parameter, an expression indexing object literals by that
parameter, and a string literal ending in `.js`.
