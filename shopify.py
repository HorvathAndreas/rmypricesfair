#!/usr/bin/env python3
"""
shopify.py - Fetcher fuer Shopify-Shops ueber die oeffentliche Products-API.

Liefert den einheitlichen Fetcher-Rueckgabe-Vertrag (gleich wie woocommerce.py):
    name, reference, ean13, upc, price, currency, url, variant_ref, available,
    variant_label

API (unauthentifiziert, JSON):  GET {base}/products.json?limit=N&page=P
- Pro Produkt sind die Varianten in 'variants[]' eingebettet.
- Pro Variante eine Zeile, wie bei woocommerce.py - das gibt dem Matcher
  feinkoernigere Treffer ueber comp_variant_ref (=Shopify variant.id).
- Variant-Label kommt aus variant.title; bei nur einer Variante ohne echte
  Optionen heisst die "Default Title" -> wird auf None gemappt.
- Brand-Praefix (vendor) wird dem Namen vorangestellt, falls die Marke nicht
  bereits Teil von product.title ist - hilft dem Fuzzy-Matcher mit Marken-
  Tokens, gleich wie bei schema_org.py.

Aufruf:
    python shopify.py https://example.myshopify.com
    python shopify.py https://example.myshopify.com --currency EUR --limit 5
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urljoin

import httpx

PRODUCTS_API = "/products.json"
PER_PAGE = 250          # Shopify max
SLEEP = 1.0             # hoeflich zwischen Seiten
TIMEOUT = 25.0
USER_AGENT = "rmypricesfair/1.0"

DEFAULT_CURRENCY = "CHF"  # Shopify-Products-API gibt keine Waehrung mit
DEFAULT_VARIANT_TITLE = "Default Title"  # Shopify-Sentinel fuer "keine Variante"


# --- HTTP / Pagination --------------------------------------------------------

def _paginate(client: httpx.Client, base_url: str):
    """Liefert Produkte Seite fuer Seite. Stoppt, wenn eine leere Seite kommt."""
    url = urljoin(base_url.rstrip("/") + "/", PRODUCTS_API.lstrip("/"))
    page = 1
    while True:
        try:
            r = client.get(url, params={"limit": PER_PAGE, "page": page})
        except httpx.HTTPError as e:
            print(f"  ! {url} page={page} -> {e}", file=sys.stderr)
            return
        if r.status_code != 200:
            print(f"  ! {r.url} -> HTTP {r.status_code}", file=sys.stderr)
            return
        batch = r.json().get("products", [])
        if not batch:
            return
        for p in batch:
            yield p
        if len(batch) < PER_PAGE:
            return
        page += 1
        time.sleep(SLEEP)


# --- Record-Bau ---------------------------------------------------------------

def _to_price(s) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _variant_label(v: dict) -> str | None:
    """Aus den Optionen einen Label-String bauen. 'Default Title' (Shopify-
    Sentinel) und leere Werte werden weggefiltert."""
    title = (v.get("title") or "").strip()
    if title and title != DEFAULT_VARIANT_TITLE:
        return title
    # Fallback: option1/2/3 zusammenbauen
    opts = [v.get(k) for k in ("option1", "option2", "option3")]
    opts = [o for o in opts if o and o != DEFAULT_VARIANT_TITLE]
    return ", ".join(opts) if opts else None


def _product_url(base_url: str, handle: str, variant_id: int | None) -> str:
    url = urljoin(base_url.rstrip("/") + "/", f"products/{handle}")
    return f"{url}?variant={variant_id}" if variant_id else url


def _full_name(product: dict) -> str:
    """Brand voranstellen, falls sie nicht bereits im Titel steckt -
    der Fuzzy-Matcher tokenisiert nur den Namen."""
    title = (product.get("title") or "").strip()
    vendor = (product.get("vendor") or "").strip()
    if vendor and vendor.lower() not in title.lower():
        return f"{vendor} {title}".strip()
    return title or vendor or "?"


def _record(product: dict, variant: dict, base_url: str,
            currency: str) -> dict | None:
    price = _to_price(variant.get("price"))
    if price is None:
        return None
    sku = (variant.get("sku") or "").strip() or None
    ean = (variant.get("barcode") or "").strip() or None
    return {
        "name": _full_name(product),
        "reference": sku,
        "ean13": ean,
        "upc": None,
        "price": price,
        "currency": currency,
        "url": _product_url(base_url, product.get("handle", ""), variant.get("id")),
        "variant_ref": str(variant["id"]) if variant.get("id") is not None else None,
        "available": bool(variant.get("available")),
        "variant_label": _variant_label(variant),
    }


# --- Public API ---------------------------------------------------------------

def fetch(base_url: str, *, currency: str = DEFAULT_CURRENCY,
          limit: int | None = None, **_unused) -> list[dict]:
    """Holt alle Produkte (eine Zeile pro Variante). 'currency' kommt vom
    Aufrufer (Shopify-Products-API liefert keine Waehrung); 'limit' begrenzt
    die Anzahl Produkte fuer Testlaufe."""
    records: list[dict] = []
    n_products = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=TIMEOUT, follow_redirects=True) as client:
        for p in _paginate(client, base_url):
            n_products += 1
            for v in (p.get("variants") or []):
                rec = _record(p, v, base_url, currency)
                if rec:
                    records.append(rec)
            if limit is not None and n_products >= limit:
                break
    return records


def fetch_one(url: str, *, currency: str = DEFAULT_CURRENCY,
              **_unused) -> dict | None:
    """Parst eine einzelne Produkt-URL. Erwartet das uebliche Shopify-Schema
    {base}/products/{handle}[?variant=<id>] und holt {base}/products/{handle}.json.
    Wird vom coverage.py-Dialog fuer manuelle URL-Hinterlegung benutzt.

    Bei Mehrvariantenprodukten wird - falls in der URL ?variant=<id> steht -
    genau diese Variante zurueckgegeben; sonst die erste verfuegbare.
    """
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if "/products/" not in path:
        print(f"  ! URL passt nicht zum Shopify-Schema (kein /products/): {url}",
              file=sys.stderr)
        return None
    handle = path.rsplit("/products/", 1)[1]
    if not handle:
        return None
    qs = parse_qs(parsed.query)
    pref_variant = None
    if "variant" in qs:
        try:
            pref_variant = int(qs["variant"][0])
        except (TypeError, ValueError):
            pref_variant = None

    json_url = f"{base}/products/{handle}.json"
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            r = client.get(json_url)
        except httpx.HTTPError as e:
            print(f"  ! {json_url} -> {e}", file=sys.stderr)
            return None
    if r.status_code != 200:
        print(f"  ! {json_url} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    product = (r.json() or {}).get("product")
    if not product:
        return None

    variants = product.get("variants") or []
    chosen = None
    if pref_variant is not None:
        for v in variants:
            if v.get("id") == pref_variant:
                chosen = v
                break
    if chosen is None:
        # erste verfuegbare, sonst erste ueberhaupt
        for v in variants:
            if v.get("available"):
                chosen = v
                break
        if chosen is None and variants:
            chosen = variants[0]
    if chosen is None:
        return None
    return _record(product, chosen, base, currency)


# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Shopify Products-API Crawler.")
    ap.add_argument("base_url", help="Shop-Basis-URL, z.B. https://shop.example")
    ap.add_argument("--currency", default=DEFAULT_CURRENCY,
                    help=f"Waehrung des Shops (Default {DEFAULT_CURRENCY}).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Nur die ersten N Produkte verarbeiten (Testlauf).")
    args = ap.parse_args()
    recs = fetch(args.base_url, currency=args.currency, limit=args.limit)
    print(f"\n{len(recs)} Records (Varianten) geholt.")
    for r in recs[:5]:
        lbl = f" [{r['variant_label']}]" if r["variant_label"] else ""
        print(f"  - {r['name']!r}{lbl}  {r['price']} {r['currency']}  "
              f"stock={r['available']}  id={r['variant_ref']} sku={r['reference']!r}")
        print(f"    {r['url']}")
