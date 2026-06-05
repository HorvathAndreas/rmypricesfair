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
DEFAULT_MIN_COMP_CHF = 1.0  # Mitbewerber-Preise darunter gelten als Platzhalter
                            # (z.B. 0.01 CHF fuer "Preis auf Anfrage") und werden
                            # nicht in den Vergleich gezogen.

SECTION_KEYS = ["match", "teurer", "guenstiger", "mittelfeld"]
SECTION_LABELS = {
    "match":      "Preis-Match (alle Mitbewerber gleich)",
    "teurer":     "Ich teurer als alle Mitbewerber",
    "guenstiger": "Ich guenstiger als alle Mitbewerber",
    "mittelfeld": "Mittelfeld",
}


# --- Datenbeschaffung ----------------------------------------------------------

def _fetch_rows(conn, min_comp_chf: float) -> tuple[list[dict], int]:
    """Liefert pro Variante den Master-Preis (CH-Listenpreis) + Liste aller
    comp-Eintraege. Pro Mitbewerber wird der eigene Preis aus
    my_variant_price[competitor.country_iso] gezogen; fehlt der Eintrag,
    faellt der Vergleich auf den Master-Preis (FX-umgerechnet) zurueck.

    Liefert zusaetzlich die Anzahl Mitbewerber-Listings, die wegen
    Platzhalter-Preis (comp_chf < min_comp_chf) ausgefiltert wurden."""
    rows = conn.execute(
        """
        SELECT
            v.id_product, v.id_product_attribute, v.name, v.variant_label,
            v.price AS my_master_price, v.currency AS my_master_currency,
            ROUND(v.price * COALESCE(fxmaster.rate_to_chf, 1.0), 2) AS my_master_chf,
            -- Land-spezifischer Eigenpreis (z.B. DE) mit Fallback aufs Master
            COALESCE(mvp.price, v.price) AS my_price,
            COALESCE(mvp.currency, v.currency) AS my_currency,
            ROUND(COALESCE(mvp.price, v.price)
                  * COALESCE(fxmy.rate_to_chf, 1.0), 2) AS my_chf,
            mvp.country_iso AS my_country,
            c.name AS comp_name,
            c.country_iso AS comp_country,
            l.last_price AS comp_price, l.last_currency AS comp_currency,
            ROUND(l.last_price * COALESCE(fxc.rate_to_chf, 1.0), 2) AS comp_chf,
            l.comp_url, l.in_stock
        FROM my_variant v
        JOIN listing l
            ON l.id_product = v.id_product
           AND l.id_product_attribute = v.id_product_attribute
        JOIN competitor c ON c.competitor_id = l.competitor_id
        LEFT JOIN my_variant_price mvp
            ON mvp.id_product = v.id_product
           AND mvp.id_product_attribute = v.id_product_attribute
           AND mvp.country_iso = c.country_iso
        LEFT JOIN fx_rate fxmaster ON fxmaster.currency = v.currency
        LEFT JOIN fx_rate fxmy     ON fxmy.currency = COALESCE(mvp.currency, v.currency)
        LEFT JOIN fx_rate fxc      ON fxc.currency = l.last_currency
        WHERE v.active = 1 AND l.active = 1 AND l.confirmed = 1
          AND l.last_price IS NOT NULL AND v.price IS NOT NULL
          AND l.match_method != 'no-match'
          AND (l.in_stock IS NULL OR l.in_stock = 1)
        ORDER BY v.id_product, v.id_product_attribute, c.name
        """
    ).fetchall()
    n_dropped = sum(1 for r in rows if r["comp_chf"] is not None
                                       and r["comp_chf"] < min_comp_chf)
    rows = [r for r in rows if r["comp_chf"] is None
                               or r["comp_chf"] >= min_comp_chf]

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
                # Der angezeigte 'meiner'-Preis im Header ist der Master.
                "my_chf": r["my_master_chf"],
                "my_currency": r["my_master_currency"],
                "my_price": r["my_master_price"],
                "comps": [],
            }
            by_var[key] = slot
        slot["comps"].append({
            "name": r["comp_name"],
            "country": r["comp_country"],
            "chf":  r["comp_chf"],
            "price": r["comp_price"],
            "currency": r["comp_currency"],
            "url": r["comp_url"],
            # Pro Mitbewerber der eigene, ggf. land-spezifische Preis in CHF;
            # die Diff fuer match/teurer/guenstiger laeuft gegen DIESEN Wert,
            # nicht gegen den Header-Master-Preis.
            "my_chf": r["my_chf"],
            "my_country": r["my_country"],   # None, wenn nur Fallback genutzt
        })
    return list(by_var.values()), n_dropped


# --- Kategorisierung -----------------------------------------------------------

def _categorize(comp_diffs: list[float], tol: float) -> str:
    """Kategorisierung basiert auf pro-Mitbewerber-Diffs (comp_chf - my_chf),
    wobei my_chf der land-spezifische Eigenpreis ist."""
    if not comp_diffs:
        return "match"
    if all(abs(d) <= tol for d in comp_diffs):
        return "match"
    if all(d < -tol for d in comp_diffs):
        return "teurer"        # alle Mitbewerber liegen unter mir
    if all(d > tol for d in comp_diffs):
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


def _fmt_comp(c: dict, tol: float) -> str:
    """Format: 'name(LAND): comp_chf (diff)'. Diff = comp_chf - per-comp my_chf.
    Das Laender-Tag macht sichtbar, gegen welchen Eigenpreis verglichen wird."""
    diff = c["chf"] - c["my_chf"]
    if abs(diff) <= tol:
        marker = "="
    else:
        marker = f"{diff:+.2f}"
    tag = f"({c['country']})" if c["country"] else ""
    return f"{c['name']}{tag}: {c['chf']:.2f} ({marker})"


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
        comps = ", ".join(_fmt_comp(c, tol) for c in v["comps"])
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
    ap.add_argument("--min-comp-price", type=float, default=DEFAULT_MIN_COMP_CHF,
                    help=f"Mitbewerber-Preise unter diesem CHF-Wert als "
                         f"Platzhalter werten und ignorieren "
                         f"(Default {DEFAULT_MIN_COMP_CHF})")
    args = ap.parse_args(argv[1:])

    conn = get_connection(args.db)
    init_db(conn)

    items, n_dropped = _fetch_rows(conn, args.min_comp_price)
    if not items:
        print("Keine bestaetigten Listings mit aktuellem Preis vorhanden.")
        return 0

    sections: dict[str, list[dict]] = defaultdict(list)
    for v in items:
        # Diffs sind comp_chf - per-comp-my_chf, damit DE-Comp gegen DE-Preis
        # und CH-Comp gegen CH-Preis verglichen wird.
        comp_diffs = [c["chf"] - c["my_chf"] for c in v["comps"]]
        cat = _categorize(comp_diffs, args.tol)
        sections[cat].append(v)

    # Sortierung pro Sektion: dort wo Handlungsdruck ist, das Dringendste oben.
    sections["match"].sort(key=lambda v: v["name"])
    sections["teurer"].sort(
        # Groesster Abstand zu meinem (Land-)Eigenpreis nach oben.
        key=lambda v: -max(c["my_chf"] - c["chf"] for c in v["comps"])
    )
    sections["guenstiger"].sort(
        key=lambda v: -max(c["chf"] - c["my_chf"] for c in v["comps"])
    )
    sections["mittelfeld"].sort(key=lambda v: v["name"])

    total_comps = sum(len(v["comps"]) for v in items)
    print(f"\n{len(items)} Varianten verglichen ueber {total_comps} Mitbewerber-Listings "
          f"(Toleranz {args.tol:.2f} CHF).")
    if n_dropped:
        print(f"  ({n_dropped} Mitbewerber-Listings unter {args.min_comp_price:.2f} CHF "
              f"als Platzhalter ignoriert.)")

    order = [args.section] if args.section else SECTION_KEYS
    for key in order:
        _print_section(key, sections.get(key, []), args.tol)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
