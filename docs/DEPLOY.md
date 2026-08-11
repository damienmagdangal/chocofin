# Deploying ChocoFin

The runbook for the bot. Provisioning, database setup, migration, service
install, verification, backups and rollback.

Two hosts, both Debian 13 (trixie) unprivileged LXCs:

| Host | Runs | Inbound ports |
|---|---|---|
| **bot LXC** | `chocofin-bot.service`, the backup and restore-test scripts | none |
| **db LXC** | Postgres | 5432, from the bot LXC only |

The bot uses Telegram **long polling**, so it opens an outbound HTTPS connection
and nothing listens. There is no reverse proxy, no certificate, no port forward
and no inbound firewall rule to write. If a step below ever seems to want one,
something has gone wrong — see `bot/__main__.py` for why this is a deployment
decision rather than a preference.

## Placeholders

Nothing in `deploy/` contains a credential, hostname or IP. Every environmental
value is one of these, and you substitute it here:

| Placeholder | Meaning |
|---|---|
| `<DB_HOST>` | address of the Postgres LXC |
| `<BOT_LXC_IP>` | address of the bot LXC, as Postgres sees it |
| `<DB_PASSWORD>` | password for the `chocofin` Postgres role |
| `<BACKUP_DB_PASSWORD>` | password for the `chocofin_backup` role |
| `<BOT_TOKEN>` | Telegram bot token from BotFather |
| `<OFFSITE_DEST>` | rsync/rclone target for off-box backup copies |
| `<RELEASE_TAG>` | the git tag being deployed, e.g. `v0.1.0` |

Two rules that make the rest of this document safe to follow:

- **Real values only ever land in `/etc/chocofin/*.env`**, mode `0600`. Never in
  a unit file, never in a script, never in a shell you ran interactively (see
  "Keeping secrets out of history" at the end).
- **A password never goes on a command line.** `psql -c "CREATE ROLE ... PASSWORD
  '...'"` puts it in the process table and your shell history. Use `\password`
  or a heredoc, as shown below.

---

## 1. Provision the bot LXC

```sh
apt update && apt install -y python3.13 python3.13-venv git postgresql-client curl
```

`postgresql-client` is not optional — it is what `pg_dump`, `pg_restore` and
`psql` come from, and the backup scripts refuse to start without them.

Install `uv` system-wide so the service user does not need a writable home:

```sh
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

The service account. A system user with no login shell and no password: it
exists to own a process, not to be logged into.

```sh
useradd --system --home-dir /opt/chocofin --shell /usr/sbin/nologin chocofin
```

Directories:

```sh
install -d -o root     -g root     -m 0755 /opt/chocofin
install -d -o chocofin -g chocofin -m 0750 /etc/chocofin
install -d -o chocofin -g chocofin -m 0700 /var/backups/chocofin
```

`/opt/chocofin` is owned by **root**, not by `chocofin`. The service user reads
its own code and cannot modify it, so a compromise of the bot process is not
also a persistence foothold. Deploys and upgrades run as root.

---

## 2. Database and roles

On the **db LXC**. Everything here runs as the `postgres` superuser.

### Create the role and database

```sh
sudo -u postgres psql
```

```sql
CREATE ROLE chocofin LOGIN;
\password chocofin          -- prompts; keeps <DB_PASSWORD> out of the process table

CREATE DATABASE chocofin
    OWNER    chocofin
    ENCODING 'UTF8'
    LOCALE   'C.UTF-8'
    TEMPLATE template0;
```

`C.UTF-8` rather than a country locale, deliberately. Category names contain
emoji and glibc's collation for those has changed across releases; an index
built under one collation and read under another is silently wrong. `C.UTF-8` is
stable across glibc upgrades, and the case-insensitive uniqueness this schema
needs comes from `lower(name)` functional indexes (migration `0002`), not from
collation. No extensions are required — nothing here needs `citext` or superuser
rights beyond this section.

### Lock down access

```sql
REVOKE CONNECT ON DATABASE chocofin FROM PUBLIC;
GRANT  CONNECT ON DATABASE chocofin TO chocofin;

\c chocofin
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO chocofin;
```

`PUBLIC` is every role that exists now and every role created later. Without the
first `REVOKE`, a role added years from now for some unrelated service can
connect to the household ledger on the day it is created, because `CONNECT` is
granted to `PUBLIC` by default. Postgres 15+ already removes `CREATE` on schema
`public` from `PUBLIC`; the `REVOKE ALL` above is explicit about it so this does
not silently depend on the server version.

### The backup role

`pg_dump` should not run as the role that owns the data and can drop it.

```sql
CREATE ROLE chocofin_backup LOGIN CREATEDB;
\password chocofin_backup

GRANT CONNECT ON DATABASE chocofin TO chocofin_backup;
GRANT pg_read_all_data TO chocofin_backup;
```

`pg_read_all_data` is read-only across the whole database, which is all a dump
needs. `CREATEDB` is there for `restore-test.sh`, which creates and drops its
own scratch database — it cannot write to `chocofin` itself.

### Network access

`postgresql.conf`:

```
listen_addresses = '<DB_HOST>'
```

`pg_hba.conf` — one line per role, scoped to a single host, TLS required:

```
hostssl  chocofin  chocofin         <BOT_LXC_IP>/32  scram-sha-256
hostssl  chocofin  chocofin_backup  <BOT_LXC_IP>/32  scram-sha-256
```

`hostssl` rather than `host`: the ledger crosses a network between two
containers, and `host` would happily accept a cleartext session. Then:

```sh
systemctl reload postgresql
```

---

## 3. Deploy the code

As root on the bot LXC. Always deploy a tag, never a branch — a rollback needs
something to roll back *to*.

```sh
git clone https://github.com/damienmagdangal/chocofin.git /opt/chocofin
cd /opt/chocofin
git checkout <RELEASE_TAG>

uv sync --frozen --no-dev
```

`--frozen` installs exactly what `uv.lock` pins and fails rather than silently
resolving something new. `--no-dev` leaves pytest and ruff off the production
box.

`ProtectSystem=strict` makes `/opt` read-only to the service, so CPython cannot
write `__pycache__` at runtime. Pre-compile once, at deploy time, so startup
does not pay to recompile on every boot:

```sh
/opt/chocofin/.venv/bin/python -m compileall -q /opt/chocofin/core /opt/chocofin/bot
```

---

## 4. Environment files

### `/etc/chocofin/chocofin.env` — the service

```sh
install -o chocofin -g chocofin -m 0600 /dev/null /etc/chocofin/chocofin.env
```

Contents:

```
TELEGRAM_BOT_TOKEN=<BOT_TOKEN>
DATABASE_URL=postgresql+asyncpg://chocofin:<DB_PASSWORD>@<DB_HOST>:5432/chocofin?ssl=require
```

Both are read by `core/config.py`, which has no defaults for either and raises
`ConfigError` if one is missing. That is the intended behaviour: a token with a
fallback value is a token that eventually gets committed.

`?ssl=require` is asyncpg's parameter, and it matches the `hostssl` lines in
`pg_hba.conf`. Without it the client will try a plaintext connection first.

`TEST_DATABASE_URL` must **not** be set on this box. Nothing in production reads
it, and `tests/conftest.py` aborts the whole session if it ever points at a
database whose name does not end in `_test`.

### `/etc/chocofin/backup.env` — the backup scripts

```sh
install -o chocofin -g chocofin -m 0600 /dev/null /etc/chocofin/backup.env
```

Contents:

```sh
PGHOST=<DB_HOST>
PGPORT=5432
PGUSER=chocofin_backup
PGDATABASE=chocofin
PGPASSFILE=/etc/chocofin/.pgpass

BACKUP_DIR=/var/backups/chocofin
RETENTION_DAYS=30
MIN_KEEP=3

OFFSITE_DEST=<OFFSITE_DEST>
```

This file is `source`d by both scripts, so it is shell, not dotenv — quote
anything containing a space.

### `/etc/chocofin/.pgpass`

```sh
install -o chocofin -g chocofin -m 0600 /dev/null /etc/chocofin/.pgpass
```

One line, `host:port:database:user:password`:

```
<DB_HOST>:5432:chocofin:chocofin_backup:<BACKUP_DB_PASSWORD>
```

**The mode matters.** libpq ignores a `.pgpass` that is group- or
world-readable and says nothing about it; the symptom is `pg_dump` hanging on a
password prompt inside cron, forever, with no output. `backup-chocofin.sh`
checks the mode at startup and refuses to run rather than let you debug that at
3am.

---

## 5. Migrate

Alembic reads `DATABASE_URL` from the environment (`migrations/env.py`);
`alembic.ini` deliberately has an empty `sqlalchemy.url`.

Look before you leap:

```sh
cd /opt/chocofin
set -a; . /etc/chocofin/chocofin.env; set +a
uv run alembic current
uv run alembic history --verbose
```

On an existing database, **take a dump first** — this is the point of no return
for a rollback that involves schema (see §9):

```sh
sudo -u chocofin /opt/chocofin/deploy/backup-chocofin.sh
```

Then:

```sh
uv run alembic upgrade head
```

Note that `0002_case_insensitive_names` fails, by design, on a database that
already contains case-duplicate account or category names. That is not a bug to
work around: merging two accounts moves real money between them, and `entries`
is append-only. Reconcile by hand with a void and a replacement, then re-run.

---

## 6. Install the service

```sh
install -m 0644 /opt/chocofin/deploy/chocofin-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now chocofin-bot.service
```

---

## 7. First-run verification

Do all six. Each one catches something the others do not.

**1. The service is up and stayed up.**

```sh
systemctl status chocofin-bot.service
```

Look for `active (running)` *and* an uptime longer than `RestartSec` — a
crash-looping unit shows `active (running)` a few seconds out of every ten.

**2. The database connected.**

```sh
journalctl -u chocofin-bot.service -b --no-pager
```

Expect `database engine ready` from `_post_init`. Its absence means the pool
never opened, regardless of what `systemctl status` says.

**3. Nothing is listening.**

```sh
ss -ltnp
```

The bot must not appear. Long polling opens outbound connections only; a
listening socket here means something is very wrong.

**4. The bot answers.** Send `/start` from an authorised Telegram account, then
add a real expense and confirm it against the account balance.

**5. Authorisation rejects a stranger.** Message the bot from an account that is
not a household member. It must refuse, and the refusal must come from the
single decorator in `bot/auth.py` — if you find yourself adding a user-id check
inside a handler to make this pass, stop.

**6. The hardening actually applied.**

```sh
systemd-analyze security chocofin-bot.service
```

Expect a score around 1.5–2.0 ("OK"). More useful than the number: nothing in
the list should say a directive was ignored. See §10 if the unit will not start.

---

## 8. Backups

The scripts run from the clone; there is nothing to copy. Both are stored in git
with mode `100755`, so a fresh clone gets them executable. Confirm rather than
assume — the bit is easy to lose through an archive export or a copy from a
Windows share:

```sh
ls -l /opt/chocofin/deploy/*.sh     # expect -rwxr-xr-x
```

**Fill in `copy_offsite()`** in `deploy/backup-chocofin.sh`. It ships as a stub
that exits non-zero on purpose: a dump that never leaves the box is not a
backup, it is a second copy on the disk that is going to fail. Until you edit
it, the script writes and verifies a local dump and then fails loudly at that
step — which is the correct behaviour, not a bug.

First run by hand, and read the output:

```sh
sudo -u chocofin /opt/chocofin/deploy/backup-chocofin.sh
echo "exit=$?"
```

Exit codes: `2` config, `3` preflight, `4` dump, `5` verification, `6` off-box
copy, `7` prune.

Then prove it restores:

```sh
sudo -u chocofin /opt/chocofin/deploy/restore-test.sh
echo "exit=$?"
```

This restores the newest dump into a scratch database, counts `entries` and
`accounts`, checks that all three DEFERRABLE constraint triggers survived, and
verifies on the restored rows that every entry has legs and every transfer sums
to zero — then drops the scratch database from an `EXIT` trap, on every path.
**Zero rows is a failure, not a pass**; a backup that restores into an empty
database succeeds at every mechanical step and is worthless.

### Schedule

```sh
crontab -u chocofin -e
```

```cron
15 3 * * *   /opt/chocofin/deploy/backup-chocofin.sh
20 4 1 * *   /opt/chocofin/deploy/restore-test.sh
```

No `MAILTO`. This LXC has no MTA, so cron's stderr mail goes nowhere. Both
scripts log to the journal instead:

```sh
journalctl -t chocofin-backup --since -7d
journalctl -t chocofin-restore-test --since -60d
```

### Monitor the stamp file, not the script

A script that fails tells you it failed. A script that **never runs** — cron
disabled, LXC restored from an old snapshot, crontab lost — tells you nothing at
all, and that silence looks exactly like success. Every successful backup
touches `/var/backups/chocofin/last-success`. Alert on its age:

```sh
find /var/backups/chocofin/last-success -mmin +1500 | grep -q . && echo STALE
```

1500 minutes is 25 hours: one missed nightly run, plus an hour of slack.

---

## 9. Rollback

Code and schema roll back differently, and conflating them is how a bad deploy
becomes a bad week.

### Code only — the migration was additive, or there was none

The safe, common case.

```sh
cd /opt/chocofin
git fetch --tags
git checkout <PREVIOUS_TAG>
uv sync --frozen --no-dev
/opt/chocofin/.venv/bin/python -m compileall -q /opt/chocofin/core /opt/chocofin/bot
systemctl restart chocofin-bot.service
```

Then re-run the checks in §7.

How to know it was additive: compare revisions.

```sh
set -a; . /etc/chocofin/chocofin.env; set +a
uv run alembic current                    # what the database is at
git diff <PREVIOUS_TAG>..<RELEASE_TAG> -- migrations/versions/
```

If the new revisions only add tables, columns or indexes, the old code ignores
them and this is all you need. Leave the schema forward — an unused column is
harmless.

### Schema too — the migration was destructive

```sh
uv run alembic downgrade <PREVIOUS_REVISION>
```

Every migration in this repo defines a working `downgrade()`, so this path
exists. But understand what it does not do: a `downgrade()` that drops a column
does not bring back the data that was in it. If the upgrade removed or rewrote
anything, `downgrade` restores the *shape* and not the *contents*.

**When in doubt, restore the pre-upgrade dump instead.** That is what §5 told
you to take, and this is what it was for.

```sh
# On the db LXC, with the bot stopped:
systemctl stop chocofin-bot.service        # on the bot LXC, first

sudo -u postgres psql -c 'ALTER DATABASE chocofin RENAME TO chocofin_broken_<DATE>;'
sudo -u postgres createdb -O chocofin chocofin
sudo -u postgres pg_restore --dbname=chocofin --exit-on-error /path/to/chocofin-<STAMP>.dump
```

Rename rather than drop. The broken database costs disk and nothing else, and
it is the only copy of whatever went wrong; drop it a week later, once the
household ledger has been reconciled by eye and you are sure. Re-apply the
`REVOKE CONNECT` from §2 afterwards — a freshly created database gets the
default `PUBLIC` grants back.

Then start the bot and work through §7 again.

---

## 10. Troubleshooting

**The unit will not start, with `SIGSYS`, or an `mmap`/`mprotect` error.**
Comment out `MemoryDenyWriteExecute=yes`, `daemon-reload`, retry. If that fixes
it, a dependency now wants writable-executable memory — worth knowing which one
before you leave the directive off permanently.

**The unit will not start, with a namespace or mount error.** Unprivileged LXCs
do not expose every kernel feature systemd's sandboxing wants. Relax in this
order, testing after each: `ProtectProc`/`ProcSubset`, then `PrivateDevices`,
then `ProtectSystem=strict` → `ProtectSystem=full`. `NoNewPrivileges`,
`PrivateTmp` and `ProtectHome` work everywhere and should be the last things you
touch. `journalctl -u chocofin-bot -b` names the directive it failed on.

`IPAddressDeny`/`IPAddressAllow` are deliberately not in the unit: they need
eBPF, which an unprivileged container generally cannot install, and would be
either a hard failure or — worse — silently ignored while looking like an egress
filter. Restrict egress at the LXC or host firewall instead.

**`pg_dump` hangs in cron but works by hand.** `/etc/chocofin/.pgpass` is not
mode `0600`. libpq ignores it silently. `backup-chocofin.sh` checks this at
startup; if you are seeing the hang, something is invoking `pg_dump` directly.

**`FATAL: no pg_hba.conf entry ... SSL off`.** `?ssl=require` is missing from
`DATABASE_URL`, or the `pg_hba.conf` line says `host` where §2 says `hostssl`.

**`permission denied for database chocofin`.** The `GRANT CONNECT` after the
`REVOKE CONNECT` in §2 was skipped. The revoke removes it from `PUBLIC`, which
includes the role you just created.

**The bot answers but every entry fails to save.** Check the journal for the
constraint trigger names in §8. A restore done without `--exit-on-error` can
leave a database with tables but no triggers.

---

## Keeping secrets out of history

`core/config.py` refuses to hold the token or the database URL in code, and a
gitleaks pre-commit hook backstops it (see `docs/secret-scanning.md`). Neither
helps with the two ways a credential escapes during a deploy:

- **Your shell history on the bot LXC.** Do not paste a `DATABASE_URL` into an
  interactive command. Use `set -a; . /etc/chocofin/chocofin.env; set +a`, as
  every example above does.
- **The process table.** `psql -c "... PASSWORD 'x'"` is visible to every user
  on the box for as long as it runs. Use `\password`, as §2 does.

If a credential does escape, rotating it is the only real fix: a new token from
BotFather, `\password` for the Postgres role, then update
`/etc/chocofin/chocofin.env` and `restart`.
