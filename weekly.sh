#!/bin/bash
# weekly.sh - woechentlicher Sync- und Update-Lauf.
#
# Reihenfolge (idempotent, jeder Schritt ist sicher mehrfach ausfuehrbar):
#   1. update_fx.py    Wechselkurse aktualisieren (frankfurter.dev, ECB-Basis)
#   2. catalog.py      eigene Shops -> my_variant + my_variant_price
#   3. matcher.py      Mitbewerber-Kataloge crawlen, Kandidaten/Hart-Matches
#   4. updater.py      aktuelle Preise fuer confirmed=1 Listings
#
# Aufruf:
#   ./weekly.sh                      (nutzt .venv/bin/python im Repo-Root)
#
# Vorgesehen fuer cron, z.B. Sonntag 03:00:
#   0 3 * * 0  /home/andreas/rmypricesfair/weekly.sh
#
# Mehrfach-Start wird via flock verhindert. Logs unter logs/weekly-YYYY-MM-DD.log,
# die letzten LOG_KEEP bleiben, aeltere werden geloescht.

set -uo pipefail

# Repo-Root ermitteln (folgt Symlinks). Damit ist das Script auch via cron
# unabhaengig vom $PWD.
cd "$(dirname "$(readlink -f "$0")")"

PY=".venv/bin/python"
LOG_DIR="logs"
LOG_KEEP=8
LOCK_FILE="/tmp/rmypricesfair-weekly.lock"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly-$(date +%Y-%m-%d).log"

# Mehrfach-Start verhindern.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Iseconds)] weekly.sh laeuft schon (Lock $LOCK_FILE), abbrechen." >&2
    exit 1
fi

# Alles ab hier in den Log-File - und gleichzeitig nach stdout/stderr.
exec > >(tee -a "$LOG_FILE") 2>&1

if [ ! -x "$PY" ]; then
    echo "Python-venv nicht gefunden ($PY). Bitte .venv anlegen oder PY anpassen." >&2
    exit 2
fi

step() {
    echo
    echo "==================================================================="
    echo "  $(date -Iseconds)  $1"
    echo "==================================================================="
}

# Einen Schritt ausfuehren; Exit-Code loggen, aber den Gesamtlauf nicht
# abbrechen - ein gescheiterter Mitbewerber soll nicht den Rest blockieren.
run() {
    local name=$1; shift
    step "$name"
    if "$@"; then
        echo "--- $name OK"
    else
        local rc=$?
        echo "!!! $name fehlgeschlagen mit exit $rc - weiter mit naechstem Job" >&2
    fi
}

echo "weekly.sh start $(date -Iseconds)  Repo: $(pwd)"

run "update_fx"  "$PY" update_fx.py
run "catalog"    "$PY" catalog.py
run "matcher"    "$PY" matcher.py
run "updater"    "$PY" updater.py

step "Aufraeumen alte Logs (behalte $LOG_KEEP)"
# ls -t sortiert nach Zeit (neueste oben), tail +N+1 wirft die ersten N raus.
ls -1t "$LOG_DIR"/weekly-*.log 2>/dev/null \
    | tail -n +$((LOG_KEEP + 1)) \
    | xargs -r rm -v --

echo
echo "weekly.sh fertig $(date -Iseconds)"
