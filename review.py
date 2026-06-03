#!/usr/bin/env python3
"""
review.py - interaktiver Dialog: Match-Kandidaten sichten und bestaetigen.

Geht alle Varianten durch, fuer die match_candidate-Vorschlaege existieren und
noch kein bestaetigtes Listing (confirmed=1) angelegt ist. Pro Variante zeigt
das Tool die Top-Kandidaten und nimmt eine Entscheidung entgegen:

    1..N   Kandidaten-Nummer uebernehmen
              -> upsert_listing(..., confirmed=1)
              -> alle Kandidaten dieser Variante werden geloescht
    s      Skip - Entscheidung vertagen
    n      None - kein Treffer; legt ein 'no-match' Listing mit confirmed=1 an
              (verhindert, dass die Variante beim naechsten matcher-Lauf
               wieder Kandidaten produziert, bis das Listing manuell entfernt wird)
    o N    URL des Kandidaten N ausgeben (zum Kopieren in den Browser)
    q      Quit

Aufrufe:
    python review.py                   # alle aktiven Mitbewerber mit Kandidaten
    python review.py NAME              # nur dieser Mitbewerber
    python review.py --min-score 0.7   # nur Varianten mit best_score >= Schwelle
"""

from __future__ import annotations

import argparse
import sys

from db import (DEFAULT_DB, delete_candidates_for_variant, get_active_competitors,
                get_candidates, get_connection, get_variants_with_candidates,
                init_db, upsert_listing)


def _trunc(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_variant_header(v, idx: int, total: int) -> None:
    print()
    print("─" * 80)
    title = v["name"]
    if v["variant_label"]:
        title += f"   [{v['variant_label']}]"
    print(f"[{idx}/{total}]  {title}")
    print(f"  id_product={v['id_product']}/{v['id_product_attribute']}   "
          f"Preis: {v['price']} {v['currency'] or ''}   "
          f"EAN: {v['ean13'] or '-'}   Ref: {v['reference'] or '-'}")
    print(f"  Kandidaten gesamt: {v['n_candidates']}   "
          f"bester Score: {v['best_score']:.2f}")


def _print_candidates(cands) -> None:
    print()
    print("  Kandidaten (Score / Methode / Name / Ref):")
    for i, c in enumerate(cands, 1):
        line = (f"  {i}) [{c['score']:.2f} {c['method']:6}] "
                f"{_trunc(c['comp_name'], 56)}")
        print(line)
        extras = []
        if c["comp_reference"]:
            extras.append(f"ref={c['comp_reference']}")
        if c["comp_ean13"]:
            extras.append(f"ean={c['comp_ean13']}")
        if c["comp_url"]:
            extras.append(_trunc(c["comp_url"], 70))
        if extras:
            print(f"       {'   '.join(extras)}")


def _confirm_candidate(conn, v, c, competitor_id: int) -> None:
    """Schreibt ein bestaetigtes Listing aus einem Kandidaten und raeumt auf."""
    upsert_listing(conn, {
        "id_product": v["id_product"],
        "id_product_attribute": v["id_product_attribute"],
        "competitor_id": competitor_id,
        "comp_name": c["comp_name"],
        "comp_reference": c["comp_reference"],
        "comp_ean13": c["comp_ean13"],
        "comp_upc": c["comp_upc"],
        "comp_url": c["comp_url"],
        "comp_variant_ref": c["comp_variant_ref"],
        "match_method": c["method"],
        "confirmed": 1,
    })
    delete_candidates_for_variant(conn, v["id_product"],
                                  v["id_product_attribute"], competitor_id)
    conn.commit()


def _mark_no_match(conn, v, competitor_id: int) -> None:
    """Markiert die Variante als 'kein Treffer'. Wir nutzen ein leeres
    Listing mit match_method='no-match' und confirmed=1, damit es nicht
    erneut auftaucht."""
    upsert_listing(conn, {
        "id_product": v["id_product"],
        "id_product_attribute": v["id_product_attribute"],
        "competitor_id": competitor_id,
        "comp_name": None, "comp_reference": None, "comp_ean13": None,
        "comp_upc": None, "comp_url": None, "comp_variant_ref": None,
        "match_method": "no-match",
        "confirmed": 1,
    })
    delete_candidates_for_variant(conn, v["id_product"],
                                  v["id_product_attribute"], competitor_id)
    conn.commit()


def _review_competitor(conn, comp, min_score: float) -> str:
    """Liefert 'quit' wenn der User abbricht, sonst 'done'."""
    rows = get_variants_with_candidates(conn, comp["competitor_id"])
    rows = [r for r in rows if (r["best_score"] or 0) >= min_score]
    total = len(rows)
    if total == 0:
        print(f"\n[{comp['name']}] Keine offenen Varianten "
              f"(min-score={min_score:.2f}).")
        return "done"

    print(f"\n=== {comp['name']} === {total} offene Varianten "
          f"(min-score={min_score:.2f})")

    for idx, v in enumerate(rows, 1):
        cands = get_candidates(conn, v["id_product"],
                               v["id_product_attribute"], comp["competitor_id"])
        if not cands:
            # zwischendrin geloescht (paranoid)
            continue
        _print_variant_header(v, idx, total)
        _print_candidates(cands)
        while True:
            try:
                raw = input("  [1-{}] | s skip | n none | o N url | q quit > "
                            .format(len(cands))).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAbbruch.")
                return "quit"
            if not raw:
                continue
            if raw == "q":
                return "quit"
            if raw == "s":
                break  # naechste Variante
            if raw == "n":
                _mark_no_match(conn, v, comp["competitor_id"])
                print(f"  -> als 'no-match' gespeichert.")
                break
            if raw.startswith("o"):
                # 'o', 'o 2', 'o2' - URL ausgeben
                parts = raw.split()
                arg = parts[1] if len(parts) > 1 else raw[1:]
                if not arg.isdigit():
                    print("  -> Bitte 'o N' mit Kandidaten-Nummer.")
                    continue
                k = int(arg)
                if not (1 <= k <= len(cands)):
                    print(f"  -> Nummer ausserhalb 1..{len(cands)}.")
                    continue
                print(f"  URL: {cands[k - 1]['comp_url'] or '(keine URL)'}")
                continue
            if raw.isdigit():
                k = int(raw)
                if not (1 <= k <= len(cands)):
                    print(f"  -> Nummer ausserhalb 1..{len(cands)}.")
                    continue
                _confirm_candidate(conn, v, cands[k - 1], comp["competitor_id"])
                print(f"  -> Kandidat {k} bestaetigt.")
                break
            print("  -> Eingabe nicht erkannt.")
    return "done"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Match-Kandidaten interaktiv reviewen.")
    ap.add_argument("name", nargs="?", help="competitor.name (Standard: alle aktiven)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="nur Varianten mit best_score >= Schwelle anzeigen")
    args = ap.parse_args(argv[1:])

    if not sys.stdin.isatty():
        print("review.py ist interaktiv und braucht ein TTY.", file=sys.stderr)
        return 2

    conn = get_connection(args.db)
    init_db(conn)
    comps = get_active_competitors(conn)
    if args.name:
        comps = [c for c in comps if c["name"] == args.name]
        if not comps:
            print(f"Aktiver Mitbewerber '{args.name}' nicht gefunden.", file=sys.stderr)
            return 2
    if not comps:
        print("Keine aktiven Mitbewerber.", file=sys.stderr)
        return 1

    for c in comps:
        result = _review_competitor(conn, c, args.min_score)
        if result == "quit":
            print("\nFortschritt gespeichert. Nochmal 'review.py' fuer den Rest.")
            break

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
