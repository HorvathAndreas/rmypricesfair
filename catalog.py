#!/usr/bin/env python3
"""
catalog.py - eigene PrestaShop-Kataloge in my_variant einsynchronisieren.

Quelle: PrestaShop-Webservice via /webservice/dispatcher.php?url=...
Auth: Basic, Benutzer = API-Key, Passwort = leer.
Konfiguration: config.yaml (gitignored, siehe config.yaml.example).

Preisbasis: brutto inkl. Landes-MwSt (Empfehlung aus CLAUDE.md). Konstruktion:
  brutto = (product.price + combination.price) * (1 + tax_rate/100)
'product.price' und 'combination.price' kommen netto vom Webservice;
'combination.price' ist der *Impact* auf den Grundpreis (kann negativ sein).

Varianten-Behandlung: haben alle Combinations eines Produkts denselben
brutto-Preis (Farbe, Groesse o.ae. ohne Preisrelevanz), wird das Produkt
als *eine* Zeile mit id_product_attribute=0 geschrieben. Nur wenn echte
Preisunterschiede bestehen, kommen die Combinations einzeln in my_variant.

Skip-Filter: Combinations mit bestimmten product_option_values werden
ignoriert (Standard: 'gebraucht', 'used'). Per Shop ueberschreibbar via
'skip_variant_attributes' in config.yaml.

Stale-Cleanup: Zeilen, die in diesem Lauf nicht angefasst wurden (z.B. alte
per-Variant-Eintraege fuer ein nun zusammengefaltetes Produkt), werden auf
active=0 gesetzt - bestehende listings bleiben referenz-stabil.

Es werden nur Shops mit is_master=true in my_variant geschrieben. Nicht-Master
werden gezaehlt und mit Sample-Zeilen ausgegeben (Verbindungstest).

Aufrufe:
    python catalog.py                # alle Shops aus config.yaml
    python catalog.py CH             # nur Shop mit Schluessel 'CH'
    python catalog.py --db pfad.db   # alternativer DB-Pfad
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import yaml

from db import (DEFAULT_DB, clear_my_variant_prices, get_connection, init_db,
                upsert_my_variant_price, upsert_variant)

# Politeness: 1 Request/s, siehe CLAUDE.md.
POLITENESS_SEC = 1.0
TIMEOUT = 30.0
CONFIG_FILE = Path(__file__).parent / "config.yaml"

# Default: alles was sich auf gebrauchte Ware bezieht, wird nicht in den
# Vergleich aufgenommen. Pro Shop in config.yaml ueberschreibbar.
DEFAULT_SKIP_ATTRS = ["gebraucht", "used"]


# --- HTTP / API ----------------------------------------------------------------

def _ws_get(client: httpx.Client, base_url: str, resource: str, **params) -> dict:
    """GET auf dispatcher.php als JSON. Schlaeft danach POLITENESS_SEC."""
    url = f"{base_url.rstrip('/')}/webservice/dispatcher.php"
    qp = {"url": resource, "output_format": "JSON", **params}
    r = client.get(url, params=qp)
    r.raise_for_status()
    time.sleep(POLITENESS_SEC)
    return r.json()


def _resolve_country_id(client: httpx.Client, base_url: str, iso: str) -> int:
    data = _ws_get(client, base_url, "countries", display="full",
                   **{"filter[iso_code]": iso})
    cs = data.get("countries", [])
    if not cs:
        raise RuntimeError(f"Land mit ISO {iso} im Shop {base_url} nicht gefunden.")
    return int(cs[0]["id"])


def _build_tax_lookup(client: httpx.Client, base_url: str, country_id: int) -> dict[int, float]:
    """Liefert {id_tax_rules_group: rate_pct} fuer das angegebene Land."""
    taxes = _ws_get(client, base_url, "taxes", display="full")["taxes"]
    rate_by_id = {int(t["id"]): float(t["rate"]) for t in taxes}
    rules = _ws_get(client, base_url, "tax_rules", display="full",
                    **{"filter[id_country]": str(country_id)})["tax_rules"]
    lookup: dict[int, float] = {}
    for r in rules:
        gid = int(r["id_tax_rules_group"])
        rate = rate_by_id.get(int(r["id_tax"]))
        if rate is None:
            continue
        # In der Praxis genau eine Rule pro (Gruppe, Land); ggf. ersten Treffer.
        lookup.setdefault(gid, rate)
    return lookup


def _fetch_active_products(client: httpx.Client, base_url: str) -> list[dict]:
    return _ws_get(client, base_url, "products", display="full",
                   **{"filter[active]": "1"})["products"]


def _fetch_all_combinations(client: httpx.Client, base_url: str) -> list[dict]:
    return _ws_get(client, base_url, "combinations", display="full")["combinations"]


def _fetch_option_values(client: httpx.Client, base_url: str) -> dict[int, str]:
    """Liefert {id: name (erste Sprache)} fuer alle product_option_values."""
    data = _ws_get(client, base_url, "product_option_values", display="full")
    out: dict[int, str] = {}
    for v in data.get("product_option_values", []):
        out[int(v["id"])] = _first_lang_value(v.get("name")) or ""
    return out


def _combo_option_ids(c: dict) -> set[int]:
    """Liefert die product_option_value-IDs einer Combination als int-Set."""
    pvs = (c.get("associations") or {}).get("product_option_values") or []
    out: set[int] = set()
    for pv in pvs:
        try:
            out.add(int(pv.get("id")))
        except (TypeError, ValueError):
            continue
    return out


# --- Hilfsfunktionen -----------------------------------------------------------

def _first_lang_value(field) -> str | None:
    """PrestaShop liefert mehrsprachige Felder als Liste [{id, value}, ...]."""
    if isinstance(field, list) and field:
        return field[0].get("value") or None
    return field or None


def _clean(s) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    return s or None


def _brutto(netto: float, rate_pct: float) -> float:
    return round(netto * (1 + rate_pct / 100.0), 2)


# --- Sync ----------------------------------------------------------------------

def _sync_master(client: httpx.Client, conn, base_url: str, currency: str,
                 country_iso: str,
                 tax_lookup: dict[int, float],
                 skip_patterns: list[str]) -> tuple[int, int, int, int, int,
                                                     int, int, int]:
    """Schreibt Varianten des Master-Shops nach my_variant UND parallel
    den eigenen Brutto-Preis nach my_variant_price[country_iso].
    Liefert (n_simple, n_collapsed, n_var, n_skipped, n_stale,
             n_migrated, n_deduped, n_orphan)."""
    # Stale Land-Preise des Masters loeschen - frisch befuellen.
    clear_my_variant_prices(conn, country_iso)
    products = _fetch_active_products(client, base_url)
    combs = _fetch_all_combinations(client, base_url)
    option_values = _fetch_option_values(client, base_url)

    # Skip-IDs aus den Pattern ableiten (case-insensitive Substring-Match).
    skip_ids: set[int] = {
        vid for vid, name in option_values.items()
        if any(p.lower() in name.lower() for p in skip_patterns)
    }

    combs_by_product: dict[int, list[dict]] = {}
    for c in combs:
        combs_by_product.setdefault(int(c["id_product"]), []).append(c)

    n_simple = n_collapsed = n_var = n_skipped = 0
    touched: set[tuple[int, int]] = set()

    for p in products:
        pid = int(p["id"])
        base_netto = float(p.get("price") or 0)
        gid_raw = p.get("id_tax_rules_group")
        gid = int(gid_raw) if gid_raw not in (None, "", "0", 0) else None
        rate = tax_lookup.get(gid, 0.0) if gid else 0.0
        name = _first_lang_value(p.get("name")) or f"Produkt {pid}"
        ref_p = _clean(p.get("reference"))
        ean_p = _clean(p.get("ean13"))
        upc_p = _clean(p.get("upc"))

        prod_combs = combs_by_product.get(pid, [])
        if not prod_combs:
            # Echtes Single-Produkt
            brutto = _brutto(base_netto, rate)
            upsert_variant(conn, {
                "id_product": pid, "id_product_attribute": 0,
                "reference": ref_p, "ean13": ean_p, "upc": upc_p,
                "name": name, "variant_label": None,
                "price": brutto, "currency": currency,
                "active": 1,
            })
            upsert_my_variant_price(conn, pid, 0, country_iso,
                                    brutto, currency)
            touched.add((pid, 0))
            n_simple += 1
            continue

        # Skip-Filter: Combinations mit "gebraucht"/"used" o.ae. raus.
        kept: list[dict] = []
        for c in prod_combs:
            if _combo_option_ids(c) & skip_ids:
                n_skipped += 1
                continue
            kept.append(c)
        if not kept:
            # Alle Combinations rausgefiltert -> Produkt insgesamt skippen.
            continue

        # Per-Variant brutto vorberechnen + variant_label aus option_values.
        variant_pricing: list[tuple[dict, float, str | None]] = []
        for c in kept:
            impact = float(c.get("price") or 0)
            br = _brutto(base_netto + impact, rate)
            label_names = [option_values.get(i, "") for i in _combo_option_ids(c)]
            label_names = [n for n in label_names if n]
            label = ", ".join(sorted(label_names)) or None
            variant_pricing.append((c, br, label))
        unique_prices = {price for _, price, _ in variant_pricing}

        if len(unique_prices) == 1:
            # Alle Combinations gleichteuer -> ein Produkt-Repraesentant.
            single_price = next(iter(unique_prices))
            upsert_variant(conn, {
                "id_product": pid, "id_product_attribute": 0,
                "reference": ref_p, "ean13": ean_p, "upc": upc_p,
                "name": name, "variant_label": None,
                "price": single_price, "currency": currency, "active": 1,
            })
            upsert_my_variant_price(conn, pid, 0, country_iso,
                                    single_price, currency)
            touched.add((pid, 0))
            n_collapsed += 1
        else:
            # Echte Preisvarianten -> jede Combination einzeln, mit Label.
            for c, price, label in variant_pricing:
                ipa = int(c["id"])
                upsert_variant(conn, {
                    "id_product": pid, "id_product_attribute": ipa,
                    "reference": _clean(c.get("reference")) or ref_p,
                    "ean13":     _clean(c.get("ean13"))     or ean_p,
                    "upc":       _clean(c.get("upc"))       or upc_p,
                    "name": name, "variant_label": label,
                    "price": price, "currency": currency, "active": 1,
                })
                upsert_my_variant_price(conn, pid, ipa, country_iso,
                                        price, currency)
                touched.add((pid, ipa))
                n_var += 1

    # Stale-Cleanup: alle nicht angefassten Zeilen deaktivieren.
    # Bestehende listings bleiben referenz-stabil, werden aber durch v.active=0
    # aus Matcher / Reporter ausgeblendet.
    all_existing = {(r[0], r[1]) for r in
                    conn.execute("SELECT id_product, id_product_attribute FROM my_variant")}
    stale = all_existing - touched
    for pid, ipa in stale:
        conn.execute(
            "UPDATE my_variant SET active=0 WHERE id_product=? AND id_product_attribute=?",
            (pid, ipa),
        )

    # Listing-Migration: bestaetigte/lebende listings, deren my_variant inaktiv
    # geworden ist (weil das Produkt collapsed wurde), auf den Repraesentanten
    # ipa=0 umparken. Konfliktet ein Listing fuer (pid, 0, comp) bereits, wird
    # die verwaiste Zeile entfernt (Dedup nach competitor_id).
    n_migrated = n_deduped = n_orphan = 0
    orphans = conn.execute(
        """
        SELECT l.listing_id, l.id_product, l.competitor_id
        FROM listing l
        JOIN my_variant v
            ON v.id_product=l.id_product
           AND v.id_product_attribute=l.id_product_attribute
        WHERE v.active = 0 AND l.id_product_attribute != 0
        """
    ).fetchall()
    for l in orphans:
        pid, lid, cid = l["id_product"], l["listing_id"], l["competitor_id"]
        rep = conn.execute(
            "SELECT 1 FROM my_variant WHERE id_product=? AND id_product_attribute=0 AND active=1",
            (pid,),
        ).fetchone()
        if rep is None:
            # Produkt komplett raus - listing stehenlassen (bleibt verwaist).
            n_orphan += 1
            continue
        exists = conn.execute(
            "SELECT 1 FROM listing WHERE id_product=? AND id_product_attribute=0 AND competitor_id=?",
            (pid, cid),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM listing WHERE listing_id=?", (lid,))
            n_deduped += 1
        else:
            conn.execute("UPDATE listing SET id_product_attribute=0 WHERE listing_id=?", (lid,))
            n_migrated += 1

    conn.commit()
    return (n_simple, n_collapsed, n_var, n_skipped, len(stale),
            n_migrated, n_deduped, n_orphan)


def _summary_only(client: httpx.Client, base_url: str) -> tuple[int, int, list[dict]]:
    """Nur zaehlen + 3 Sample-Produkte zurueckgeben."""
    products = _fetch_active_products(client, base_url)
    combs = _fetch_all_combinations(client, base_url)
    return len(products), len(combs), products[:3]


def _norm_name(s: str | None) -> str | None:
    """Normalisierter Produkt-Name fuer exakten Vergleich: lowercase,
    Whitespace zusammengezogen. Leere Strings -> None."""
    if not s:
        return None
    out = " ".join(s.lower().split())
    return out or None


def _build_secondary_price_index(client: httpx.Client, base_url: str,
                                 tax_lookup: dict[int, float],
                                 skip_patterns: list[str]
                                 ) -> tuple[dict[str, float],
                                            dict[str, float],
                                            dict[str, float]]:
    """Liest einen Nicht-Master-Shop und liefert drei Lookups:
    {reference -> brutto}, {ean13 -> brutto}, {normalized_name -> brutto},
    jeweils in der Shop-Waehrung. Erstauftritt gewinnt.

    Wichtig: pro Combination wird sowohl die Combination-eigene reference/ean
    ALS AUCH die Product-Level-reference/ean indiziert. So funktioniert der
    Lookup auch, wenn der Master collapsed gespeichert hat (eine Zeile pro
    Produkt) und der Sekundaer-Shop pro Combination eigene SKUs fuehrt -
    oder umgekehrt."""
    products = _fetch_active_products(client, base_url)
    combs = _fetch_all_combinations(client, base_url)
    option_values = _fetch_option_values(client, base_url)

    skip_ids = {vid for vid, name in option_values.items()
                if any(p.lower() in name.lower() for p in skip_patterns)}

    combs_by_product: dict[int, list[dict]] = {}
    for c in combs:
        combs_by_product.setdefault(int(c["id_product"]), []).append(c)

    by_ref: dict[str, float] = {}
    by_ean: dict[str, float] = {}
    by_name: dict[str, float] = {}
    for p in products:
        pid = int(p["id"])
        base_netto = float(p.get("price") or 0)
        gid_raw = p.get("id_tax_rules_group")
        gid = int(gid_raw) if gid_raw not in (None, "", "0", 0) else None
        rate = tax_lookup.get(gid, 0.0) if gid else 0.0

        ref_p = _clean(p.get("reference"))
        ean_p = _clean(p.get("ean13"))
        name_p = _norm_name(_first_lang_value(p.get("name")))

        prod_combs = combs_by_product.get(pid, [])
        if not prod_combs:
            brutto = _brutto(base_netto, rate)
            if ref_p: by_ref.setdefault(ref_p, brutto)
            if ean_p: by_ean.setdefault(ean_p, brutto)
            if name_p: by_name.setdefault(name_p, brutto)
            continue

        for c in prod_combs:
            if _combo_option_ids(c) & skip_ids:
                continue
            impact = float(c.get("price") or 0)
            brutto = _brutto(base_netto + impact, rate)
            # Beide Ebenen erfassen (combination + product), damit ein
            # collapsed Master ueber ref_p findet und ein expanded Master
            # ueber die combination-eigene ref.
            for r in (_clean(c.get("reference")), ref_p):
                if r: by_ref.setdefault(r, brutto)
            for e in (_clean(c.get("ean13")), ean_p):
                if e: by_ean.setdefault(e, brutto)
            if name_p: by_name.setdefault(name_p, brutto)
    return by_ref, by_ean, by_name


def _sync_secondary(client: httpx.Client, conn, base_url: str,
                    country_iso: str, currency: str,
                    tax_lookup: dict[int, float],
                    skip_patterns: list[str]) -> tuple[int, int, int, int]:
    """Synchronisiert einen Nicht-Master-Shop in my_variant_price[country_iso].
    Zuordnung Master <-> Sekundaer in dieser Reihenfolge: reference, ean13,
    normalisierter Name (lowercase + Whitespace zusammengezogen).
    Liefert (n_ref, n_ean, n_name, n_unmatched)."""
    clear_my_variant_prices(conn, country_iso)
    by_ref, by_ean, by_name = _build_secondary_price_index(
        client, base_url, tax_lookup, skip_patterns)

    n_ref = n_ean = n_name = n_unmatched = 0
    for v in conn.execute(
        "SELECT id_product, id_product_attribute, reference, ean13, name "
        "FROM my_variant WHERE active = 1").fetchall():
        ref = (v["reference"] or "").strip()
        ean = (v["ean13"] or "").strip()
        name = _norm_name(v["name"])
        price = None
        if ref and ref in by_ref:
            price = by_ref[ref]
            n_ref += 1
        elif ean and ean in by_ean:
            price = by_ean[ean]
            n_ean += 1
        elif name and name in by_name:
            price = by_name[name]
            n_name += 1
        if price is None:
            n_unmatched += 1
            continue
        upsert_my_variant_price(conn, v["id_product"], v["id_product_attribute"],
                                country_iso, price, currency)
    conn.commit()
    return n_ref, n_ean, n_name, n_unmatched


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Eigene PrestaShop-Kataloge synchronisieren.")
    p.add_argument("shop", nargs="?", help="Shop-Schluessel aus config.yaml (Standard: alle)")
    p.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad")
    p.add_argument("--config", default=str(CONFIG_FILE), help="Konfigurationsdatei")
    args = p.parse_args(argv[1:])

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Konfiguration fehlt: {cfg_path}\n"
              f"Vorlage kopieren: cp config.yaml.example config.yaml", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(cfg_path.read_text())
    shops = (cfg or {}).get("shops", {})
    if not shops:
        print(f"Keine Shops in {cfg_path} definiert.", file=sys.stderr)
        return 2
    if args.shop:
        if args.shop not in shops:
            print(f"Shop '{args.shop}' nicht in {cfg_path}.", file=sys.stderr)
            return 2
        shops = {args.shop: shops[args.shop]}

    conn = get_connection(args.db)
    init_db(conn)

    for key, s in shops.items():
        print(f"\n=== {key}: {s['base_url']} ===")
        with httpx.Client(auth=(s["api_key"], ""), timeout=TIMEOUT,
                          headers={"Accept": "application/json"}) as client:
            try:
                if s.get("is_master"):
                    cid = _resolve_country_id(client, s["base_url"], s["country_iso"])
                    tlk = _build_tax_lookup(client, s["base_url"], cid)
                    print(f"  Land {s['country_iso']} id={cid}, Steuergruppen geladen: {len(tlk)}")
                    skip_pats = s.get("skip_variant_attributes") or DEFAULT_SKIP_ATTRS
                    print(f"  Skip-Patterns: {skip_pats}")
                    ns, nc, nv, nsk, nd, nm, ndp, nor = _sync_master(
                        client, conn, s["base_url"], s["currency"],
                        s["country_iso"], tlk, skip_pats)
                    print(f"  geschrieben in my_variant: simple={ns}, collapsed={nc}, "
                          f"echte-varianten={nv}, gesamt={ns + nc + nv} "
                          f"({s['currency']} brutto)")
                    print(f"  my_variant_price[{s['country_iso']}]: "
                          f"{ns + nc + nv} Eintraege geschrieben")
                    print(f"  skip (Filter): {nsk} Combinations,  "
                          f"deaktiviert (stale): {nd}")
                    print(f"  listing-Migration: {nm} migriert auf ipa=0, "
                          f"{ndp} dedupliziert, {nor} verwaist (Produkt weg)")
                else:
                    # Sekundaer-Shop: Preise in my_variant_price[country_iso]
                    # schreiben, Zuordnung ueber reference/ean13.
                    cid = _resolve_country_id(client, s["base_url"], s["country_iso"])
                    tlk = _build_tax_lookup(client, s["base_url"], cid)
                    print(f"  Land {s['country_iso']} id={cid}, "
                          f"Steuergruppen geladen: {len(tlk)}")
                    skip_pats = s.get("skip_variant_attributes") or DEFAULT_SKIP_ATTRS
                    n_ref, n_ean, n_name, n_un = _sync_secondary(
                        client, conn, s["base_url"], s["country_iso"],
                        s["currency"], tlk, skip_pats)
                    print(f"  my_variant_price[{s['country_iso']}]: "
                          f"per reference={n_ref}, per ean13={n_ean}, "
                          f"per name={n_name}, keine Zuordnung={n_un}")
            except httpx.HTTPError as e:
                print(f"  HTTP-Fehler: {e}", file=sys.stderr)
                return 1

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
