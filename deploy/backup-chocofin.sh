#!/usr/bin/env bash
#
# ChocoFin nightly backup: pg_dump -Fc, verify, copy off-box, prune.
#
# Cron -- install with `crontab -u chocofin -e`:
#
#     15 3 * * *  /opt/chocofin/deploy/backup-chocofin.sh
#
# Deliberately no MAILTO. A minimal Debian LXC has no MTA, so cron's stderr
# mail goes nowhere, and a failure that reports only that way is a failure
# nobody hears. Every message below also goes to the journal:
#
#     journalctl -t chocofin-backup --since -7d
#
# And because a script that never runs cannot report its own absence, each
# success touches a stamp file. Alert on ITS age, not on this script's output:
#
#     find /var/backups/chocofin/last-success -mmin +1500 | grep -q . && echo STALE
#
# No credential, hostname or IP appears in this file. Everything environmental
# comes from the config file below. See docs/DEPLOY.md.

set -Eeuo pipefail

readonly TAG="chocofin-backup"

# Distinct codes so the failing stage is visible in cron's exit status alone.
readonly EX_CONFIG=2
readonly EX_PREFLIGHT=3
readonly EX_DUMP=4
readonly EX_VERIFY=5
readonly EX_OFFSITE=6
readonly EX_PRUNE=7

# --- logging ---------------------------------------------------------------
# Two channels on purpose: stderr for an interactive run, syslog for cron. The
# `|| true` guards only the logger call -- if syslog is unavailable we still
# want the stderr line and the non-zero exit, not a crash inside the error path.

log() {
    local level=$1
    shift
    printf '%s %-5s %s\n' "$(date -Is)" "$level" "$*" >&2
    logger -t "$TAG" -p "user.${level}" -- "$*" 2>/dev/null || true
}
info() { log info "$@"; }
warn() { log warning "$@"; }
err()  { log err "$@"; }

die() {
    local code=$1
    shift
    err "$*"
    exit "$code"
}

on_error() {
    local code=$? line=$1
    err "UNHANDLED FAILURE at line ${line} (exit ${code}) -- this run produced no usable backup"
    exit "$code"
}
trap 'on_error $LINENO' ERR

# --- configuration ---------------------------------------------------------
# Sourced, not parsed, so it can compute values. Must be mode 0600 and owned by
# the user cron runs this as. Required keys are checked immediately below;
# docs/DEPLOY.md has the annotated template.

CONFIG_FILE="${CHOCOFIN_BACKUP_CONFIG:-/etc/chocofin/backup.env}"

[[ -r "$CONFIG_FILE" ]] || die $EX_CONFIG "config file ${CONFIG_FILE} is missing or unreadable"
# shellcheck source=/dev/null
source "$CONFIG_FILE"

for required in PGHOST PGPORT PGUSER PGDATABASE PGPASSFILE BACKUP_DIR RETENTION_DAYS; do
    [[ -n "${!required:-}" ]] || die $EX_CONFIG "${required} is not set in ${CONFIG_FILE}"
done

# Keep at least this many dumps regardless of age, so a fortnight of failed
# runs followed by one success cannot leave a single dump on the disk.
MIN_KEEP="${MIN_KEEP:-3}"
LOCK_FILE="${LOCK_FILE:-${BACKUP_DIR}/.backup.lock}"

# Exported so pg_dump, pg_restore and psql all pick them up without repeating
# flags. PGPASSFILE keeps the password out of the process table and out of the
# environment file's exported values.
export PGHOST PGPORT PGUSER PGDATABASE PGPASSFILE

# --- preflight -------------------------------------------------------------

for binary in pg_dump pg_restore psql sha256sum flock find; do
    command -v "$binary" >/dev/null 2>&1 \
        || die $EX_PREFLIGHT "required command '${binary}' is not on PATH"
done

[[ -d "$BACKUP_DIR" ]] || die $EX_PREFLIGHT "backup directory ${BACKUP_DIR} does not exist"
[[ -w "$BACKUP_DIR" ]] || die $EX_PREFLIGHT "backup directory ${BACKUP_DIR} is not writable by $(id -un)"

# libpq ignores a .pgpass that is group- or world-readable, and says nothing
# about it. The symptom is pg_dump hanging on a password prompt under cron,
# which is a miserable thing to debug at 3am, so check it here instead.
[[ -f "$PGPASSFILE" ]] || die $EX_PREFLIGHT "PGPASSFILE ${PGPASSFILE} does not exist"
pgpass_mode="$(stat -c '%a' -- "$PGPASSFILE")"
[[ "$pgpass_mode" == "600" ]] \
    || die $EX_PREFLIGHT "PGPASSFILE ${PGPASSFILE} is mode ${pgpass_mode}; libpq silently ignores anything but 0600"

# Only one at a time. An overrun run racing its successor would have them both
# writing temp files and both pruning.
exec 9>"$LOCK_FILE"
flock -n 9 || die $EX_PREFLIGHT "another run holds ${LOCK_FILE}; refusing to overlap"

umask 077

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="${BACKUP_DIR}/chocofin-${STAMP}.dump"
META="${FINAL}.meta"
TMP_DUMP="$(mktemp "${BACKUP_DIR}/.in-progress.XXXXXXXX")"
TOC_FILE="${TMP_DUMP}.toc"

# A partial dump left lying around is the dangerous kind of garbage: it looks
# like a backup. Remove the work files on every exit path, successful or not.
cleanup() { rm -f -- "$TMP_DUMP" "$TOC_FILE"; }
trap cleanup EXIT

info "starting backup of database '${PGDATABASE}' -> ${FINAL}"

# --- source row counts -----------------------------------------------------
# Recorded so restore-test.sh can compare what came back against what was here,
# rather than against a hardcoded threshold. A `> 0` check is wrong in both
# directions: it fails the very first restore test on a fresh deploy, where an
# empty database is correct and expected, and it passes a dump of 400 entries
# that restores as 12.
#
# Taken BEFORE pg_dump opens its snapshot, and this ordering is the whole basis
# of the comparison. `entries` and `entry_legs` are append-only -- a correction
# voids and inserts, it never UPDATEs or DELETEs -- and nothing removes an
# account. So the snapshot pg_dump takes a moment from now can only hold MORE
# rows than these numbers, never fewer, which is why restore-test.sh asserts
# `restored >= recorded`.
#
# Counting after the dump instead would invert that: an expense typed into the
# bot at 03:16 would put the count above the dump and fail a restore test on a
# backup that is perfectly good.

count_source_rows() {
    psql --no-psqlrc --tuples-only --no-align --quiet --set=ON_ERROR_STOP=1 \
         --dbname="$PGDATABASE" --command="SELECT count(*) FROM ${1};" \
        | tr -d '[:space:]'
}

declare -A SRC_ROWS=()
for table in entries entry_legs accounts; do
    if ! count="$(count_source_rows "$table")"; then
        die $EX_DUMP "could not count rows in '${table}' -- refusing to write a dump whose contents cannot be verified later"
    fi
    # An empty string from a psql that failed quietly would be recorded, then
    # read back as 0, and would make every later comparison trivially true.
    [[ "$count" =~ ^[0-9]+$ ]] \
        || die $EX_DUMP "row count for '${table}' was '${count}', not a number -- refusing to record it"
    SRC_ROWS[$table]="$count"
done

info "source row counts -- entries=${SRC_ROWS[entries]} entry_legs=${SRC_ROWS[entry_legs]} accounts=${SRC_ROWS[accounts]}"

# --- dump ------------------------------------------------------------------
# Custom format because it is what pg_restore needs for selective restore, and
# because restore-test.sh reads its table of contents to verify the file.
#
# No --no-owner/--no-acl here: the dump should carry everything, including the
# REVOKE CONNECT grants set up in DEPLOY.md. Dropping ownership is a restore-time
# decision, and restore-test.sh makes it there.

if ! pg_dump --format=custom --compress=9 --no-password \
        --file="$TMP_DUMP" -- "$PGDATABASE"; then
    die $EX_DUMP "pg_dump failed for database '${PGDATABASE}' -- no backup written"
fi

[[ -s "$TMP_DUMP" ]] || die $EX_DUMP "pg_dump exited 0 but produced an empty file"

# --- verify ----------------------------------------------------------------
# pg_dump exiting 0 means it finished writing, not that the result is readable.
# Reading the archive's table of contents back catches truncation and
# corruption now, rather than during an actual restore.

if ! pg_restore --list -- "$TMP_DUMP" > "$TOC_FILE" 2>/dev/null; then
    die $EX_VERIFY "dump is unreadable: pg_restore --list failed on the file just written"
fi

# The ledger's core tables must be present in the archive. This proves the dump
# covers the right database and that nothing was silently excluded; it does NOT
# prove the tables have rows -- an empty table still appears here. Row counts
# are restore-test.sh's job, which is the script that makes this backup real.
for table in entries entry_legs accounts households; do
    if ! grep -qE "TABLE DATA[[:space:]]+public[[:space:]]+${table}([[:space:]]|$)" "$TOC_FILE"; then
        die $EX_VERIFY "dump has no TABLE DATA entry for '${table}' -- wrong database, or a filtered dump"
    fi
done

# Atomic: the finished name never exists until the file behind it is complete
# and verified, so a crash mid-dump cannot leave something that looks restorable.
mv -- "$TMP_DUMP" "$FINAL"

# The counts travel with the dump. Plain key=value so restore-test.sh can read
# it with sed and a human can read it with cat; the dump's own name is in here
# too, so a meta file that got separated from its dump is obvious rather than
# silently applied to the wrong one.
{
    printf 'dump=%s\n'            "$(basename "$FINAL")"
    printf 'taken_at=%s\n'        "$STAMP"
    printf 'database=%s\n'        "$PGDATABASE"
    printf 'rows_entries=%s\n'    "${SRC_ROWS[entries]}"
    printf 'rows_entry_legs=%s\n' "${SRC_ROWS[entry_legs]}"
    printf 'rows_accounts=%s\n'   "${SRC_ROWS[accounts]}"
} > "$META" || die $EX_VERIFY "failed to write metadata sidecar ${META}"

# Both files in the one sidecar, on purpose. The counts are what the restore
# test is judged against, so they need the same bit-rot and truncation
# protection as the dump itself -- and this way restore-test.sh's existing
# `sha256sum --check` covers the pair with no extra machinery. A meta file that
# rotted, was edited, or arrived truncated off-box fails there, before any
# comparison is made against numbers that can no longer be trusted.
( cd "$BACKUP_DIR" \
    && sha256sum -- "$(basename "$FINAL")" "$(basename "$META")" > "$(basename "$FINAL").sha256" ) \
    || die $EX_VERIFY "failed to write checksum sidecar for ${FINAL}"

info "dump verified: $(du -h -- "$FINAL" | cut -f1) at ${FINAL}"

# --- off-box copy ----------------------------------------------------------

copy_offsite() {
    local dump=$1 meta=$2 sidecar=$3

    # -----------------------------------------------------------------------
    # FILL THIS IN.
    #
    # Rules for whatever replaces the `die` below:
    #   * it must exit non-zero on failure -- no `|| true`, no `&`
    #   * host, user, key and remote path come from ${CONFIG_FILE}, never from
    #     this file
    #   * copy all THREE files. Without the .sha256 the remote copy cannot be
    #     checked; without the .meta it cannot be restore-tested, because there
    #     is nothing to compare the restored row counts against.
    #
    # rsync over SSH:
    #
    #   rsync --archive --chmod=F600 \
    #         -e "ssh -i ${OFFSITE_SSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=yes" \
    #         -- "$dump" "$meta" "$sidecar" "${OFFSITE_DEST}/" \
    #     || return 1
    #
    # rclone to object storage:
    #
    #   rclone copy --config "${RCLONE_CONFIG}" -- "$dump"    "${OFFSITE_DEST}" || return 1
    #   rclone copy --config "${RCLONE_CONFIG}" -- "$meta"    "${OFFSITE_DEST}" || return 1
    #   rclone copy --config "${RCLONE_CONFIG}" -- "$sidecar" "${OFFSITE_DEST}" || return 1
    #
    # Whichever you use, verify the bytes arrived rather than trusting exit 0 --
    # e.g. `rclone check` on the pair, or an `ssh ... sha256sum -c` against the
    # sidecar. A transport that reports success on a truncated upload is exactly
    # the failure this whole script is built to avoid.
    # -----------------------------------------------------------------------

    die $EX_OFFSITE "copy_offsite() is still the stub shipped with the repo -- edit deploy/backup-chocofin.sh and configure OFFSITE_DEST (see docs/DEPLOY.md). The local dump at ${dump} IS complete and verified; it just has not left the box, so it does not yet count as a backup."
}

copy_offsite "$FINAL" "$META" "${FINAL}.sha256"
info "off-box copy complete"

# --- prune -----------------------------------------------------------------
# Reached only after a verified dump AND a successful off-box copy. Pruning
# before either would trade old good backups for a new bad one.

prune() {
    local newest_name total
    newest_name="$(basename "$FINAL")"

    total="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'chocofin-*.dump' | wc -l)"
    if (( total <= MIN_KEEP )); then
        info "prune skipped: ${total} dump(s) on disk, keeping at least ${MIN_KEEP}"
        return 0
    fi

    # -mtime +N cannot match the file created moments ago, and the name guard
    # makes that explicit rather than relying on it. The .meta and .sha256 ages
    # out with the dump it describes; keeping either behind would leave a
    # sidecar pointing at a file that is gone.
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        \( -name 'chocofin-*.dump' -o -name 'chocofin-*.dump.sha256' -o -name 'chocofin-*.dump.meta' \) \
        ! -name "${newest_name}" ! -name "${newest_name}.sha256" ! -name "${newest_name}.meta" \
        -mtime "+${RETENTION_DAYS}" \
        -print -delete
}

if ! pruned="$(prune)"; then
    die $EX_PRUNE "retention prune failed -- the new backup is safe, but old dumps are accumulating"
fi
# An `[[ ... ]] && info` one-liner would abort the script under `set -e` on the
# ordinary day when nothing is old enough to prune.
if [[ -n "$pruned" ]]; then
    info "pruned:"$'\n'"${pruned}"
fi

# --- success ---------------------------------------------------------------
# The stamp file's mtime is the thing to monitor. It is the only signal that
# distinguishes "backups are fine" from "cron has not fired in three weeks",
# because a script that never runs cannot log its own silence.

date -Is > "${BACKUP_DIR}/last-success"

info "backup complete: ${FINAL}"
exit 0
