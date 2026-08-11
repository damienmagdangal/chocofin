# Secret scanning

A `pre-commit` hook runs [gitleaks](https://github.com/gitleaks/gitleaks) over
the staged diff and fails the commit if it finds a credential. The point is that
`TELEGRAM_BOT_TOKEN` and `DATABASE_URL` never reach the history — once a secret
is in a commit, rotating it is the only real fix, and this repo is self-hosted
against a Postgres LXC holding live household data.

`core/config.py` already refuses to hold either value in code. This hook is the
backstop for the accident that module cannot prevent: a debug print, a pasted
connection string in a test, a `.env` staged by a wildcard `git add`.

## Setup on a fresh clone

**Git hooks are not cloned.** `.pre-commit-config.yaml` is in the repo, but the
hook it installs is not, so every clone needs this once:

```
uv sync
uv run pre-commit install
```

That writes `.git/hooks/pre-commit`. Until you run it, nothing is scanning.

You do **not** need to install gitleaks separately. `pre-commit` fetches the
version pinned in `.pre-commit-config.yaml` and builds it in its own cache,
bootstrapping a Go toolchain if the machine has none. The first commit after
setup takes a minute or two while that happens; every commit after is ~100ms.

Verify it is live:

```
uv run pre-commit run --all-files
```

## What runs

The hook is the upstream `gitleaks` hook, pinned by `rev` in
`.pre-commit-config.yaml`. It executes:

```
gitleaks git --pre-commit --redact --staged --verbose
```

- `--staged` — the staged diff only. Unstaged edits and files already in
  history are not scanned on commit.
- `--redact` — findings print with the secret replaced by `REDACTED`, so the
  credential is not copied into your terminal scrollback or CI logs.

Rules come from `.gitleaks.toml` at the repo root, which gitleaks picks up
automatically because it sits in the scanned path.

## What it catches

`.gitleaks.toml` sets `[extend] useDefault = true`, so the full upstream ruleset
applies (AWS, GitHub, Stripe, private keys, generic high-entropy assignments,
and so on). Two things are worth knowing specifically:

**Telegram bot tokens** are covered by the upstream `telegram-bot-api-token`
rule. It is *semi-generic*: it matches a token shape only when the word `telegr`
appears nearby. A line like `TELEGRAM_BOT_TOKEN = "..."` trips it; the same
digits pasted with no surrounding context do not.

**Database URLs with inline passwords** are covered by a rule added here,
`chocofin-database-url-password`. The upstream ruleset has nothing for ordinary
Postgres connection strings, and `DATABASE_URL` / `TEST_DATABASE_URL` are the
highest-value secrets in this repo after the bot token. The rule uses
`secretGroup` to target the password specifically, so a redacted finding still
shows you which host and database it was for.

It deliberately ignores placeholders, so documentation and fixtures can show the
real URL shape:

```
postgresql+asyncpg://chocofin:${POSTGRES_PASSWORD}@host/chocofin
postgresql+asyncpg://chocofin:<password>@host/chocofin
postgresql+asyncpg://chocofin:changeme@host/chocofin_test
```

Anything else in that position — a literal that is not an interpolated variable
or a recognised placeholder — is blocked. (This file cannot show you a blocked
example, because writing one here would fail the very hook it documents.)

## Checking that it still works

Stage a file containing a fake bot token and confirm the commit is refused. The
token has to be the right shape — 5–16 digits, then `:A`, then exactly 34
characters — so generate it rather than typing one:

```
python -c "print('TELEGRAM_BOT_TOKEN = \"8123456789:A' + 'x'*34 + '\"')" > probe.py
git add probe.py
git commit -m probe          # must fail with exit code 1
git restore --staged probe.py && rm probe.py
```

If that commit succeeds, the hook is not installed — re-run
`uv run pre-commit install`.

## When it fires

**If it found a real secret:** do not amend it away and move on. If the value
ever existed outside your machine, rotate it — a new bot token from BotFather,
a new Postgres password. Then remove it from the staged file and commit the
fixed version. Nothing was written to history, so there is no history to clean.

**If it is a false positive:** add a scoped entry to `.gitleaks.toml` and commit
that alongside the change, so the exemption is reviewable and applies to
everyone. Prefer the narrowest form — a `[[rules.allowlists]]` block under the
specific rule, keyed to a path or a stopword — over a global `[[allowlists]]`
entry, and never over disabling the rule.

**Do not reach for `git commit --no-verify`.** It skips the scan entirely, which
is the one moment the scan exists for. If a rule is wrong, fix the rule.

The hook is intentionally the *only* thing in `.pre-commit-config.yaml`. `ruff`
and `pytest` stay manual commands (see `CLAUDE.md`) so that a slow or noisy lint
is never the reason someone starts habitually passing `--no-verify` and turns
the secret scan off along with it.

## Scanning beyond the staged diff

The hook only sees what you are about to commit. To audit everything already in
the history:

```
uv run pre-commit run --all-files   # staged diff only, despite the name
```

is *not* what you want. Use gitleaks directly against the repo instead:

```
gitleaks git . --redact
```

The history was clean when this hook was introduced. `.gitleaks.toml` allowlists
`.venv/`, `node_modules/`, and `__pycache__/` so that whole-tree scans
(`gitleaks dir .`) are not buried under false positives from vendored
dependencies — the hook itself never sees those paths, since they are gitignored
and never staged.

## Upgrading gitleaks

```
uv run pre-commit autoupdate
```

That bumps `rev` in `.pre-commit-config.yaml`. Commit the bump, then re-run the
probe above — a new ruleset version can change what fires.
