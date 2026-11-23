# Contributing

Thanks for helping! This is an unofficial, personal-agent project; small focused PRs are easiest to review.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate    # Python 3.12+
pip install -r requirements.txt
python3 -m pytest tests -q                       # offline unit tests
```

## Guidelines

- Open an issue first for larger changes (new driver, new adapter, behavior changes).
- Add tests with descriptive names for new behavior; they must run offline with no Slack/Copilot access.
- Never commit secrets, real hostnames, personal or company names; use `example.com` / `me@example.com` in examples and tests.
- Secrets in config are references only (`${env:}`, `${file:}`, `${keychain:}`).
- Security-sensitive changes (auth, command execution, token handling) need extra care; see [SECURITY.md](SECURITY.md).
- Read the module docstring of any file you change; architecture notes are in [docs/](docs/).
- New drivers: see [docs/development.md](docs/development.md); off by default, read-only first, confirm-first writes.

## Commits and PRs

- Conventional prefixes only: `feat`, `fix`, `refactor`, `chore`, `docs`, `ci` (e.g. `fix: handle empty allow-list`). Test-only changes use `chore:`.
- Keep commits small and logical; fill in the PR template.
- By contributing you agree your work is licensed under Apache-2.0.

## Conduct

Be kind; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
