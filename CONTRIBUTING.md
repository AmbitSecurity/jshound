# Contributing

Thanks for helping. Bug reports and pull requests are both welcome.

## Reporting a missed request

If jshound misses a request you can see in the browser, [open an issue](https://github.com/AmbitSecurity/jshound/issues/new/choose) with:

- the site, or the smallest piece of JavaScript that shows the problem
- the request you expected (method and URL)
- what jshound printed instead

## Making a change

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
pip install -e . pytest openapi-spec-validator
pytest tests -q
```

1. Add a test with the smallest JavaScript that shows the case. `tests/test_client_base.py` is a
   good model: each test is a few lines of code and the requests jshound should find in it.
2. Make the change, and run `pytest tests -q`.
3. Open a pull request saying what was missed and what jshound finds now.

One rule: a value jshound cannot resolve stays marked as unresolved. A change that fills in a guess
where the code does not say will not be merged.

Never commit JavaScript copied from a site you do not own. Write the smallest example by hand, or
build one with the bundler yourself.

If you change the Babel version in `package.json`, rebuild the bundled copy with
`npm install && npm run bundle`.

To check a change against many real apps, see `bench/regress.sh`: it runs jshound over saved sites
before and after, and `bench/regress_diff.py` lists every request that changed.

## License

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
