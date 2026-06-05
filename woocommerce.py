#!/usr/bin/env python3
"""
woocommerce.py - Fetcher fuer WooCommerce-Shops ueber die oeffentliche Store API.

Liefert den einheitlichen Fetcher-Rueckgabe-Vertrag (eine Liste von dicts):
    name, reference, ean13, upc, price, currency, url, variant_ref, available, variant_label

Store API (unauthentifiziert, JSON):  GET {base}/wp-json/wc/store/v1/products
- Preise kommen als String in Minor-Units -> price = int(prices['price']) / 10**minor_unit
- Varianten sind eigene Produkte (type='variation') und werden gebuendelt
  ueber ?type=variation geholt; das Varianten-Label (z.B. "Groesse: S")
  steckt im Eltern-Produkt unter variations[].attributes.
- Match-Schluessel ist in der Praxis die SKU (reference); das EAN-Feld
  (global_unique_id) ist bei WooCommerce oft leer.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlparse

import httpx

STORE_API = "/wp-json/wc/store/v1/products"
PER_PAGE = 100
SLEEP = 1.0          # hoeflich zwischen Seiten
TIMEOUT = 25.0
USER_AGENT = "rmypricesfair/1.0"


def _to_price(prices: dict) -> float | None:
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    minor = prices.get("currency_minor_unit", 2) or 0
    return int(raw) / (10 ** minor)


def _paginate(client: httpx.Client, url: str, extra_params: dict | None = None):
    """Iteriert ueber alle Seiten eines Store-API-Endpunkts (X-WP-TotalPages)."""
    page = 1
    while True:
        params = {"per_page": PER_PAGE, "page": page}
        if extra_params:
            params.update(extra_params)
        r = client.get(url, params=params)
        if r.status_code != 200:
            print(f"  ! {r.url} -> HTTP {r.status_code}", file=sys.stderr)
            break
        batch = r.json()
        if not batch:
            break
        for item in batch:
            yield item
        total_pages = int(r.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
        time.sleep(SLEEP)


def _record(obj: dict, name: str, variant_label: str | None) -> dict | None:
    price = _to_price(obj.get("prices") or {})
    if price is None:
        return None
    return {
        "name": name or obj.get("name", ""),
        "reference": (obj.get("sku") or "").strip() or None,
        "ean13": (obj.get("global_unique_id") or "").strip() or None,
        "upc": None,
        "price": price,
        "currency": (obj.get("prices") or {}).get("currency_code"),
        "url": obj.get("permalink"),
        "variant_ref": str(obj.get("id")),
        "available": bool(obj.get("is_in_stock")),
        "variant_label": variant_label,
    }


def fetch(base_url: str) -> list[dict]:
    """Holt alle Varianten eines WooCommerce-Shops als normalisierte Records."""
    url = base_url.rstrip("/") + STORE_API
    records: list[dict] = []
    # variation_id -> (eltern_name, label) aus den variablen Produkten
    variation_meta: dict[int, tuple[str, str | None]] = {}

    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=TIMEOUT, follow_redirects=True) as client:
        # 1) Nicht-Varianten-Produkte
        for p in _paginate(client, url):
            ptype = p.get("type")
            if ptype == "simple":
                rec = _record(p, p.get("name", ""), None)
                if rec:
                    records.append(rec)
            elif ptype == "variable":
                pname = p.get("name", "")
                for v in (p.get("variations") or []):
                    attrs = v.get("attributes") or []
                    label = ", ".join(
                        f"{a.get('name')}: {a.get('value')}" for a in attrs
                    ) or None
                    variation_meta[v["id"]] = (pname, label)
            # grouped / external etc. ignorieren

        # 2) Varianten gebuendelt
        for v in _paginate(client, url, {"type": "variation"}):
            pname, label = variation_meta.get(v.get("id"), (v.get("name", ""), None))
            rec = _record(v, pname, label)
            if rec:
                records.append(rec)

    return records


def fetch_one(url: str, **_unused) -> dict | None:
    """Holt eine einzelne WooCommerce-Produktseite ueber die Store API.
    Der Slug wird aus dem letzten Pfad-Segment der URL abgeleitet, dann
    /wp-json/wc/store/v1/products?slug=<slug> abgefragt.

    Variable Produkte: liefern den Eltern-Datensatz; 'price' aus prices.price
    ist dann der niedrigste sichtbare Preis und 'variant_ref' die parent-id.
    Fuer einen exakten Varianten-Treffer muesste die variation_id bekannt sein,
    die steht nicht im Permalink.

    Zusaetzliche kwargs werden ignoriert (Symmetrie zum schema_org.fetch_one).
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        print(f"  ! Ungueltige URL: {url}", file=sys.stderr)
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        print(f"  ! Konnte keinen Slug aus URL ableiten: {url}", file=sys.stderr)
        return None
    slug = segments[-1]
    api = f"{parsed.scheme}://{parsed.netloc}{STORE_API}"
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=TIMEOUT, follow_redirects=True) as client:
        r = client.get(api, params={"slug": slug})
        if r.status_code != 200:
            print(f"  ! {r.url} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        items = r.json() or []
        if not items:
            return None
        p = items[0]
        return _record(p, p.get("name", ""), None)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Aufruf: python woocommerce.py <base_url>")
        raise SystemExit(1)
    recs = fetch(sys.argv[1])
    print(f"\n{len(recs)} Records geholt.")
    simple = [r for r in recs if r["variant_label"] is None]
    variants = [r for r in recs if r["variant_label"] is not None]
    print(f"  davon ohne Variante: {len(simple)}, mit Variante: {len(variants)}")
    for r in (simple[:1] + variants[:2]):
        print(f"  - {r['name']!r} [{r['variant_label']}] "
              f"ref={r['reference']} ean={r['ean13']} "
              f"{r['price']} {r['currency']} stock={r['available']}")
