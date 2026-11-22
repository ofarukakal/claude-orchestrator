# Task: write tests for billing.py

`billing.py` in the project directory is believed to be correct. Its docstrings are the
specification. Write a thorough test file `test_billing.py` in the project directory.

The purpose of the tests is to catch regressions: if someone later breaks any documented behaviour
of any function, at least one test must fail. Cover every promise the docstrings make, including
edge cases and error cases, not just the happy path.

Requirements:

- Standard library only; plain `assert` statements. No pytest or other third-party packages.
- `python test_billing.py` must exit with code 0 against the current `billing.py`, and with a
  non-zero code if any assertion fails.
- Do not modify `billing.py`.
- Test only documented behaviour. A test that fails against the current, correct `billing.py`
  disqualifies the whole file.
