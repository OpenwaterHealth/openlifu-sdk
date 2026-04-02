# Changelog

All notable changes to `openlifu-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- `CONTRIBUTING.md` with development setup, coding conventions, and release process.
- `CHANGELOG.md` (this file).
- `.editorconfig` for consistent editor settings across contributors.
- `.pre-commit-config.yaml` with ruff (lint + format) and general file checks.
- `ruff`, `mypy`, and `pytest` configuration in `pyproject.toml`.
- CI workflow (`.github/workflows/test.yml`) that runs lint and tests on every push
  and pull request across Ubuntu, Windows, and macOS.

### Fixed
- **`LIFUUart`** — removed library-level `log.propagate = False`, hard-coded
  `log.setLevel(logging.ERROR)`, and manual `StreamHandler` setup. Libraries must not
  configure the root logging hierarchy. Logger name changed from `"UART"` to
  `__name__`.
- **`LIFUTXDevice`** — same logging configuration fix; logger name changed from
  `"TXDevice"` to `__name__`.
- **`LIFUInterface`** — `signal_connect`, `signal_disconnect`, `signal_data_received`,
  `hvcontroller`, and `txdevice` were declared as class-level attributes, causing all
  instances to share the same signal objects. These are now proper instance attributes
  created in `__init__`. Instance attribute types are also correctly annotated with
  `Optional[...]`.
- **`pyproject.toml`** — the `[test]` and `[dev]` optional-dependency groups were
  identical. `[dev]` now adds `pre-commit`, `ruff`, and `mypy` on top of the test
  dependencies.
- **All modules** — replaced f-string logging calls (`logger.error(f"msg {x}")`) with
  lazy `%`-style formatting (`logger.error("msg %s", x)`) throughout
  `LIFUTXDevice.py`, `LIFUHVController.py`, `LIFUUart.py`, `LIFUInterface.py`, and
  `LIFUUserConfig.py`.
- **`LIFUTXDevice.py`** — fixed a bare `logging.warn` statement (useless expression /
  root-logger call) in `calc_pulse_pattern`; replaced with `logger.warning(...)`.
- **`LIFUTXDevice.py`** — moved the mid-file `LIFUConfig` import block to the top of
  the file (alongside all other imports) to comply with PEP 8 and ruff E402.
- **`LIFUTXDevice.write_config_json`** — `raise ValueError(...)` inside an `except`
  block now chains the original exception (`raise ... from e`) per PEP 3134 / ruff B904.

---

## [1.0.1] — 2025-XX-XX

_(Previous release — see git history for details.)_

## [1.0.0] — 2025-XX-XX

_(Initial public release.)_

[Unreleased]: https://github.com/OpenwaterHealth/openlifu-sdk/compare/1.0.1...HEAD
[1.0.1]: https://github.com/OpenwaterHealth/openlifu-sdk/compare/1.0.0...1.0.1
[1.0.0]: https://github.com/OpenwaterHealth/openlifu-sdk/releases/tag/1.0.0
