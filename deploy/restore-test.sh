#!/usr/bin/env bash
#
# ChocoFin restore test -- the script that turns a backup from a theory into a
# fact. Restores the newest dump into a scratch database, compares its row
# counts against the ones backup-chocofin.sh recorded in the .meta sidecar at
# dump time, checks the schema arrived intact, and drops it.
#
# Run it by hand after any change to backup-chocofin.sh, and on a schedule --
# monthly is enough, and cheap:
#
#     20 4 1 * *  /opt/chocofin/deploy/restore-test.sh
#
# An untested backup is a guess. The failure this catches is not "the file is
# missing" -- it is "the file restored, but not all of it, and nobody looked".
#
# Shares /etc/chocofin/backup.env with the backup script. No credential,
# hostname or IP in this file. See docs/DEPLOY.md.

set -Eeuo pipefail

readonly TAG="chocofin-restore-test"

readonly EX_CONFIG=2
readonly EX_PREFLIGHT=3
readonly EX_NODUMP=4
readonly EX_CHECKSUM=5
readonly EX_RESTORE=6
readonly EX_ASSERT=7

log() {
    local level=$1
    shift
    printf '%s %-5s %s\n' "$(date -Is)" "$level" "$*" >&2
    logger -t "$TAG" -p "user.${level}" -- "$*" 2>/dev/null || true
}
info() { log info "$@"; }
err()  { log err "$@"; }

die() {
    local code=$1
    shift
    err "$*"
    exit "$code"
}

on_error() {
    local code=$? line=$1
    err "UNHANDLED FAILURE at line ${line} (exit ${code}) -- restore test did NOT pass"
    exit "$code"
}
trap 'on_error $LINENO' ERR

# --- configuration ---------------------------------------------------------

CONFIG_FILE="${CHOCOFIN_BACKUP_CONFIG:-/etc/chocofin/backup.env}"

[[ -r "$CONFIG_FILE" ]] || die $EX_CONFIG "config file ${CONFIG_FILE} is missing or unreadable"
# shellcheck source=/dev/null
source "$CONFIG_FILE"

for required in PGHOST PGPORT PGUSER PGDATABASE PGPASSFILE BACKUP_DIR; do
    [[ -n "${!required:-}" ]] || die $EX_CONFIG "${required} is not set in ${CONFIG_FILE}"
done

# CREATE DATABASE needs a connection to something that is not the database
# being created. Never the live one.
MAINTENANCE_DB="${MAINTENANCE_DB:-postgres}"

export PGHOST PGPORT PGUSER PGPASSFILE

# --- preflight -------------------------------------------------------------

for binary in pg_restore psql createdb sha256sum find sed; do
    command -v "$binary" >/dev/null 2>&1 \
        || die $EX_PREFLIGHT "required command '${binary}' is not on PATH"
done

[[ -d "$BACKUP_DIR" ]] || die $EX_PREFLIGHT "backup directory ${BACKUP_DIR} does not exist"

# --- pick the newest dump --------------------------------------------------

DUMP="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'chocofin-*.dump' -printf '%T@ %p\n' \
        | sort -rn | head -n1 | cut -d' ' -f2-)"

[[ -n "$DUMP" ]] || die $EX_NODUMP "no chocofin-*.dump found in ${BACKUP_DIR} -- there is nothing to test"

info "testing newest dump: ${DUMP} ($(du -h -- "$DUMP" | cut -f1), modified $(date -Is -r "$DUMP"))"

META="${DUMP}.meta"

# Written by backup-chocofin.sh at dump time; it holds the row counts the
# assertions below are judged against. Without it this script can only check
# that the restore mechanically succeeded, which is the weaker test it used to
# be -- so treat its absence exactly like a missing checksum and stop.
[[ -f "$META" ]] \
    || die $EX_CHECKSUM "no metadata sidecar at ${META} -- cannot tell how many rows this dump is supposed to contain"

# --- checksum --------------------------------------------------------------
# Catches bit rot on the backup volume, which is silent by definition and is a
# real failure mode for a dump that sits untouched for weeks.
#
# The sidecar lists the dump AND the .meta, so this one check covers both. That
# matters: the counts in the .meta are what every assertion below is measured
# against, and a rotted or edited meta would move the goalposts silently. This
# fails first instead.

if [[ -f "${DUMP}.sha256" ]]; then
    if ! ( cd "$BACKUP_DIR" && sha256sum --check --quiet -- "$(basename "${DUMP}.sha256")" ); then
        die $EX_CHECKSUM "checksum mismatch on ${DUMP} -- the file on disk is not the file that was written"
    fi
    info "checksum ok"
else
    die $EX_CHECKSUM "no checksum sidecar at ${DUMP}.sha256 -- cannot tell whether this dump is intact"
fi

# --- scratch database ------------------------------------------------------

SCRATCH="chocofin_restoretest_$$_$(date -u +%Y%m%d%H%M%S)"

# Belt and braces. Nothing below should be able to touch the live database, so
# make that structurally true rather than merely intended.
[[ "$SCRATCH" != "$PGDATABASE" ]] \
    || die $EX_PREFLIGHT "scratch name collided with the live database name -- refusing to continue"
[[ "$SCRATCH" == *_restoretest_* ]] \
    || die $EX_PREFLIGHT "scratch name '${SCRATCH}' does not carry the _restoretest_ marker -- refusing to continue"

psql_scratch() {
    psql --no-psqlrc --tuples-only --no-align --quiet \
         --set=ON_ERROR_STOP=1 --dbname="$SCRATCH" "$@"
}

dropped=no
drop_scratch() {
    [[ "$dropped" == no ]] || return 0
    dropped=yes
    # WITH (FORCE) terminates leftover backends, so a half-finished pg_restore
    # holding a connection cannot strand a scratch database on the live server.
    # Postgres 13+.
    psql --no-psqlrc --quiet --dbname="$MAINTENANCE_DB" \
         --command="DROP DATABASE IF EXISTS \"${SCRATCH}\" WITH (FORCE);" >/dev/null 2>&1 \
        || err "could not drop scratch database ${SCRATCH} -- drop it by hand"
}
# Registered before the database exists, so no exit path can leak one.
trap drop_scratch EXIT

info "creating scratch database ${SCRATCH}"
createdb -- "$SCRATCH" \
    || die $EX_RESTORE "createdb failed -- does ${PGUSER} have CREATEDB? (see docs/DEPLOY.md)"

# --- restore ---------------------------------------------------------------
# --no-owner/--no-acl because the scratch database belongs to whoever runs this,
# not to the live owner role, and the GRANT/REVOKE lines in the dump refer to
# roles this test has no business recreating.
#
# --exit-on-error because pg_restore's default is to log errors, carry on, and
# exit 0 -- which would turn a broken restore into a passing test, the exact
# outcome this script exists to prevent.
#
# --single-transaction so a failed restore leaves nothing behind. Note what it
# does NOT do: the constraint triggers live in the dump's post-data section, so
# they are created after the COPYs and never fire during the load. Restoring
# cleanly therefore proves the triggers EXIST, not that the restored rows would
# satisfy them -- which is why the ledger invariants are checked directly below.

if ! pg_restore --dbname="$SCRATCH" --no-owner --no-acl --exit-on-error \
        --single-transaction -- "$DUMP"; then
    die $EX_RESTORE "pg_restore failed -- this backup is NOT restorable"
fi

info "restore completed without error"

# --- assertions ------------------------------------------------------------

count_rows() {
    psql_scratch --command="SELECT count(*) FROM ${1};" | tr -d '[:space:]'
}

# The row counts recorded at dump time. Anything that is not a bare integer is
# fatal rather than defaulted: a missing key comes back as the empty string,
# compares as 0, and makes every assertion below trivially true -- which is the
# silent pass this script exists to prevent.
#
# Read in the main shell, not from a function called in `$( )`, so that `die`
# ends the script rather than just the subshell it was called in.
declare -A SRC_ROWS=()
for key in rows_entries rows_entry_legs rows_accounts; do
    # The trailing `q` stops at the first match, so a duplicated key cannot
    # silently concatenate two values into something that is not a number.
    meta_value="$(sed -n "/^${key}=/{s///;p;q;}" -- "$META")"
    [[ "$meta_value" =~ ^[0-9]+$ ]] \
        || die $EX_ASSERT "metadata key '${key}' in ${META} is '${meta_value}', not a number -- the recorded counts are unusable, so this dump cannot be verified"
    SRC_ROWS[$key]="$meta_value"
done

src_entries="${SRC_ROWS[rows_entries]}"
src_legs="${SRC_ROWS[rows_entry_legs]}"
src_accounts="${SRC_ROWS[rows_accounts]}"

entries_count="$(count_rows entries)"
accounts_count="$(count_rows accounts)"
legs_count="$(count_rows entry_legs)"

info "row counts -- entries=${entries_count}/${src_entries} accounts=${accounts_count}/${src_accounts} entry_legs=${legs_count}/${src_legs} (restored/recorded at dump time)"

# Compare against what the source actually held, not against a threshold.
#
# A `> 0` check is wrong at both ends. It fails the first restore test of a new
# deployment, where the database is empty by design until someone types the
# first expense into the bot -- and that run is the one gating the deploy. It
# also passes a dump of 400 entries that restored as 12, which is the exact
# disaster worth catching.
#
# `>=` rather than `==` because backup-chocofin.sh takes these counts just
# before pg_dump opens its snapshot, and nothing ever deletes from these three
# tables (entries are append-only; a correction voids and inserts). So the dump
# can legitimately hold a row or two more than was recorded -- an expense typed
# while the dump ran -- but it can never hold fewer.
if (( src_entries == 0 && src_legs == 0 && src_accounts == 0 )); then
    info "note: the source database held no entries, legs or accounts when this dump was taken, so the checks below confirm an empty ledger round-tripped -- expected on a new deployment, worth investigating on an established one"
fi

(( entries_count >= src_entries )) \
    || die $EX_ASSERT "restored entries (${entries_count}) is fewer than the ${src_entries} recorded at dump time -- rows were lost in the round trip"
(( accounts_count >= src_accounts )) \
    || die $EX_ASSERT "restored accounts (${accounts_count}) is fewer than the ${src_accounts} recorded at dump time -- rows were lost in the round trip"
(( legs_count >= src_legs )) \
    || die $EX_ASSERT "restored entry_legs (${legs_count}) is fewer than the ${src_legs} recorded at dump time -- rows were lost in the round trip"
(( legs_count >= entries_count )) \
    || die $EX_ASSERT "entry_legs (${legs_count}) < entries (${entries_count}) -- every entry must have at least one leg, so legs were lost"

# Schema state, not just data. A dump restored without alembic_version leaves a
# database that `alembic upgrade head` would try to migrate from scratch.
version_rows="$(count_rows alembic_version)"
(( version_rows == 1 )) \
    || die $EX_ASSERT "alembic_version has ${version_rows} row(s), expected exactly 1 -- schema state did not survive the round trip"
alembic_head="$(psql_scratch --command='SELECT version_num FROM alembic_version;' | tr -d '[:space:]')"
info "alembic revision in dump: ${alembic_head}"

# The DEFERRABLE constraint triggers are the ledger's real integrity guarantee
# (see migrations/versions/0001_baseline.py). pg_dump carries them, but a dump
# taken with the wrong flags would not -- and a restored ledger that accepts an
# unbalanced transfer is not the ledger that was backed up.
for trigger in trg_entry_legs_validate trg_entries_validate_legs trg_categories_validate_depth; do
    present="$(psql_scratch --command="SELECT count(*) FROM pg_trigger WHERE tgname = '${trigger}' AND NOT tgisinternal;" | tr -d '[:space:]')"
    (( present == 1 )) \
        || die $EX_ASSERT "constraint trigger '${trigger}' is missing from the restored database"
done
info "all three constraint triggers present"

# The triggers did not run during the load (see above), so check the invariants
# they enforce against the restored rows directly. This is the difference
# between "the file restored" and "the ledger came back intact".

orphan_entries="$(psql_scratch --command='
    SELECT count(*) FROM entries e
    WHERE NOT EXISTS (SELECT 1 FROM entry_legs l WHERE l.entry_id = e.id);' | tr -d '[:space:]')"
(( orphan_entries == 0 )) \
    || die $EX_ASSERT "${orphan_entries} restored entries have no legs -- entry_legs data was lost"

unbalanced="$(psql_scratch --command="
    SELECT count(*) FROM (
        SELECT e.id
        FROM entries e
        JOIN entry_legs l ON l.entry_id = e.id
        WHERE e.kind = 'transfer'
        GROUP BY e.id
        HAVING sum(l.amount_minor) <> 0
    ) AS bad;" | tr -d '[:space:]')"
(( unbalanced == 0 )) \
    || die $EX_ASSERT "${unbalanced} restored transfers do not sum to zero -- the restored ledger is not internally consistent"

info "ledger invariants hold on restored data: every entry has legs, every transfer sums to zero"

# --- done ------------------------------------------------------------------
# drop_scratch runs from the EXIT trap.

info "RESTORE TEST PASSED for ${DUMP}"
exit 0
