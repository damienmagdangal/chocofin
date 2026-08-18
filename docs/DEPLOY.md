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
| `<TELEGRAM_USER_ID>` | numeric Telegram user id of the household owner |
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

Install `uv` system-wide, on the binary path rather than in a home directory:

```sh
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

`UV_INSTALL_DIR` is the point of that line. Left to itself the installer writes
into `$HOME` — `.local/` for the binary, `.cache/` for the download — and if it
is ever run as `chocofin` it does so in `/opt/chocofin`, which is exactly why
the checkout cannot live there (see below).

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

**`/opt/chocofin` is the service user's home directory, not the checkout.** It
is where tooling run as `chocofin` deposits its own state — `uv`'s installer
alone writes `.local/` and `.cache/` there — so it is not reliably empty, and
`git clone <repo> /opt/chocofin` will fail on it. The code goes in
`/opt/chocofin/app`, created by the clone step in §3:

```
/opt/chocofin/          home: .local/, .cache/, whatever else lands here
/opt/chocofin/app/      the clone: core/, bot/, deploy/, .venv/
```

Both are owned by **root**, not by `chocofin`. The service user reads its own
code and cannot modify it, so a compromise of the bot process is not also a
persistence foothold. Deploys and upgrades run as root.

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

The database-level statements, from the `postgres` session you are already in:

```sql
REVOKE CONNECT ON DATABASE chocofin FROM PUBLIC;
GRANT  CONNECT ON DATABASE chocofin TO chocofin;
ALTER  DATABASE chocofin OWNER TO chocofin;
```

`PUBLIC` is every role that exists now and every role created later. Without the
first `REVOKE`, a role added years from now for some unrelated service can
connect to the household ledger on the day it is created, because `CONNECT` is
granted to `PUBLIC` by default.

Then the schema, in a **separate** `psql` aimed at the target database:

```sh
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname=chocofin <<'SQL'
ALTER SCHEMA public OWNER TO chocofin;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
```

**`--dbname=chocofin` rather than `\c chocofin`, deliberately.** In an
interactive psql a failed `\c` keeps the previous connection open — you are
still on the `postgres` maintenance database, the statements after it apply to
*that* database's `public` schema, and every one of them reports success. Nothing
surfaces until `alembic upgrade head` in §5 dies with `permission denied for
schema public` against a database whose setup apparently went fine. A separate
psql cannot make that mistake: if it cannot reach `chocofin` it exits non-zero
having run nothing at all. `ON_ERROR_STOP=1` is the other half — without it psql
carries on after a failed statement and still exits `0`.

**Ownership rather than grants**, because the schema is not finished. `GRANT
USAGE, CREATE ON SCHEMA public TO chocofin` describes `public` as it is today;
every table, index and sequence a later migration adds is a new object, and
anything the role does not create itself needs `ALTER DEFAULT PRIVILEGES` kept in
sync beside the grant. Making `chocofin` the owner means new objects created by
future migrations inherit correctly with no further grants — revision `0001` and
a revision written two years from now both just work. It also removes a layer of
indirection: Postgres 15+ ships `public` owned by `pg_database_owner` with
`USAGE` still granted to `PUBLIC`, so the effective owner is whoever owns the
database. The `ALTER` states it outright and the `REVOKE ALL` takes back that
residual `USAGE`, neither of which then depends on the server version.

### Verify the lockdown

Both checks run on the db LXC. The first proves the ownership landed on the
database you meant; the second proves the role cannot reach anything else.

**1. `chocofin` can create in `public`, in the right database.**

```sh
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname=chocofin <<'SQL'
SET ROLE chocofin;
SELECT current_database() AS db, current_user AS role;
CREATE TABLE _probe_privileges (id int);
DROP TABLE _probe_privileges;
RESET ROLE;
SQL
echo "exit=$?"
```

Expect the row `chocofin | chocofin` and `exit=0`. `SET ROLE` is what makes this
a real test rather than a superuser doing what superusers can always do:
privilege checks run as `chocofin` from that point, bypass included, so a
schema the role cannot create in fails here exactly as Alembic would —
`ERROR: no schema has been selected to create in`.

This is a privileges test, not a login test. `pg_hba.conf` below admits
`chocofin` only from `<BOT_LXC_IP>`, so there is no way to log in as it from the
db LXC; §5's `alembic current` is the first genuine end-to-end connection.

**2. `chocofin` cannot connect to any other database on this instance.**

`CONNECT` is granted to `PUBLIC` by default on *every* database, and that
includes the maintenance ones — so a role scoped to the ledger can still open a
session on `postgres` or `template1` until you say otherwise:

```sql
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
```

Then assert it, rather than assuming:

```sql
SELECT datname, has_database_privilege('chocofin', datname, 'CONNECT') AS can_connect
FROM pg_database
WHERE datallowconn
ORDER BY datname;
```

`chocofin` must be the only row reading `t`. Any other application database
sharing this instance has its own `PUBLIC` default and needs its own `REVOKE`;
the query lists them all so you cannot miss one.

Three things worth knowing about those revokes:

- **Superusers are unaffected.** `sudo -u postgres psql` keeps working; the
  revoke binds ordinary roles only.
- **A database created later comes back with the `PUBLIC` default.** A new
  database does not inherit `template1`'s ACL — its `datacl` starts null — so
  the revoke does not propagate forward. That is why §9 tells you to re-apply
  it after restoring into a freshly created database, and equally why
  `restore-test.sh` is unaffected: its scratch database is created by, and
  owned by, `chocofin_backup`.
- **`pg_hba.conf` is the independent second layer.** Its database column names
  `chocofin` and nothing else, so a login aimed at another database is refused
  before authentication regardless of any grant. Once §4 is done you can prove
  that from the bot LXC:

  ```sh
  psql "host=<DB_HOST> user=chocofin dbname=postgres sslmode=require" -c 'select 1'
  ```

  It must fail with `FATAL: no pg_hba.conf entry`. A password prompt instead
  means a `pg_hba.conf` line is wider than §2 wrote it.

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
install -d -o root -g root -m 0755 /opt/chocofin/app
git clone https://github.com/damienmagdangal/chocofin.git /opt/chocofin/app
cd /opt/chocofin/app
git checkout <RELEASE_TAG>

uv sync --frozen --no-dev
```

Create `app/` explicitly and clone into it. Do not clone into `/opt/chocofin`
itself — that is the service user's home (§1), it already contains `.local/` and
`.cache/`, and `git clone` refuses a non-empty target.

`--frozen` installs exactly what `uv.lock` pins and fails rather than silently
resolving something new. `--no-dev` leaves pytest and ruff off the production
box.

`ProtectSystem=strict` makes `/opt` read-only to the service, so CPython cannot
write `__pycache__` at runtime. Pre-compile once, at deploy time, so startup
does not pay to recompile on every boot:

```sh
/opt/chocofin/app/.venv/bin/python -m compileall -q /opt/chocofin/app/core /opt/chocofin/app/bot
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

## 5. Migrate and seed

Alembic reads `DATABASE_URL` from the environment (`migrations/env.py`);
`alembic.ini` deliberately has an empty `sqlalchemy.url`.

Look before you leap:

```sh
cd /opt/chocofin/app
set -a; . /etc/chocofin/chocofin.env; set +a
uv run alembic current
uv run alembic history --verbose
```

On an existing database, **take a dump first** — this is the point of no return
for a rollback that involves schema (see §9):

```sh
sudo -u chocofin /opt/chocofin/app/deploy/backup-chocofin.sh
```

Then:

```sh
uv run alembic upgrade head
```

Note that `0002_case_insensitive_names` fails, by design, on a database that
already contains case-duplicate account or category names. That is not a bug to
work around: merging two accounts moves real money between them, and `entries`
is append-only. Reconcile by hand with a void and a replacement, then re-run.

### Seed the first household

A migrated database is empty, and an empty database has no `members` row. The
bot answers a Telegram user because a row there says so, and until `/link` lands
there is no way to write that first row from inside the bot — so `/start` from
your own account is refused until this runs.

`<TELEGRAM_USER_ID>` is the numeric id, not the `@handle`. Any of the id-echo
bots on Telegram will tell you yours; it is not a secret and it is not the
token.

```sh
cd /opt/chocofin/app
set -a; . /etc/chocofin/chocofin.env; set +a

uv run python -m scripts.seed_household \
    --household "Home" \
    --telegram-user-id <TELEGRAM_USER_ID> \
    --display-name "Alex" \
    --account "Wallet:cash:opening=1500" \
    --account "BPI:bank:opening=42350.75" \
    --account "GCash:ewallet:opening=820.50" \
    --account "Visa:credit_card:opening=-3000:limit=50000:billing=BPI"
```

It prints the database it is aimed at and asks you to type that database's name
before it writes anything. `--yes` skips the question — never the echo — and is
for scripts, not for saving four seconds here. `telegram_user_id` is globally
UNIQUE: pointing this at the wrong database grants that Telegram account access
to whatever ledger is there, and a later run cannot take it back.

**`NAME:TYPE` then `key=value`.** Amounts are in **pesos**, converted to
centavos once inside the script; never type centavos, and never a currency
symbol.

| Key | Applies to | If you leave it out |
|---|---|---|
| `opening=` | every account | the account starts at 0 |
| `limit=` | credit cards only | **rejected** |
| `billing=` | credit cards only | **rejected** |

**Get `opening=` right the first time.** Every balance the app shows is
`opening_balance_minor + SUM(legs)`. Seeding zero into an account that already
holds money makes every balance and the net worth wrong from the first screen,
and `entries` is append-only — the only later fix is an adjusting entry for
money that never moved, sitting in the ledger permanently. Count the accounts
now, on the day you deploy.

**A card's opening balance is negative.** Liabilities are negative balances:
`opening=-3000` is three thousand pesos owed. A card seeded positive adds its
debt to net worth instead of subtracting it.

**A card must name both `limit=` and `billing=`, or the run is refused.**
Without a limit, available credit is `NULL` for the life of the card. Without a
billing account, `settle_card` refuses to invent where the money came from and
every `/pay` raises `CardHasNoBillingAccountError`. Half an account is worse
than a clear error, because nothing afterwards tells you it is half.
`billing=` may name an account created anywhere in the same command — order
does not matter — or one already in the household, which is how you add a card
months later.

**Re-running is safe and is the normal way to add an account.** An existing
household, member or account is left alone rather than duplicated, and an
account that already exists keeps the opening balance, credit limit and billing
account it has — the script reports the difference instead of writing over it:

```
account 'Visa' already exists
'Visa' opening balance is -₱3,000.00, not -₱4,500.00 — left alone
```

That is not a failure; it is the script refusing to move balances behind your
back. If the seeded value really is wrong, fix it deliberately — and read the
paragraph above about what "fixing" an opening balance costs once entries exist.

---

## 6. Install the service

```sh
install -m 0644 /opt/chocofin/app/deploy/chocofin-bot.service /etc/systemd/system/
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

Check `/balances` *before* that first expense: every account must read exactly
the `opening=` you gave it in §5, and the card must show available credit rather
than nothing. Then the expense must move one balance by exactly its own amount.
A balance that is wrong by a constant is a wrong `opening=`, and §5 explains why
that is much cheaper to fix now than after a month of entries.

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
ls -l /opt/chocofin/app/deploy/*.sh     # expect -rwxr-xr-x
```

**Fill in `copy_offsite()`** in `deploy/backup-chocofin.sh`. It ships as a stub
that exits non-zero on purpose: a dump that never leaves the box is not a
backup, it is a second copy on the disk that is going to fail. Until you edit
it, the script writes and verifies a local dump and then fails loudly at that
step — which is the correct behaviour, not a bug.

Copy all **three** files it is handed: the `.dump`, the `.meta` holding the
source row counts, and the `.sha256` covering both. A remote dump without its
`.sha256` cannot be checked for bit rot, and one without its `.meta` cannot be
restore-tested at all — there is nothing to compare the restored counts against,
and `restore-test.sh` refuses to run rather than downgrade itself to a weaker
check.

First run by hand, and read the output:

```sh
sudo -u chocofin /opt/chocofin/app/deploy/backup-chocofin.sh
echo "exit=$?"
```

Exit codes: `2` config, `3` preflight, `4` dump, `5` verification, `6` off-box
copy, `7` prune.

Then prove it restores:

```sh
sudo -u chocofin /opt/chocofin/app/deploy/restore-test.sh
echo "exit=$?"
```

This restores the newest dump into a scratch database, checks that all three
DEFERRABLE constraint triggers survived, and verifies on the restored rows that
every entry has legs and every transfer sums to zero — then drops the scratch
database from an `EXIT` trap, on every path.

The row counts are checked **against what the source database actually held**,
not against a threshold. `backup-chocofin.sh` counts `entries`, `entry_legs` and
`accounts` just before it dumps and writes them to a `.meta` sidecar next to the
dump; the restore test reads that file and fails if fewer rows came back. A
plain "zero rows is a failure" rule would be wrong in both directions — it would
fail this very run on a fresh deployment, where an empty database is correct
until the first expense is typed into the bot, and it would happily pass a dump
of 400 entries that restored as 12.

So **an empty result here is a pass on a new deployment** and the script says so
in its output. On an established household it means the source was empty when
the dump was taken, which is worth investigating immediately.

The comparison is `restored >= recorded`, because the counts are taken just
before `pg_dump` opens its snapshot and nothing is ever deleted from those three
tables — a dump can legitimately contain a row typed while it ran, but never
fewer than were there when it started.

### Schedule

```sh
crontab -u chocofin -e
```

```cron
15 3 * * *   /opt/chocofin/app/deploy/backup-chocofin.sh
20 4 1 * *   /opt/chocofin/app/deploy/restore-test.sh
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
cd /opt/chocofin/app
git fetch --tags
git checkout <PREVIOUS_TAG>
uv sync --frozen --no-dev
/opt/chocofin/app/.venv/bin/python -m compileall -q /opt/chocofin/app/core /opt/chocofin/app/bot
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
