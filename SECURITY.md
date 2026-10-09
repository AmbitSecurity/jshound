# Security

jshound reads JavaScript from sites it does not control, so a bug in how it handles hostile input
is a real vulnerability: a bundle that runs code on the machine analysing it, a response that fills
the disk, a URL that makes it reach into a private network.

If you find one, please report it privately through GitHub: **Security** tab, then **Report a
vulnerability**. Include the input that triggers it and what happens. Please do not open a public
issue for it.

What jshound already defends against, so you know where the line is:

- It never runs the JavaScript it reads. Chunk URLs and route tables come from the parsed syntax
  tree; there is no `eval`, no `Function`, no `vm`.
- Node runs with an allowlisted environment, so a parser bug cannot read your shell's secrets.
- A response is read up to 64 MB and no further.
- URLs a target names in private address space are refused unless you pass `--allow-private`.
