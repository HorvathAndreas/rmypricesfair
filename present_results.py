#!/usr/bin/env python3
"""
present_results.py - Preisvergleichs-Uebersicht.

Vergleicht meine Preise (my_variant.price, CHF-normalisiert) mit den
zuletzt erfassten Mitbewerberpreisen (listing.last_price, CHF-normalisiert)
ueber alle bestaetigten, lieferbaren Listings und gliedert die Ergebnisse
in vier Sektionen:

  1. Preis-Match              - alle Mitbewerber liegen ca. gleich (+/- TOL)
  2. Ich teurer als alle      - jeder Mitbewerber unterbietet mich
  3. Ich guenstiger als alle  - jeder Mitbewerber liegt ueber meinem Preis
  4. Mittelfeld               - Mischbild (einige drueber, einige drunter)

Innerhalb der 'teurer'-Sektion stehen die schlimmsten Faelle oben
(groesster Abstand zum guenstigsten Mitbewerber), in 'guenstiger' die
groesste Aufschlagsreserve oben.

Aufrufe:
    python present_results.py
    python present_results.py --section teurer
    python present_results.py --tol 0.05    # Toleranz fuer Preis-Match
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from db import DEFAULT_DB, get_connection, init_db

DEFAULT_TOL_CHF = 0.01  # Toleranz fuer Preisgleichheit

SECTION_KEYS = ["match", "teurer", "guenstiger", "mittelfeld"]
SECTION_LABELS = {
    "match":      "Preis-Match (alle Mitbewerber gleich)",
    "teurer":     "Ich teurer als alle Mitbewerber",
    "guenstiger": "Ich guenstiger als alle Mitbewerber",
    "mittelfeld": "Mittelfeld",
}


# --- Datenbeschaffung ----------------------------------------------------------

def _fetch_rows(conn) -> list[dict]:
    """Liefert pro Variante my_chf + Liste aller comp-Eintraege (mit Preisen)."""
    rows = conn.execute(
        """
        SELECT
            v.id_product, v.id_product_attribute, v.name, v.variant_label,
            v.price AS my_price, v.currency AS my_currency,
            ROUND(v.price * COALESCE(fxv.rate_to_chf, 1.0), 2) AS my_chf,
            c.name AS comp_name,
            l.last_price AS comp_price, l.last_currency AS comp_currency,
            ROUND(l.last_price * COALESCE(fxc.rate_to_chf, 1.0), 2) AS comp_chf,
            l.comp_url, l.in_stock
        FROM my_variant v
        JOIN listing l
            ON l.id_product = v.id_product
           AND l.id_product_attribute = v.id_product_attribute
        JOIN competitor c ON c.competitor_id = l.competitor_id
        LEFT JOIN fx_rate fxv ON fxv.currency = v.currency
        LEFT JOIN fx_rate fxc ON fxc.currency = l.last_currency
        WHERE v.active = 1 AND l.active = 1 AND l.confirmed = 1
          AND l.last_price IS NOT NULL AND v.price IS NOT NULL
          AND l.match_method != 'no-match'
          AND (l.in_stock IS NULL OR l.in_stock = 1)
        ORDER BY v.id_product, v.id_product_attribute, c.name
        """
    ).fetchall()

    by_var: dict[tuple[int, int], dict] = {}
    for r in rows:
        key = (r["id_product"], r["id_product_attribute"])
        slot = by_var.get(key)
        if slot is None:
            slot = {
                "id_product": r["id_product"],
                "id_product_attribute": r["id_product_attribute"],
                "name": r["name"],
                "variant_label": r["variant_label"],
                "my_chf": r["my_chf"],
                "my_currency": r["my_currency"],
                "my_price": r["my_price"],
                "comps": [],
            }
            by_var[key] = slot
        slot["comps"].append({
            "name": r["comp_name"],
            "chf":  r["comp_chf"],
            "price": r["comp_price"],
            "currency": r["comp_currency"],
            "url": r["comp_url"],
        })
    return list(by_var.values())


# --- Kategorisierung -----------------------------------------------------------

def _categorize(my_chf: float, comp_chfs: list[float], tol: float) -> str:
    diffs = [c - my_chf for c in comp_chfs]
    if all(abs(d) <= tol for d in diffs):
        return "match"
    if all(d < -tol for d in diffs):
        return "teurer"        # alle Mitbewerber liegen unter mir
    if all(d > tol for d in diffs):
        return "guenstiger"    # alle Mitbewerber liegen ueber mir
    return "mittelfeld"


# --- Ausgabe -------------------------------------------------------------------

def _row_label(v: dict, width: int) -> str:
    s = v["name"]
    if v["variant_label"]:
        s += f" [{v['variant_label']}]"
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s


def _fmt_comp(c: dict, my_chf: float, tol: float) -> str:
    diff = c["chf"] - my_chf
    if abs(diff) <= tol:
        marker = "="
    else:
        marker = f"{diff:+.2f}"
    return f"{c['name']}: {c['chf']:.2f} ({marker})"


def _print_section(key: str, items: list[dict], tol: float, name_w: int = 46) -> None:
    label = SECTION_LABELS[key]
    print()
    print(f"═══ {label} ═══  ({len(items)} Varianten)")
    if not items:
        print("  (keine)")
        return
    print()
    print(f"  {'Variante':<{name_w}}  {'meiner':>10}  Mitbewerber")
    print(f"  {'-' * name_w}  {'-' * 10}  {'-' * 50}")
    for v in items:
        comps = ", ".join(_fmt_comp(c, v["my_chf"], tol) for c in v["comps"])
        print(f"  {_row_label(v, name_w):<{name_w}}  "
              f"{v['my_chf']:>7.2f} CHF  {comps}")


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Preisvergleichs-Uebersicht.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad")
    ap.add_argument("--section", choices=SECTION_KEYS,
                    help="nur diese Sektion ausgeben")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL_CHF,
                    help=f"Toleranz CHF fuer Preis-Match (Default {DEFAULT_TOL_CHF})")
    args = ap.parse_args(argv[1:])

    conn = get_connection(args.db)
    init_db(conn)

    items = _fetch_rows(conn)
    if not items:
        print("Keine bestaetigten Listings mit aktuellem Preis vorhanden.")
        return 0

    sections: dict[str, list[dict]] = defaultdict(list)
    for v in items:
        comp_chfs = [c["chf"] for c in v["comps"]]
        cat = _categorize(v["my_chf"], comp_chfs, args.tol)
        sections[cat].append(v)

    # Sortierung pro Sektion: dort wo Handlungsdruck ist, das Dringendste oben.
    sections["match"].sort(key=lambda v: v["name"])
    sections["teurer"].sort(
        key=lambda v: -(v["my_chf"] - min(c["chf"] for c in v["comps"]))
    )
    sections["guenstiger"].sort(
        key=lambda v: -(min(c["chf"] for c in v["comps"]) - v["my_chf"])
    )
    sections["mittelfeld"].sort(key=lambda v: v["name"])

    total_comps = sum(len(v["comps"]) for v in items)
    print(f"\n{len(items)} Varianten verglichen ueber {total_comps} Mitbewerber-Listings "
          f"(Toleranz {args.tol:.2f} CHF).")

    order = [args.section] if args.section else SECTION_KEYS
    for key in order:
        _print_section(key, sections.get(key, []), args.tol)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
