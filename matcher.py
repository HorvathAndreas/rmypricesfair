#!/usr/bin/env python3
"""
matcher.py - Auto-Match: eigene Varianten <-> Mitbewerber-Produkte.

Holt fuer jeden aktiven Mitbewerber den Katalog ueber den passenden
Plattform-Fetcher und legt Match-Vorschlaege an:

  1. Harte Treffer (ean13 / reference, beidseitig eindeutig)
        -> Direkt in 'listing' mit confirmed=0.
  2. Weiche Treffer (Token-Jaccard auf normalisierten Namen)
        -> Top-N Kandidaten in 'match_candidate' (Score, method='name').

Hard und Fuzzy schliessen sich aus pro Variante: gibt es einen harten Treffer,
werden keine Kandidaten erzeugt.

Vor jedem Lauf werden die alten Kandidaten des Mitbewerbers geloescht.
Bestaetigte 'listing'-Zeilen (confirmed=1) bleiben unangetastet.

Aufrufe:
    python matcher.py                 # alle aktiven Mitbewerber
    python matcher.py NAME            # nur einen Mitbewerber
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import defaultdict

from db import (DEFAULT_DB, clear_candidates, get_active_competitors,
                get_connection, get_unmatched_variants, init_db,
                insert_candidate, upsert_listing)

import woocommerce

# Plattform -> Fetcher-Funktion. Vertrag: fetch(base_url) -> list[dict].
FETCHERS = {
    "woocommerce": woocommerce.fetch,
}

# Fuzzy-Parameter
FUZZY_THRESHOLD = 0.40  # Mindest-Jaccard fuer einen Kandidaten
TOP_N = 5               # max Kandidaten pro Variante
MIN_TOKEN_LEN = 2

# Allgemeine Fuellwoerter, die wenig diskriminieren (raus damit).
STOP_TOKENS = {
    "de", "und", "mit", "im", "in", "fur", "fuer", "von", "zu", "the",
    "kit", "set", "neu", "new",
}


# --- Normalisierung + Jaccard --------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9äöüß]+")


def _tokens(s: str | None) -> set[str]:
    """lowercase, HTML-Entities aufloesen, Sonderzeichen weg, kurze Tokens raus."""
    if not s:
        return set()
    s = html.unescape(s).lower()
    s = _NON_ALNUM.sub(" ", s)
    return {t for t in s.split()
            if len(t) >= MIN_TOKEN_LEN and t not in STOP_TOKENS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _dedupe_candidates(scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """Faltet comp-Varianten desselben Eltern-Produkts zusammen.
    Gruppierungs-Schluessel: reference (SKU); leerer SKU -> normalisierter Name.
    Pro Schluessel gewinnt der erste Treffer in der bereits sortierten Liste,
    also der mit dem hoechsten Score. Reihenfolge bleibt sonst erhalten."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[float, dict]] = []
    for sc, rec in scored:
        ref = (rec.get("reference") or "").strip()
        if ref:
            key = ("ref", ref)
        else:
            key = ("name", (rec.get("name") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((sc, rec))
    return out


# --- Indizes -------------------------------------------------------------------

def _build_indices(records: list[dict]) -> tuple[dict, dict, dict]:
    """Indiziert comp-Records nach ean13 und reference.
    Liefert (by_ean, by_ref_unique, by_ref_all)."""
    by_ean: dict[str, dict] = {}
    by_ref_all: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        ean = (r.get("ean13") or "").strip()
        if ean:
            by_ean.setdefault(ean, r)  # ersten Treffer behalten
        ref = (r.get("reference") or "").strip()
        if ref:
            by_ref_all[ref].append(r)
    by_ref_unique = {k: v[0] for k, v in by_ref_all.items() if len(v) == 1}
    return by_ean, by_ref_unique, by_ref_all


# --- Match-Lauf ----------------------------------------------------------------

def _candidate_dict(v_row, cid, rec, score) -> dict:
    return {
        "id_product": v_row["id_product"],
        "id_product_attribute": v_row["id_product_attribute"],
        "competitor_id": cid,
        "comp_name": rec.get("name"),
        "comp_reference": rec.get("reference"),
        "comp_ean13": rec.get("ean13"),
        "comp_upc": rec.get("upc"),
        "comp_url": rec.get("url"),
        "comp_variant_ref": rec.get("variant_ref"),
        "method": "name",
        "score": round(float(score), 4),
    }


def _listing_dict(v_row, cid, rec, method) -> dict:
    return {
        "id_product": v_row["id_product"],
        "id_product_attribute": v_row["id_product_attribute"],
        "competitor_id": cid,
        "comp_name": rec.get("name"),
        "comp_reference": rec.get("reference"),
        "comp_ean13": rec.get("ean13"),
        "comp_upc": rec.get("upc"),
        "comp_url": rec.get("url"),
        "comp_variant_ref": rec.get("variant_ref"),
        "match_method": method,
        "confirmed": 0,
    }


def _match_competitor(conn, comp_row, records: list[dict]) -> dict:
    cid = comp_row["competitor_id"]
    by_ean, by_ref_unique, by_ref_all = _build_indices(records)

    # Alte Kandidaten dieses Mitbewerbers weg - frischer Stand.
    n_cleared = clear_candidates(conn, cid)

    # Comp-Tokens einmal vorberechnen.
    comp_tokens = [(rec, _tokens(rec.get("name"))) for rec in records]

    stats = {"ean": 0, "ref": 0, "ref_ambig": 0,
             "fuzzy_variants": 0, "fuzzy_cands": 0,
             "no_match": 0, "cleared": n_cleared}

    for v in get_unmatched_variants(conn, cid):
        ean = v["ean13"]
        ref = v["reference"]
        rec, method = None, None

        # 1) ean13
        if ean and ean in by_ean:
            rec, method = by_ean[ean], "ean13"
        # 2) reference (nur wenn comp-Seite eindeutig)
        elif ref:
            if ref in by_ref_unique:
                rec, method = by_ref_unique[ref], "reference"
            elif ref in by_ref_all:
                stats["ref_ambig"] += 1
                # weiter als Fuzzy-Kandidat behandeln

        if rec is not None:
            upsert_listing(conn, _listing_dict(v, cid, rec, method))
            stats["ean" if method == "ean13" else "ref"] += 1
            continue

        # 3) Fuzzy: Token-Jaccard auf normalisierten Namen
        my_t = _tokens(v["name"])
        if not my_t:
            stats["no_match"] += 1
            continue
        scored = []
        for crec, ct in comp_tokens:
            sc = _jaccard(my_t, ct)
            if sc >= FUZZY_THRESHOLD:
                scored.append((sc, crec))
        if not scored:
            stats["no_match"] += 1
            continue
        scored.sort(key=lambda x: -x[0])
        scored = _dedupe_candidates(scored)
        top = scored[:TOP_N]
        for sc, crec in top:
            insert_candidate(conn, _candidate_dict(v, cid, crec, sc))
        stats["fuzzy_variants"] += 1
        stats["fuzzy_cands"] += len(top)

    conn.commit()
    return stats


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Auto-Match eigene Varianten <-> Mitbewerber.")
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
        print("Keine aktiven Mitbewerber in der DB.", file=sys.stderr)
        return 1

    exit_code = 0
    for c in comps:
        print(f"\n=== {c['name']} ({c['platform']}) {c['base_url']} ===")
        fetch = FETCHERS.get(c["platform"])
        if fetch is None:
            print(f"  Plattform '{c['platform']}' nicht unterstuetzt - skip", file=sys.stderr)
            exit_code = 1
            continue
        try:
            records = fetch(c["base_url"])
        except Exception as e:
            print(f"  Fetch fehlgeschlagen: {e}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"  {len(records)} comp Records, alte Kandidaten geloescht")
        st = _match_competitor(conn, c, records)
        print(f"  hart angelegt (listing, confirmed=0):  ean={st['ean']}, ref={st['ref']}")
        print(f"  fuzzy (match_candidate, method=name): "
              f"{st['fuzzy_variants']} Varianten / {st['fuzzy_cands']} Kandidaten")
        print(f"  ref-mehrdeutig (Fallback Fuzzy): {st['ref_ambig']}   ohne Treffer: {st['no_match']}")

    conn.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
