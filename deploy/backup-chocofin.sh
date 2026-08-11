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
TMP_DUMP="$(mktemp "${BACKUP_DIR}/.in-progress.XXXXXXXX")"
TOC_FILE="${TMP_DUMP}.toc"

# A partial dump left lying around is the dangerous kind of garbage: it looks
# like a backup. Remove the work files on every exit path, successful or not.
cleanup() { rm -f -- "$TMP_DUMP" "$TOC_FILE"; }
trap cleanup EXIT

info "starting backup of database '${PGDATABASE}' -> ${FINAL}"

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

( cd "$BACKUP_DIR" && sha256sum -- "$(basename "$FINAL")" > "$(basename "$FINAL").sha256" ) \
    || die $EX_VERIFY "failed to write checksum sidecar for ${FINAL}"

info "dump verified: $(du -h -- "$FINAL" | cut -f1) at ${FINAL}"

# --- off-box copy ----------------------------------------------------------

copy_offsite() {
    local dump=$1 sidecar=$2

    # -----------------------------------------------------------------------
    # FILL THIS IN.
    #
    # Rules for whatever replaces the `die` below:
    #   * it must exit non-zero on failure -- no `|| true`, no `&`
    #   * host, user, key and remote path come from ${CONFIG_FILE}, never from
    #     this file
    #   * copy the .sha256 sidecar too, or the remote copy cannot be checked
    #
    # rsync over SSH:
    #
    #   rsync --archive --chmod=F600 \
    #         -e "ssh -i ${OFFSITE_SSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=yes" \
    #         -- "$dump" "$sidecar" "${OFFSITE_DEST}/" \
    #     || return 1
    #
    # rclone to object storage:
    #
    #   rclone copy --config "${RCLONE_CONFIG}" -- "$dump"    "${OFFSITE_DEST}" || return 1
    #   rclone copy --config "${RCLONE_CONFIG}" -- "$sidecar" "${OFFSITE_DEST}" || return 1
    #
    # Whichever you use, verify the bytes arrived rather than trusting exit 0 --
    # e.g. `rclone check` on the pair, or an `ssh ... sha256sum -c` against the
    # sidecar. A transport that reports success on a truncated upload is exactly
    # the failure this whole script is built to avoid.
    # -----------------------------------------------------------------------

    die $EX_OFFSITE "copy_offsite() is still the stub shipped with the repo -- edit deploy/backup-chocofin.sh and configure OFFSITE_DEST (see docs/DEPLOY.md). The local dump at ${dump} IS complete and verified; it just has not left the box, so it does not yet count as a backup."
}

copy_offsite "$FINAL" "${FINAL}.sha256"
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
    # makes that explicit rather than relying on it.
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        \( -name 'chocofin-*.dump' -o -name 'chocofin-*.dump.sha256' \) \
        ! -name "${newest_name}" ! -name "${newest_name}.sha256" \
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
