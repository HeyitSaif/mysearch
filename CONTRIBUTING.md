# Contributing

MySearch is a small project, so keep changes focused. For anything bigger than a bug fix, open an issue first.

- Set up with `./scripts/setup.sh` (see [docs/INSTALL.md](docs/INSTALL.md)), then run the tests with
  `.venv/bin/python -m unittest discover -s tests`.
- Tests and builds run locally, not on GitHub Actions. `scripts/setup.sh` installs the git hook `scripts/hooks/pre-push`
  (`scripts/install_hooks.sh` does it alone); it runs the tests in a sandbox, the snapshots and the audit before every push.
  The commands by hand are in [docs/RELEASING.md](docs/RELEASING.md), "Checks before a push".
- Write tests with generic fixtures: `/Users/alex/...` paths, invented names and documents. Never use real files, names,
  keys or identifiers.
- Never commit a real document, a key, a credential or a private gold set. The maintainer runs
  `scripts/export_public.sh` before each public release; it audits the public tree for keys, personal identifiers
  and private terms. If the audit flags a test value, replace the value rather than asking for an exemption.
- Follow the surrounding code. Comments say why, not what.

By contributing you agree that your contribution is licensed under the MIT licence in [LICENSE](LICENSE).
