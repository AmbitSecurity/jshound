Real output of `rollup -f es`. Nothing broke on it: contract
extraction and chunk discovery both work, and `esm (no bundler runtime marker)` is the correct
bundler answer rather than a detection failure, pure ESM output genuinely carries no runtime global.
