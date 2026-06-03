#!/usr/bin/env python3
"""
init_db.py - Datenbank anlegen (oeffentlicher Einstiegspunkt).

Wenn die DB-Datei noch nicht existiert, wird sie mit dem Schema aus db.py
angelegt. Existiert sie bereits, wird ausdruecklich nachgefragt, ob sie
geloescht und neu erstellt werden soll - sonst Abbruch ohne Aenderung.

Aufrufe:
    python init_db.py                    # Standardpfad data/prices.db
    python init_db.py pfad/zur.db        # alternativer Pfad
    python init_db.py --force            # ohne Rueckfrage loeschen+neu anlegen
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db import DEFAULT_DB, get_connection, init_db


def _ask_confirm_delete(path: Path) -> bool:
    """Nur 'ja' (case-insensitive) bestaetigt. Alles andere = Nein."""
    if not sys.stdin.isatty():
        # Nicht-interaktiv: niemals stillschweigend loeschen.
        print(
            f"DB {path} existiert bereits. Nicht-interaktive Sitzung - "
            "fuer Loeschen+Neuanlage --force verwenden.",
            file=sys.stderr,
        )
        return False
    print(f"DB {path} existiert bereits.")
    print("Bestehende Datei wird unwiderruflich GELOESCHT und neu angelegt.")
    answer = input("Wirklich loeschen? Tippe 'ja' zum Bestaetigen: ").strip().lower()
    return answer == "ja"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Legt die SQLite-DB mit dem Schema aus db.py an.",
    )
    p.add_argument("path", nargs="?", default=str(DEFAULT_DB),
                   help="DB-Pfad (Standard: data/prices.db)")
    p.add_argument("--force", action="store_true",
                   help="bestehende DB ohne Rueckfrage loeschen und neu anlegen")
    args = p.parse_args(argv[1:])

    path = Path(args.path)

    if path.exists():
        if not (args.force or _ask_confirm_delete(path)):
            print("Abbruch. DB unveraendert.")
            return 1
        path.unlink()
        print(f"Bestehende DB geloescht: {path}")

    conn = get_connection(path)
    init_db(conn)
    conn.close()
    print(f"Schema initialisiert: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
