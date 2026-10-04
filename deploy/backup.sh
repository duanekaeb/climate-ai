#!/usr/bin/env bash
# Nightly Climate AI database backup: pg_dump (custom format) through `docker compose exec`,
# verified, rotated (keeps the newest 14 by default).
#
#   deploy/backup.sh                         # run from anywhere
#   BACKUP_DIR=/mnt/nas/climate KEEP=30 deploy/backup.sh
#
# Cron (as a user that can run docker), 03:17 every night:
#   17 3 * * * /path/to/climate-ai/deploy/backup.sh >> "$HOME/climate-ai-backup.log" 2>&1
#
# WHAT IS INSIDE: the dump contains the ecobee refresh token and the HomeKit pairing keys
# (the controller's private key), both encrypted with CLIMATE_SECRET_KEY from .env. The dump
# alone cannot decrypt them, and the key alone is useless without the dump, so:
#   * back up CLIMATE_SECRET_KEY (or the whole .env) SEPARATELY, e.g. in your password
#     manager, NOT next to these dumps;
#   * losing the key means signing in to ecobee again and HomeKit-resetting and re-pairing
#     every thermostat.
# The files are written 0600 in a 0700 directory.
#
# Restore (TimescaleDB needs its pre/post-restore calls; same TimescaleDB version):
#   docker compose stop app worker homekit agent mcp
#   docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
#   docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();"'
#   docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < climate-YYYYmmdd-HHMMSS.dump
#   docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT timescaledb_post_restore();"'
#   docker compose up -d
# (docs/DEPLOY.md, "Backups", has the same steps.)

set -euo pipefail
umask 077
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/climate-ai-backups}"
KEEP="${KEEP:-14}"

log() { printf '%s backup: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "KEEP must be a positive integer (got '$KEEP')"
command -v docker >/dev/null 2>&1 || die "docker not found in PATH"

# Run compose from the repository so it finds docker-compose.yml and .env (including any
# COMPOSE_FILE / COMPOSE_PROFILES set there).
cd "$REPO_DIR"
[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in $REPO_DIR"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

docker compose exec -T db pg_isready -q >/dev/null 2>&1 \
  || die "the db service is not running (docker compose ps db)"

stamp="$(date '+%Y%m%d-%H%M%S')"
out="$BACKUP_DIR/climate-$stamp.dump"
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT

log "dumping to $out"
# Custom format: compressed, restorable table by table. TimescaleDB prints harmless
# "circular foreign-key constraints" warnings for its catalog tables; they go to the log.
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' > "$tmp"

[[ -s "$tmp" ]] || die "pg_dump produced an empty file"
# Verify the archive is readable (lists the table of contents without restoring anything).
docker compose exec -T db pg_restore --list < "$tmp" > /dev/null \
  || die "pg_restore could not read the new dump; keeping the previous backups untouched"

mv "$tmp" "$out"
trap - EXIT
log "ok: $(du -h "$out" | cut -f1) $out"

# Rotate: names sort chronologically (climate-YYYYmmdd-HHMMSS.dump); keep the newest $KEEP.
shopt -s nullglob
dumps=("$BACKUP_DIR"/climate-*.dump)
shopt -u nullglob
excess=$(( ${#dumps[@]} - KEEP ))
if (( excess > 0 )); then
  for old in "${dumps[@]:0:excess}"; do
    log "rotating out $old"
    rm -f -- "$old"
  done
fi
log "done; ${#dumps[@]} dump(s) before rotation, keeping at most $KEEP"
