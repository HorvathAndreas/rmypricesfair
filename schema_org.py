#!/usr/bin/env python3
"""
schema_org.py - generischer Fetcher fuer Shops mit XML-Sitemap und
                schema.org-Microdata auf den Produktseiten.

Erwartet zwei Eigenschaften der Zielseite:
  1) Eine XML-Sitemap unter /sitemap.xml. Der Sitemap-Index wird nach
     Sub-Sitemaps gefiltert, deren <loc> einen vorgegebenen Substring
     enthaelt (sitemap_filter, z.B. "sitemap=shop"). In diesen Sub-
     Sitemaps liegen die Produkt-URLs.
  2) Die Produktseiten exponieren itemprop="brand|name|price|priceCurrency|
     availability" als HTML-Microdata (Vocabulary: schema.org).

Welche URLs Produkte sind und woher die variant_ref kommt, entscheidet ein
vom Aufrufer mitgegebener product_url_regex - die erste Capture-Gruppe muss
die plattformseitige Produkt-ID liefern.

Vertrag (gleich wie woocommerce.py):
    name, reference, ean13, upc, price, currency, url, variant_ref,
    available, variant_label

Nicht aus schema.org ableitbare Felder (SKU, EAN, UPC, Variant-Label)
bleiben None. Matching laeuft dann ausschliesslich fuzzy ueber den Namen.

Konfiguration (typischerweise im fetcher_config-Blob der competitor-Zeile):
    sitemap_filter (str)    Substring zur Auswahl der Shop-Sub-Sitemaps,
                            z.B. "sitemap=shop".
    product_url_regex (str) Regex, der genau Produkt-URLs trifft und in
                            Gruppe 1 die variant_ref liefert.

Aufruf der CLI:
    python schema_org.py <base_url> \\
        --sitemap-filter 'sitemap=shop' \\
        --product-url-regex '.+-(\\d+)/?$' \\
        [--limit N]
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import time
from urllib.parse import urljoin

import httpx

SITEMAP_INDEX_PATH = "/sitemap.xml"
PER_REQUEST_SLEEP = 1.0   # Politeness, siehe CLAUDE.md.
TIMEOUT = 30.0
USER_AGENT = "rmypricesfair/1.0"

# Schema.org-Werte fuer 'lieferbar' / 'nicht lieferbar'. http und https beide,
# weil manche Implementierungen die Variante mit/ohne 's' schreiben.
_AVAIL_IN = {
    "https://schema.org/InStock", "http://schema.org/InStock",
    "https://schema.org/LimitedAvailability", "http://schema.org/LimitedAvailability",
    "https://schema.org/PreOrder", "http://schema.org/PreOrder",
}
_AVAIL_OUT = {
    "https://schema.org/OutOfStock", "http://schema.org/OutOfStock",
    "https://schema.org/SoldOut", "http://schema.org/SoldOut",
    "https://schema.org/Discontinued", "http://schema.org/Discontinued",
}


# --- Sitemap-Discovery --------------------------------------------------------

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.I)


def _get(client: httpx.Client, url: str) -> str | None:
    """GET mit Politeness-Sleep. Liefert den Body oder None bei !=200."""
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None
    time.sleep(PER_REQUEST_SLEEP)
    if r.status_code != 200:
        print(f"  ! {url} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    return r.text


def _collect_product_urls(client: httpx.Client, base_url: str,
                          sitemap_filter: str,
                          product_url_re: re.Pattern) -> list[str]:
    """Holt den Sitemap-Index, folgt allen Sub-Sitemaps, deren URL
    'sitemap_filter' als Substring enthaelt, und liefert die deduplizierten
    Produkt-URLs (alle <loc>-Eintraege, die 'product_url_re' matchen)."""
    index_url = urljoin(base_url, SITEMAP_INDEX_PATH)
    body = _get(client, index_url)
    if not body:
        return []
    sub_sitemaps = [
        html.unescape(m.group(1)) for m in _LOC_RE.finditer(body)
        if sitemap_filter in m.group(1)
    ]
    if not sub_sitemaps:
        print(f"  ! Keine passenden Sub-Sitemaps "
              f"(filter={sitemap_filter!r}) in {index_url}.", file=sys.stderr)
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for sm in sub_sitemaps:
        body = _get(client, sm)
        if not body:
            continue
        for m in _LOC_RE.finditer(body):
            u = html.unescape(m.group(1))
            if product_url_re.search(u) and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


# --- Produktseite-Parser ------------------------------------------------------

# itemprop-Felder, die wir aus dem Product-Block brauchen. priceCurrency und
# availability kommen ueblicherweise nur im Product-Block vor; 'name' und
# 'brand' koennen auch andernorts auftauchen (z.B. Store-Namen im Footer).
# Wir nehmen die LETZTE Fundstelle - der Product-Block steht im Quelltext
# nach Header-/Footer-Bloecken.
_ITEMPROP_RE = re.compile(
    r'itemprop="(?P<prop>brand|name|price|priceCurrency|availability)"'
    r'[^>]*?(?:'
    r'content="(?P<content>[^"]*)"'
    r'|href="(?P<href>[^"]*)"'
    r'|>\s*(?P<text>[^<]{0,300})<'
    r')',
    re.I,
)

_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAGS_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _TAGS_RE.sub(" ", s)).strip()


def _parse_product_page(url: str, body: str,
                        product_url_re: re.Pattern) -> dict | None:
    """Extrahiert einen Record aus einer Produktseite. None, wenn kein
    verwertbarer Preis gefunden wurde (Seite tot/ausgelistet)."""
    props: dict[str, str] = {}
    for m in _ITEMPROP_RE.finditer(body):
        prop = m.group("prop").lower()
        val = m.group("content") or m.group("href") or (m.group("text") or "")
        val = html.unescape(val).strip()
        if val:
            props[prop] = val

    raw_price = props.get("price")
    if not raw_price:
        return None
    try:
        price = float(raw_price.replace(",", "."))
    except ValueError:
        return None

    # Name: bevorzugt aus <h1> (kompakter, enthaelt typischerweise Brand+Name
    # zusammen); Fallback auf itemprop="name". Brand voranstellen, falls sie
    # noch nicht im Namen vorkommt - der Matcher tokenisiert nur den Namen.
    name_h1 = ""
    m_h1 = _H1_RE.search(body)
    if m_h1:
        name_h1 = _strip_tags(m_h1.group(1))
    name_item = props.get("name", "")
    name = name_h1 or name_item
    brand = props.get("brand", "")
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}".strip()
    if not name:
        return None

    avail_raw = props.get("availability", "")
    if avail_raw in _AVAIL_IN:
        available: bool | None = True
    elif avail_raw in _AVAIL_OUT:
        available = False
    else:
        available = None

    m_id = product_url_re.search(url)
    variant_ref = m_id.group(1) if m_id else None

    return {
        "name": name,
        "reference": None,
        "ean13": None,
        "upc": None,
        "price": price,
        "currency": (props.get("pricecurrency") or "").upper() or None,
        "url": url,
        "variant_ref": variant_ref,
        "available": available,
        "variant_label": None,
    }


# --- Public API ---------------------------------------------------------------

def fetch(base_url: str, *, sitemap_filter: str, product_url_regex: str,
          limit: int | None = None) -> list[dict]:
    """Crawlt den Shop unter 'base_url' und liefert normalisierte Records.

    sitemap_filter      Substring fuer die Auswahl der Shop-Sub-Sitemaps.
    product_url_regex   Regex mit einer Capture-Gruppe fuer die variant_ref;
                        nur passende <loc>-Eintraege werden als Produkte
                        gezaehlt.
    limit               Optional: nur die ersten N URLs verarbeiten (Test).
    """
    product_url_re = re.compile(product_url_regex)
    records: list[dict] = []
    with httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-CH,de;q=0.9",
        },
        timeout=TIMEOUT,
        follow_redirects=True,
    ) as client:
        urls = _collect_product_urls(client, base_url, sitemap_filter,
                                     product_url_re)
        if limit is not None:
            urls = urls[:limit]
        print(f"  {len(urls)} Produkt-URLs in Sitemap", file=sys.stderr)
        for i, url in enumerate(urls, 1):
            body = _get(client, url)
            if not body:
                continue
            rec = _parse_product_page(url, body, product_url_re)
            if rec is None:
                continue
            records.append(rec)
            if i % 100 == 0:
                print(f"  ... {i}/{len(urls)} Seiten verarbeitet "
                      f"({len(records)} verwertbar)", file=sys.stderr)
    return records


# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Sitemap- + schema.org-Crawler fuer einen Shop.")
    ap.add_argument("base_url", help="Shop-Basis-URL, z.B. https://example.com")
    ap.add_argument("--sitemap-filter", required=True,
                    help="Substring zur Auswahl der Shop-Sub-Sitemaps.")
    ap.add_argument("--product-url-regex", required=True,
                    help="Regex fuer Produkt-URLs; Gruppe 1 = variant_ref.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Nur die ersten N Produkt-URLs verarbeiten (Testlauf).")
    args = ap.parse_args()
    recs = fetch(args.base_url,
                 sitemap_filter=args.sitemap_filter,
                 product_url_regex=args.product_url_regex,
                 limit=args.limit)
    print(f"\n{len(recs)} Records geholt.")
    for r in recs[:5]:
        print(f"  - {r['name']!r}  {r['price']} {r['currency']}  "
              f"stock={r['available']}  id={r['variant_ref']}")
        print(f"    {r['url']}")
