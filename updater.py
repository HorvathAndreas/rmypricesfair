#!/usr/bin/env python3
"""
updater.py - Wochenlauf: aktuelle Mitbewerber-Preise in confirmed listings schreiben.

Fuer jeden aktiven Mitbewerber:
  1) Katalog 1x ueber den passenden Plattform-Fetcher holen.
  2) Records nach variant_ref / reference / ean13 indizieren.
  3) Pro confirmed=1 Listing den aktuellen Preis schreiben (update_listing_price).
     Vorrang beim Lookup: comp_variant_ref > comp_reference > comp_ean13.
     - 'no-match' Listings werden uebersprungen.
     - Faellt der Lookup oder der Fetcher fehl: Zeile bleibt unangetastet
       (Vorgabe aus CLAUDE.md: kein 'Aenderung auf NULL' bei Fehlschlag).
     - 'price_changed_at' wird nur bei echter Preisaenderung gesetzt
       (Logik in db.update_listing_price).

Aufrufe:
    python updater.py                # alle aktiven Mitbewerber
    python updater.py NAME           # nur einen Mitbewerber
"""

from __future__ import annotations

import argparse
import json
import sys

from db import (DEFAULT_DB, get_active_competitors, get_confirmed_listings,
                get_connection, init_db, update_listing_price)

import woocommerce
import schema_org
import shopify

# Plattform -> Fetcher. Vertrag: fetch(base_url, **fetcher_config) ->
# list[dict]. fetcher_config kommt pro Mitbewerber als JSON aus der DB und
# wird als kwargs durchgereicht.
FETCHERS = {
    "woocommerce": woocommerce.fetch,
    "schema_org": schema_org.fetch,
    "shopify": shopify.fetch,
}

MAX_CHANGES_PRINTED = 20  # nur die ersten N Preisaenderungen pro Mitbewerber zeigen


# --- Indizes + Lookup ----------------------------------------------------------

def _build_indices(records: list[dict]) -> tuple[dict, dict, dict]:
    by_var: dict[str, dict] = {}
    by_ref: dict[str, dict] = {}
    by_ean: dict[str, dict] = {}
    for r in records:
        vr = r.get("variant_ref")
        if vr:
            by_var.setdefault(str(vr), r)
        ref = (r.get("reference") or "").strip()
        if ref:
            by_ref.setdefault(ref, r)
        ean = (r.get("ean13") or "").strip()
        if ean:
            by_ean.setdefault(ean, r)
    return by_var, by_ref, by_ean


def _find_match(listing, by_var, by_ref, by_ean):
    """Sucht den passenden comp record fuer ein Listing. Liefert (rec, via) oder (None, None)."""
    vr = listing["comp_variant_ref"]
    if vr and str(vr) in by_var:
        return by_var[str(vr)], "variant_ref"
    ref = listing["comp_reference"]
    if ref and ref in by_ref:
        return by_ref[ref], "reference"
    ean = listing["comp_ean13"]
    if ean and ean in by_ean:
        return by_ean[ean], "ean13"
    return None, None


# --- Update-Lauf ---------------------------------------------------------------

def _update_competitor(conn, comp, records: list[dict]) -> tuple[dict, list[tuple]]:
    by_var, by_ref, by_ean = _build_indices(records)
    cid = comp["competitor_id"]
    listings = get_confirmed_listings(conn, cid)

    stats = {"checked": 0, "changed": 0, "unchanged": 0,
             "no_match_skip": 0, "miss": 0}
    changes: list[tuple] = []  # (my_name, old, new, currency, url)

    for l in listings:
        if l["match_method"] == "no-match":
            stats["no_match_skip"] += 1
            continue
        rec, via = _find_match(l, by_var, by_ref, by_ean)
        if rec is None:
            stats["miss"] += 1
            continue
        price = rec.get("price")
        if price is None:
            stats["miss"] += 1
            continue
        currency = rec.get("currency") or comp["currency"]
        avail = rec.get("available")
        in_stock = 1 if avail is True else (0 if avail is False else None)

        # Alten Preis fuer das Log merken (vor dem Update aus dem Row holen).
        old_price = l["last_price"]
        changed = update_listing_price(conn, l["listing_id"],
                                       float(price), currency, in_stock)
        stats["checked"] += 1
        if changed:
            stats["changed"] += 1
            my_name = _get_variant_name(conn, l)
            changes.append((my_name, old_price, float(price), currency,
                            l["comp_url"]))
        else:
            stats["unchanged"] += 1

    conn.commit()
    return stats, changes


def _get_variant_name(conn, listing) -> str:
    r = conn.execute(
        "SELECT name FROM my_variant WHERE id_product=? AND id_product_attribute=?",
        (listing["id_product"], listing["id_product_attribute"]),
    ).fetchone()
    return (r["name"] if r else "?") or "?"


# --- CLI -----------------------------------------------------------------------

def _fmt_price(p, ccy) -> str:
    return f"{p:.2f} {ccy}" if p is not None else f"--- {ccy or ''}".strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Mitbewerber-Preise in confirmed listings aktualisieren.")
    ap.add_argument("name", nargs="?", help="competitor.name (Standard: alle aktiven)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad")
    args = ap.parse_args(argv[1:])

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

    exit_code = 0
    for c in comps:
        print(f"\n=== {c['name']} ({c['platform']}) {c['base_url']} ===")
        fetch = FETCHERS.get(c["platform"])
        if fetch is None:
            print(f"  Plattform '{c['platform']}' nicht unterstuetzt - skip", file=sys.stderr)
            exit_code = 1
            continue
        cfg_raw = c["fetcher_config"] if "fetcher_config" in c.keys() else None
        cfg = json.loads(cfg_raw) if cfg_raw else {}
        try:
            records = fetch(c["base_url"], **cfg)
        except Exception as e:
            print(f"  Fetch fehlgeschlagen: {e}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"  {len(records)} comp Records")
        st, changes = _update_competitor(conn, c, records)
        print(f"  geprueft: {st['checked']}  -> changed={st['changed']}, "
              f"unchanged={st['unchanged']}")
        print(f"  no-match-skip: {st['no_match_skip']}, ohne Treffer (Listing tot): {st['miss']}")
        if changes:
            print(f"  Preisaenderungen (max {MAX_CHANGES_PRINTED} gezeigt):")
            for my_name, old, new, ccy, url in changes[:MAX_CHANGES_PRINTED]:
                print(f"    {my_name[:48]:48}  {_fmt_price(old, ccy)} -> "
                      f"{_fmt_price(new, ccy)}")

    conn.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
