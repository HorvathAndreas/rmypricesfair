#!/usr/bin/env python3
"""
db.py - Datenzugriffs-Schicht fuer das Preis-Monitoring.

Nur Standardbibliothek (sqlite3), keine externen Abhaengigkeiten.
Enthaelt die idempotente Schema-Initialisierung und die Zugriffs-Funktionen,
auf denen Fetcher, Catalog-Sync, Matcher, Updater und Reporter aufsetzen.

CLI:
    python db.py init [pfad]      # legt das Schema an (Standard: data/prices.db)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).parent / "data" / "prices.db"

# --- Schema (Referenzwaehrung = CHF, kein Preisverlauf) -----------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_variant (
    id_product           INTEGER NOT NULL,
    id_product_attribute INTEGER NOT NULL DEFAULT 0,
    reference            TEXT,
    ean13                TEXT,
    upc                  TEXT,
    name                 TEXT NOT NULL,
    variant_label        TEXT,
    price                REAL,
    currency             TEXT NOT NULL DEFAULT 'CHF',
    active               INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (id_product, id_product_attribute)
);
CREATE INDEX IF NOT EXISTS idx_my_variant_ean13     ON my_variant(ean13);
CREATE INDEX IF NOT EXISTS idx_my_variant_reference ON my_variant(reference);

CREATE TABLE IF NOT EXISTS competitor (
    competitor_id  INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    base_url       TEXT NOT NULL,
    platform       TEXT NOT NULL DEFAULT 'shopify',
    currency       TEXT NOT NULL DEFAULT 'EUR',
    fetcher_config TEXT,                       -- JSON, plattformspezifische Knoepfe
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS listing (
    listing_id           INTEGER PRIMARY KEY,
    id_product           INTEGER NOT NULL,
    id_product_attribute INTEGER NOT NULL DEFAULT 0,
    competitor_id        INTEGER NOT NULL REFERENCES competitor(competitor_id),
    comp_name        TEXT,
    comp_reference   TEXT,
    comp_ean13       TEXT,
    comp_upc         TEXT,
    comp_url         TEXT,
    comp_variant_ref TEXT,
    match_method     TEXT,
    confirmed        INTEGER NOT NULL DEFAULT 0,
    last_price       REAL,
    last_currency    TEXT,
    in_stock         INTEGER,
    price_changed_at TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (id_product, id_product_attribute)
        REFERENCES my_variant(id_product, id_product_attribute),
    UNIQUE (id_product, id_product_attribute, competitor_id)
);
CREATE INDEX IF NOT EXISTS idx_listing_variant ON listing(id_product, id_product_attribute);

CREATE TABLE IF NOT EXISTS fx_rate (
    currency    TEXT PRIMARY KEY,
    rate_to_chf REAL NOT NULL,
    updated_at  TEXT
);

-- Pro-Land-Preis fuer die eigene Variante. Der Master-Shop fuellt den Eintrag
-- fuer sein eigenes Land (z.B. CH). Weitere Shops (is_master=false in
-- config.yaml) fuellen ihre Laender-Eintraege (z.B. DE). present_results.py
-- waehlt pro Mitbewerber die Zeile mit passendem country_iso und faellt
-- sonst auf my_variant.price + FX zurueck.
CREATE TABLE IF NOT EXISTS my_variant_price (
    id_product           INTEGER NOT NULL,
    id_product_attribute INTEGER NOT NULL DEFAULT 0,
    country_iso          TEXT NOT NULL,
    price                REAL NOT NULL,
    currency             TEXT NOT NULL,
    PRIMARY KEY (id_product, id_product_attribute, country_iso),
    FOREIGN KEY (id_product, id_product_attribute)
        REFERENCES my_variant(id_product, id_product_attribute)
);
CREATE INDEX IF NOT EXISTS idx_mvp_country ON my_variant_price(country_iso);

CREATE TABLE IF NOT EXISTS match_candidate (
    candidate_id         INTEGER PRIMARY KEY,
    id_product           INTEGER NOT NULL,
    id_product_attribute INTEGER NOT NULL DEFAULT 0,
    competitor_id        INTEGER NOT NULL REFERENCES competitor(competitor_id),
    comp_name            TEXT,
    comp_reference       TEXT,
    comp_ean13           TEXT,
    comp_upc             TEXT,
    comp_url             TEXT,
    comp_variant_ref     TEXT,
    method               TEXT NOT NULL,
    score                REAL NOT NULL,
    FOREIGN KEY (id_product, id_product_attribute)
        REFERENCES my_variant(id_product, id_product_attribute)
);
CREATE INDEX IF NOT EXISTS idx_cand_variant
    ON match_candidate(id_product, id_product_attribute, competitor_id);
CREATE INDEX IF NOT EXISTS idx_cand_score ON match_candidate(score DESC);
"""

# Seed-Kurse: werden nur eingefuegt, wenn die Waehrung noch fehlt (kein Ueberschreiben).
SEED_FX = [
    ("CHF", 1.0, None),
    ("EUR", 0.95, dt.date.today().isoformat()),  # Beispiel, von Hand pflegen
]


# --- Verbindung & Initialisierung ---------------------------------------------

def get_connection(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Oeffnet die DB, aktiviert Foreign Keys und liefert Zeilen als sqlite3.Row."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Legt Schema und Seed-Kurse an. Idempotent - mehrfach aufrufbar."""
    conn.executescript(SCHEMA)
    # Migrations: CREATE TABLE IF NOT EXISTS aktualisiert keine bestehende
    # Tabelle, daher fehlende Spalten manuell nachziehen.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(competitor)")}
    if "fetcher_config" not in cols:
        conn.execute("ALTER TABLE competitor ADD COLUMN fetcher_config TEXT")
    if "country_iso" not in cols:
        conn.execute("ALTER TABLE competitor ADD COLUMN country_iso TEXT")
        # Best-effort Backfill ueber die TLD der base_url. Bei Unsicherheit
        # bleibt NULL - present_results.py faellt dann auf den Master-Preis
        # mit FX-Umrechnung zurueck (= bisheriges Verhalten).
        for r in conn.execute(
            "SELECT competitor_id, base_url FROM competitor "
            "WHERE country_iso IS NULL").fetchall():
            iso = _tld_country_iso(r[1])
            if iso:
                conn.execute(
                    "UPDATE competitor SET country_iso=? WHERE competitor_id=?",
                    (iso, r[0]))
    conn.executemany(
        "INSERT OR IGNORE INTO fx_rate (currency, rate_to_chf, updated_at) VALUES (?, ?, ?)",
        SEED_FX,
    )
    conn.commit()


# --- Hilfsfunktionen ----------------------------------------------------------

_ISO_TLDS = {"ch", "de", "at", "fr", "it", "li", "nl", "be", "lu",
             "es", "pt", "se", "no", "dk", "fi", "pl", "cz"}


def _tld_country_iso(base_url: str | None) -> str | None:
    """Heuristisch das Land aus der Top-Level-Domain ableiten.
    Liefert ISO-3166-1-alpha-2 oder None. Bsp.: '.ch' -> 'CH', '.de' -> 'DE'.
    Nicht-Laender-TLDs (com/org/net etc.) -> None."""
    if not base_url:
        return None
    m = re.search(r"\.([a-z]{2})(?::\d+)?(?:/|$)", base_url.lower())
    if not m:
        return None
    tld = m.group(1)
    return tld.upper() if tld in _ISO_TLDS else None


# --- Eigener Katalog (my_variant) ---------------------------------------------

_VARIANT_DEFAULTS = {
    "id_product_attribute": 0, "reference": None, "ean13": None, "upc": None,
    "variant_label": None, "price": None, "currency": "CHF", "active": 1,
}


def upsert_variant(conn: sqlite3.Connection, v: dict) -> None:
    """Fuegt eine Variante ein oder aktualisiert sie (Schluessel: id_product + id_product_attribute)."""
    row = {**_VARIANT_DEFAULTS, **v}
    conn.execute(
        """
        INSERT INTO my_variant
            (id_product, id_product_attribute, reference, ean13, upc,
             name, variant_label, price, currency, active)
        VALUES
            (:id_product, :id_product_attribute, :reference, :ean13, :upc,
             :name, :variant_label, :price, :currency, :active)
        ON CONFLICT(id_product, id_product_attribute) DO UPDATE SET
            reference=excluded.reference, ean13=excluded.ean13, upc=excluded.upc,
            name=excluded.name, variant_label=excluded.variant_label,
            price=excluded.price, currency=excluded.currency, active=excluded.active
        """,
        row,
    )


def get_unmatched_variants(conn: sqlite3.Connection, competitor_id: int) -> list[sqlite3.Row]:
    """Aktive Varianten, die fuer diesen Mitbewerber noch kein listing haben."""
    return conn.execute(
        """
        SELECT v.* FROM my_variant v
        WHERE v.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM listing l
              WHERE l.id_product = v.id_product
                AND l.id_product_attribute = v.id_product_attribute
                AND l.competitor_id = ?
          )
        """,
        (competitor_id,),
    ).fetchall()


# --- Mitbewerber ---------------------------------------------------------------

def get_active_competitors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM competitor WHERE active = 1 ORDER BY name").fetchall()


def upsert_competitor(conn: sqlite3.Connection, name: str, base_url: str,
                      platform: str = "shopify", currency: str = "EUR",
                      fetcher_config: dict | None = None,
                      country_iso: str | None = None) -> int:
    """Legt einen Mitbewerber an (oder aktualisiert per Name) und gibt die
    competitor_id zurueck. 'fetcher_config' wird als JSON gespeichert und
    spaeter als kwargs an die Fetcher-Funktion durchgereicht. 'country_iso'
    (z.B. 'CH', 'DE') steuert, welcher eigene Preis aus my_variant_price
    fuer den Vergleich herangezogen wird."""
    cfg_json = json.dumps(fetcher_config) if fetcher_config is not None else None
    iso = country_iso or _tld_country_iso(base_url)
    existing = conn.execute("SELECT competitor_id FROM competitor WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE competitor SET base_url=?, platform=?, currency=?, "
            "fetcher_config=?, country_iso=COALESCE(?, country_iso) "
            "WHERE competitor_id=?",
            (base_url, platform, currency, cfg_json, iso,
             existing["competitor_id"]),
        )
        return existing["competitor_id"]
    cur = conn.execute(
        "INSERT INTO competitor (name, base_url, platform, currency, "
        "fetcher_config, country_iso) VALUES (?, ?, ?, ?, ?, ?)",
        (name, base_url, platform, currency, cfg_json, iso),
    )
    return cur.lastrowid


# --- Eigene Preise pro Land ---------------------------------------------------

def upsert_my_variant_price(conn: sqlite3.Connection, id_product: int,
                            id_product_attribute: int, country_iso: str,
                            price: float, currency: str) -> None:
    """Schreibt/aktualisiert den eigenen Preis fuer eine Variante in einem
    bestimmten Land."""
    conn.execute(
        """
        INSERT INTO my_variant_price
            (id_product, id_product_attribute, country_iso, price, currency)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id_product, id_product_attribute, country_iso) DO UPDATE SET
            price = excluded.price,
            currency = excluded.currency
        """,
        (id_product, id_product_attribute, country_iso, price, currency),
    )


def clear_my_variant_prices(conn: sqlite3.Connection, country_iso: str) -> int:
    """Leert alle Eintraege fuer ein Land - vor jedem Shop-Sync aufrufen,
    damit Stale-Eintraege verschwinden."""
    cur = conn.execute(
        "DELETE FROM my_variant_price WHERE country_iso = ?", (country_iso,))
    return cur.rowcount


# --- Listings (Zuordnung + aktueller Preis) -----------------------------------

_LISTING_DEFAULTS = {
    "comp_name": None, "comp_reference": None, "comp_ean13": None, "comp_upc": None,
    "comp_url": None, "comp_variant_ref": None, "match_method": None, "confirmed": 0,
}


def upsert_listing(conn: sqlite3.Connection, l: dict) -> None:
    """Legt eine Zuordnung an oder aktualisiert deren Mitbewerber-Identitaet.
    'confirmed' wird bei Konflikt bewusst NICHT ueberschrieben, damit ein erneuter
    Matcher-Lauf manuelle Bestaetigungen nicht zuruecksetzt."""
    row = {**_LISTING_DEFAULTS, **l}
    conn.execute(
        """
        INSERT INTO listing
            (id_product, id_product_attribute, competitor_id,
             comp_name, comp_reference, comp_ean13, comp_upc, comp_url, comp_variant_ref,
             match_method, confirmed)
        VALUES
            (:id_product, :id_product_attribute, :competitor_id,
             :comp_name, :comp_reference, :comp_ean13, :comp_upc, :comp_url, :comp_variant_ref,
             :match_method, :confirmed)
        ON CONFLICT(id_product, id_product_attribute, competitor_id) DO UPDATE SET
            comp_name=excluded.comp_name, comp_reference=excluded.comp_reference,
            comp_ean13=excluded.comp_ean13, comp_upc=excluded.comp_upc,
            comp_url=excluded.comp_url, comp_variant_ref=excluded.comp_variant_ref,
            match_method=excluded.match_method
        """,
        row,
    )


def get_confirmed_listings(conn: sqlite3.Connection, competitor_id: int) -> list[sqlite3.Row]:
    """Bestaetigte, aktive Listings eines Mitbewerbers (Basis fuer den Updater)."""
    return conn.execute(
        "SELECT * FROM listing WHERE competitor_id = ? AND confirmed = 1 AND active = 1",
        (competitor_id,),
    ).fetchall()


def update_listing_price(conn: sqlite3.Connection, listing_id: int, price: float,
                         currency: str, in_stock: int | None, now: str | None = None) -> bool:
    """Schreibt den aktuellen Stand. 'price_changed_at' wird NUR bei tatsaechlicher
    Preisaenderung gesetzt. Bei fehlgeschlagenem Abruf (price=None) NICHT aufrufen -
    dann bleibt die Zeile unangetastet. Gibt True zurueck, wenn sich der Preis aenderte."""
    if price is None:
        raise ValueError("update_listing_price nicht mit price=None aufrufen (Abruf fehlgeschlagen).")
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    row = conn.execute("SELECT last_price FROM listing WHERE listing_id = ?", (listing_id,)).fetchone()
    if row is None:
        raise ValueError(f"listing_id {listing_id} existiert nicht.")
    old = row["last_price"]
    changed = old is None or abs(old - price) > 1e-9
    if changed:
        conn.execute(
            "UPDATE listing SET last_price=?, last_currency=?, in_stock=?, price_changed_at=? "
            "WHERE listing_id=?",
            (price, currency, in_stock, now, listing_id),
        )
    else:
        # Preis gleich -> nur Lagerstatus aktualisieren, Zeitstempel bleibt stehen
        conn.execute("UPDATE listing SET in_stock=? WHERE listing_id=?", (in_stock, listing_id))
    return changed


# --- Match-Kandidaten (Fuzzy-Vorschlaege fuers Review-Dialog) -----------------

def clear_candidates(conn: sqlite3.Connection, competitor_id: int) -> int:
    """Loescht alle Kandidaten eines Mitbewerbers (vor jedem Matcher-Lauf).
    Liefert die Anzahl geloeschter Zeilen."""
    cur = conn.execute("DELETE FROM match_candidate WHERE competitor_id = ?", (competitor_id,))
    return cur.rowcount


def insert_candidate(conn: sqlite3.Connection, c: dict) -> None:
    """Fuegt einen Kandidaten ein. Pflichtfelder: id_product, id_product_attribute,
    competitor_id, method, score."""
    conn.execute(
        """
        INSERT INTO match_candidate
            (id_product, id_product_attribute, competitor_id,
             comp_name, comp_reference, comp_ean13, comp_upc, comp_url, comp_variant_ref,
             method, score)
        VALUES
            (:id_product, :id_product_attribute, :competitor_id,
             :comp_name, :comp_reference, :comp_ean13, :comp_upc, :comp_url, :comp_variant_ref,
             :method, :score)
        """,
        {
            "comp_name": None, "comp_reference": None, "comp_ean13": None, "comp_upc": None,
            "comp_url": None, "comp_variant_ref": None,
            **c,
        },
    )


def delete_candidates_for_variant(conn: sqlite3.Connection, id_product: int,
                                  id_product_attribute: int, competitor_id: int) -> int:
    """Loescht die Kandidaten einer einzelnen Variante bei einem Mitbewerber.
    Wird vom Review-Dialog aufgerufen, sobald eine Entscheidung gefallen ist."""
    cur = conn.execute(
        "DELETE FROM match_candidate WHERE id_product = ? "
        "AND id_product_attribute = ? AND competitor_id = ?",
        (id_product, id_product_attribute, competitor_id),
    )
    return cur.rowcount


def get_candidates(conn: sqlite3.Connection, id_product: int,
                   id_product_attribute: int, competitor_id: int) -> list[sqlite3.Row]:
    """Kandidaten fuer eine Variante bei einem Mitbewerber, nach Score absteigend."""
    return conn.execute(
        """
        SELECT * FROM match_candidate
        WHERE id_product = ? AND id_product_attribute = ? AND competitor_id = ?
        ORDER BY score DESC
        """,
        (id_product, id_product_attribute, competitor_id),
    ).fetchall()


def get_variants_with_candidates(conn: sqlite3.Connection,
                                 competitor_id: int) -> list[sqlite3.Row]:
    """Varianten, fuer die noch kein bestaetigtes listing existiert, aber Kandidaten.
    Wird vom Review-Dialog verwendet."""
    return conn.execute(
        """
        SELECT v.id_product, v.id_product_attribute, v.name, v.variant_label,
               v.reference, v.ean13, v.price, v.currency,
               COUNT(c.candidate_id) AS n_candidates,
               MAX(c.score) AS best_score
        FROM my_variant v
        JOIN match_candidate c
            ON c.id_product = v.id_product
           AND c.id_product_attribute = v.id_product_attribute
        WHERE c.competitor_id = ?
          AND v.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM listing l
              WHERE l.id_product = v.id_product
                AND l.id_product_attribute = v.id_product_attribute
                AND l.competitor_id = ?
                AND l.confirmed = 1
          )
        GROUP BY v.id_product, v.id_product_attribute
        ORDER BY best_score DESC
        """,
        (competitor_id, competitor_id),
    ).fetchall()


# --- Wechselkurse --------------------------------------------------------------

def get_fx_rates(conn: sqlite3.Connection) -> dict[str, float]:
    return {r["currency"]: r["rate_to_chf"] for r in conn.execute("SELECT currency, rate_to_chf FROM fx_rate")}


def set_fx_rate(conn: sqlite3.Connection, currency: str, rate_to_chf: float,
                updated_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO fx_rate (currency, rate_to_chf, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(currency) DO UPDATE SET rate_to_chf=excluded.rate_to_chf, updated_at=excluded.updated_at",
        (currency, rate_to_chf, updated_at or dt.date.today().isoformat()),
    )


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "init":
        path = Path(argv[2]) if len(argv) >= 3 else DEFAULT_DB
        conn = get_connection(path)
        init_db(conn)
        conn.close()
        print(f"Schema initialisiert: {path}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
