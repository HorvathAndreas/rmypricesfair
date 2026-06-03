#!/usr/bin/env python3
"""
update_fx.py - Wechselkurse pflegen.

Ohne Argumente werden alle Fremdwaehrungen aus fx_rate gegen die
frankfurter.app-API (ECB-Referenzkurse, kostenlos, ohne API-Key)
aktualisiert. Mit Argumenten laesst sich ein Kurs von Hand setzen.

Aufrufe:
    python update_fx.py                          # alle Kurse refreshen (ECB)
    python update_fx.py EUR 0.94                 # Datum = heute
    python update_fx.py EUR 0.94 2026-06-01      # explizites Datum
    python update_fx.py --list                   # aktuellen Stand anzeigen
    python update_fx.py --db pfad/zur.db ...     # abweichender DB-Pfad
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import httpx

from db import DEFAULT_DB, get_connection, get_fx_rates, init_db, set_fx_rate

FX_API = "https://api.frankfurter.dev/v1/latest"


def _print_rates(conn) -> None:
    rows = conn.execute(
        "SELECT currency, rate_to_chf, updated_at FROM fx_rate ORDER BY currency"
    ).fetchall()
    if not rows:
        print("(keine Kurse hinterlegt)")
        return
    width = max(len(r["currency"]) for r in rows)
    for r in rows:
        stamp = r["updated_at"] or "-"
        print(f"{r['currency']:<{width}}  {r['rate_to_chf']:.4f}  {stamp}")


def _valid_date(s: str) -> str:
    # ISO-Datum, sonst raus.
    dt.date.fromisoformat(s)
    return s


def _refresh_rates(conn) -> int:
    """Holt aktuelle Kurse fuer alle Fremdwaehrungen in fx_rate von frankfurter.app
    (Basis = CHF) und schreibt sie zurueck. CHF selbst wird nie veraendert."""
    foreign = [r[0] for r in conn.execute(
        "SELECT currency FROM fx_rate WHERE currency != 'CHF' ORDER BY currency"
    ).fetchall()]
    if not foreign:
        print("Keine Fremdwaehrungen in fx_rate - nichts zu aktualisieren.")
        return 0
    try:
        resp = httpx.get(FX_API, params={"from": "CHF", "to": ",".join(foreign)}, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Fehler beim Abruf von {FX_API}: {e}", file=sys.stderr)
        return 1
    data = resp.json()
    date = data.get("date") or dt.date.today().isoformat()
    rates = data.get("rates", {})  # 1 CHF = rates[CCY] CCY
    old = get_fx_rates(conn)
    for ccy in foreign:
        if ccy not in rates:
            print(f"{ccy}: nicht in API-Resultat - uebersprungen.", file=sys.stderr)
            continue
        # rate_to_chf = CHF pro 1 Einheit Fremdwaehrung = 1 / (Fremdwaehrung pro 1 CHF)
        new = round(1.0 / rates[ccy], 4)
        prev = old.get(ccy)
        set_fx_rate(conn, ccy, new, date)
        if prev is None:
            print(f"{ccy}: neu gesetzt auf {new:.4f} ({date})")
        elif abs(prev - new) < 1e-9:
            print(f"{ccy}: unveraendert {new:.4f} ({date})")
        else:
            print(f"{ccy}: {prev:.4f} -> {new:.4f} ({date})")
    conn.commit()
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Wechselkurse (Waehrung -> CHF) anzeigen oder setzen.",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad (Standard: data/prices.db)")
    p.add_argument("--list", action="store_true", help="aktuellen Stand anzeigen und beenden")
    p.add_argument("currency", nargs="?", help="ISO-Waehrungscode, z.B. EUR")
    p.add_argument("rate", nargs="?", type=float, help="Kurs zu CHF, z.B. 0.94")
    p.add_argument("date", nargs="?", help="optional, ISO-Datum YYYY-MM-DD (Standard: heute)")
    args = p.parse_args(argv[1:])

    conn = get_connection(args.db)
    init_db(conn)  # idempotent: stellt sicher, dass fx_rate existiert

    if args.list:
        _print_rates(conn)
        return 0

    # Ohne currency+rate: alle Fremdwaehrungen automatisch refreshen.
    if args.currency is None and args.rate is None:
        return _refresh_rates(conn)

    if not args.currency or args.rate is None:
        p.print_usage(sys.stderr)
        print("Fehler: currency und rate gemeinsam angeben (oder --list, oder ohne Args refreshen).",
              file=sys.stderr)
        return 2

    currency = args.currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        print(f"Fehler: ungueltiger Waehrungscode '{args.currency}' (3 Buchstaben erwartet).",
              file=sys.stderr)
        return 2
    if args.rate <= 0:
        print(f"Fehler: rate muss > 0 sein (war {args.rate}).", file=sys.stderr)
        return 2

    if args.date:
        try:
            date = _valid_date(args.date)
        except ValueError:
            print(f"Fehler: ungueltiges Datum '{args.date}' (erwartet YYYY-MM-DD).",
                  file=sys.stderr)
            return 2
    else:
        date = dt.date.today().isoformat()
    old = get_fx_rates(conn).get(currency)
    set_fx_rate(conn, currency, args.rate, date)
    conn.commit()

    if old is None:
        print(f"{currency}: neu gesetzt auf {args.rate:.4f} ({date})")
    elif abs(old - args.rate) < 1e-9:
        print(f"{currency}: unveraendert {args.rate:.4f} ({date})")
    else:
        print(f"{currency}: {old:.4f} -> {args.rate:.4f} ({date})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
