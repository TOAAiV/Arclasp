# Contributing to Arclasp

Thanks for considering a contribution. This is the open-source SDK; the
Arclasp backend is closed-source and not part of this repository.

## Requirements

- Python 3.10 or newer (3.10–3.13 if you need the CrewAI adapter; CrewAI's own
  dependencies aren't yet compatible with 3.14).

## Setup

```bash
git clone https://github.com/TOAAiV/Arclasp.git
cd Arclasp
python -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

Install a framework extra too if you're working on an adapter, e.g.
`pip install -e ".[dev,langgraph]"`.

## Running tests

```bash
pytest
```

This runs the full non-mutating unit and integration test suite. Tests that
require a private backend to be checked out separately (parity tests against
`backend.app.services.policy_engine`) skip automatically when that module
isn't importable — that's expected in a standalone clone of this repo.

## Building the package

```bash
pip install build
python -m build
```

Inspect the resulting `dist/*.whl` and `dist/*.tar.gz` before publishing
anything — only maintainers publish releases.

## Pull requests

- Keep changes focused; unrelated cleanup makes review harder.
- Add or update tests for behavior changes.
- Run `ruff check .` and `pyright` before opening a PR.
- Describe *why* the change is needed, not just what changed.

## Reporting security issues

Do not open a public issue for a security vulnerability. See
[SECURITY.md](SECURITY.md) for the private disclosure process.
