# Contributing

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt
```

## Running tests

From the repo root, with the venv active:

```bash
cd tests
pytest unit                 # fast, no live hass fixture
pytest integration          # additionally needs a real (in-process) hass via phacc
```

Both suites need `homeassistant` + `voluptuous` installed (see `requirements_test.txt`) — even
`tests/unit` triggers `custom_components/kiosk_satellite_manager/__init__.py`'s top-level imports.
"Unit" means no live hass fixture, not dependency-free. `tests/integration` additionally uses
`pytest-homeassistant-custom-component` to spin up that fixture — install a `homeassistant` version
that matches your `pytest-homeassistant-custom-component` pin before running it.

## Scope

Bug reports and PRs are welcome on [GitHub](https://github.com/cfoxga/kiosk-satellite-manager/issues). Open an issue first if your change
needs design discussion before code. The GitHub repo is updated for each release, so a PR may be
merged upstream and arrive with the next release rather than appear on `main` right away.

## Commit style

Conventional commits (`feat(...)`, `fix(...)`, `docs(...)`, `test(...)`, `refactor(...)`).
