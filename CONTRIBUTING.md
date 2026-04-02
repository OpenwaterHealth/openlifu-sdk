# Contributing to openlifu-sdk

Thank you for your interest in contributing! This guide covers environment setup, coding conventions, testing, and the pull request process.

## Table of Contents

- [Development setup](#development-setup)
- [Project structure](#project-structure)
- [Coding conventions](#coding-conventions)
- [Running tests](#running-tests)
- [Linting and formatting](#linting-and-formatting)
- [Pre-commit hooks](#pre-commit-hooks)
- [Pull request process](#pull-request-process)
- [Release process](#release-process)

---

## Development setup

1. **Fork and clone** the repository:

   ```bash
   git clone https://github.com/<your-fork>/openlifu-sdk.git
   cd openlifu-sdk
   ```

2. **Create a virtual environment** (Python 3.12+):

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

3. **Install in editable mode** with dev dependencies:

   ```bash
   pip install -e ".[dev]"
   ```

4. **Install pre-commit hooks** (optional but recommended):

   ```bash
   pre-commit install
   ```

---

## Project structure

```
src/openlifu_sdk/          # Library source (src layout)
unit-test/                 # Automated unit tests (pytest)
examples/                  # Hardware-targeting interactive scripts (not automated)
.github/workflows/         # CI workflows
```

The package uses a `src/` layout (PEP 517). All library code lives under `src/openlifu_sdk/`.

---

## Coding conventions

- **Python version:** 3.12+. Use modern syntax (`X | Y` unions, `match`, etc.).
- **Imports:** Use `from __future__ import annotations` at the top of every module.
- **Line length:** 120 characters (enforced by ruff).
- **Logging:**
  - Use `logging.getLogger(__name__)` — never hard-code logger names.
  - Never set `logger.propagate = False` or add handlers in library code.
  - Use `%s` lazy formatting: `logger.info("msg %s", value)` — never f-strings.
- **Type annotations:** Add return type annotations to all public methods.
- **Docstrings:** Google style with `Args:` / `Returns:` / `Raises:` sections.
- **Exceptions:** Raise with chaining inside `except` blocks (`raise ... from err`).
- **Signals:** Use `LIFUSignal` for event callbacks; never share signal objects at class level — always create them in `__init__`.

---

## Running tests

```bash
# All tests, verbose
pytest unit-test/ -v

# With coverage
pytest unit-test/ -v --cov=src --cov-report=term-missing

# Single test file
pytest unit-test/test_tx_device.py -v
```

Tests in `unit-test/` use `unittest.mock.MagicMock(spec=LIFUUart)` to mock hardware — **no physical device is required**.

The `examples/` scripts require real hardware and are not run in CI.

---

## Linting and formatting

This project uses [ruff](https://docs.astral.sh/ruff/) for both linting and formatting.

```bash
# Check for lint errors
ruff check src/

# Auto-fix lint errors
ruff check src/ --fix

# Format code
ruff format src/

# Check formatting without modifying files
ruff format src/ --check
```

All lint and format checks run automatically in CI on every push and pull request.

---

## Pre-commit hooks

[pre-commit](https://pre-commit.com/) runs ruff, mypy, and general file checks before every commit.

```bash
# Install hooks (one-time, after cloning)
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

---

## Pull request process

1. Create a branch from `main`:

   ```bash
   git checkout -b feat/my-feature
   ```

2. Make your changes, write or update tests, and ensure all checks pass:

   ```bash
   pytest unit-test/ -v
   ruff check src/ && ruff format src/ --check
   ```

3. Open a pull request against `main`. CI will run automatically.
4. Address reviewer feedback.
5. A maintainer will merge once CI is green and the PR is approved.

---

## Release process

Releases are driven by git tags:

| Tag pattern | Outcome |
|---|---|
| `1.2.3` | Full release — wheel built, GitHub Release created, published to PyPI |
| `pre-1.2.3` | Pre-release — wheel built, GitHub pre-release created, **not** published to PyPI |

Steps for maintainers:

1. Ensure `CHANGELOG.md` is up to date.
2. Push a tag: `git tag 1.2.3 && git push origin 1.2.3`
3. The `release-build.yml` workflow builds the wheel and creates the GitHub Release automatically.
4. The `publish-pypi.yml` workflow publishes to PyPI when a non-pre-release GitHub Release is published.
