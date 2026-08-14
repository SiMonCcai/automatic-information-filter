# Contributing

Small, focused changes are easiest to review.

1. Create a branch from `main`.
2. Install the development dependencies with `pip install -e '.[dev]'`.
3. Add tests for behavior changes.
4. Run `ruff check .` and `pytest`.
5. Open a pull request that explains the use case and test coverage.

New adapters should implement one of the public interfaces in `information_filter.interfaces`. Keep provider-specific dependencies optional when possible, never include credentials or real user data in fixtures, and document the smallest working TOML example.
