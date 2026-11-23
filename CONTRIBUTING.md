# Contributing

Thanks for helping! The project is small on purpose.

1. Fork, then create a branch.
2. Make your change. Keep it dependency free (standard library only) and keep the [security rules](SECURITY.md).
3. Add or update a test. Provider behaviour goes into `tests/fakes.py` so it can be tested without a real key.
4. Run `python -m unittest discover -s tests -t .` and make sure everything passes.
5. Open a pull request and say what you changed and why.

Bug reports are most useful with the output of `agentbridge doctor` (it never prints keys) and the provider and model you used. Please do not paste API keys.

Good first issues: provider quirks, a Windows install script, more translation edge cases (new content block types).
