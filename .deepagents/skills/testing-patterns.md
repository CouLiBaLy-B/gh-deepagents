# Skill: testing-patterns

When adding tests, follow the conventions of the host project first. These are
defaults if none exist.

## Layout
- Python: `tests/test_<module>.py`, mirror the source tree.
- JS/TS: `__tests__/<file>.test.ts` next to source, or `tests/` root.
- Go: `<file>_test.go` alongside source.
- Rust: `#[cfg(test)] mod tests` in-file, or `tests/` for integration.

## Naming
- `test_<verb>_<expected>` (Python) — `test_parse_returns_none_on_empty`
- `it("should …")` / `test("…")` (JS) — focus on behaviour, not function
- `Test<Func>_<Case>` (Go) — `TestParse_EmptyInput`

## Structure: Arrange-Act-Assert
```python
def test_resolve_handles_missing_user():
    # Arrange
    repo = FakeRepo(users={})
    # Act
    out = resolve(repo, user_id=42)
    # Assert
    assert out is None
```

## Cover edges
For every new function, write tests for:
1. Happy path (typical input)
2. Empty / null / zero input
3. Boundary (off-by-one, max length, overflow)
4. Invalid type / malformed input
5. Failure mode (dependency raises) — patch + assert exception path

## Fixtures > globals
- Python: pytest `@pytest.fixture` (scope=`function` unless costly)
- JS: `beforeEach` for setup, no module-level mutable state
- Go: table-driven tests with `t.Run(name, ...)`

## Network/IO
- ALWAYS mock external HTTP (requests-mock, nock, httptest).
- Never hit real services in unit tests. Use `tests/integration/` for those.

## Time / randomness
- Inject a `clock` / `rand` interface; freeze in tests.
- Python: `freezegun` or `monkeypatch.setattr("time.time", ...)`.

## Assertions
- Prefer one logical assertion per test (split if needed).
- Use rich diffs: `pytest.approx`, deep-eq matchers.
- Avoid asserting on log strings unless that's the contract.

## What NOT to test
- Don't test the framework / stdlib.
- Don't test private helpers in isolation if covered through the public API.
- Don't snapshot-test output you don't read (snapshots rot).
