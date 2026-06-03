#!/usr/bin/env python3
"""
coverage.py - interaktiv: pro eigener Variante sehen, wie sie bei jedem
              aktiven Mitbewerber abgedeckt ist, und Luecken manuell schliessen.

Pro Variante eine Status-Zeile fuer jeden Mitbewerber:
  MATCH     confirmed=1 Listing vorhanden (mit aktuellem Preis, falls erfasst)
  NO-MATCH  confirmed=1, match_method='no-match' (bewusst leer markiert)
  AUTO      confirmed=0 (matcher hat hart vorgeschlagen, noch nicht via
            review.py bestaetigt)
  REVIEW    Match-Kandidaten vorhanden -> review.py nutzen
  LUECKE    gar nichts

Aktionen pro Variante:
  u <n>   URL fuer Mitbewerber Nr. n hinterlegen (manuelles Listing)
  n <n>   Mitbewerber Nr. n als 'kein Treffer' markieren
  s       Skip - naechste Variante
  q       Quit
  Enter   naechste Variante

Manuelles Hinterlegen einer URL ruft den plattform-spezifischen
'fetch_one'-Helfer auf, parst die Seite, zeigt eine Vorschau und schreibt
nach Bestaetigung ein confirmed=1 Listing inkl. aktuellem Preis. Aktuell
unterstuetzt nur die Plattform 'schema_org' das manuelle URL-Hinterlegen.

Aufrufe:
    python coverage.py                  # alle Varianten
    python coverage.py --only-gaps      # nur Varianten mit >=1 Luecke
    python coverage.py --competitor X   # Status fuer X anzeigen,
                                        # nur Varianten mit Luecke/Review bei X
"""

from __future__ import annotations

import argparse
import json
import sys

from db import (DEFAULT_DB, get_active_competitors, get_connection, init_db,
                update_listing_price, upsert_listing)

import schema_org

# Plattform -> single-URL Fetcher. Wer hier fehlt, kann nicht manuell per URL
# verlinkt werden (der User muesste manuell die Felder pflegen).
SINGLE_URL_FETCHERS = {
    "schema_org": schema_org.fetch_one,
}


# --- Datenbeschaffung ---------------------------------------------------------

def _load_state(conn, only_competitor: str | None):
    """Laedt aktive Varianten, aktive Mitbewerber (optional gefiltert),
    bestehende Listings (active=1) und die aggregierten Kandidaten-Counts
    in einem Rutsch."""
    variants = conn.execute(
        "SELECT * FROM my_variant WHERE active=1 ORDER BY name, id_product"
    ).fetchall()
    comps = get_active_competitors(conn)
    if only_competitor:
        comps = [c for c in comps if c["name"] == only_competitor]
        if not comps:
            print(f"Aktiver Mitbewerber '{only_competitor}' nicht gefunden.",
                  file=sys.stderr)
            return None, None, None, None

    listings: dict[tuple[int, int, int], dict] = {}
    for r in conn.execute("SELECT * FROM listing WHERE active=1").fetchall():
        listings[(r["id_product"], r["id_product_attribute"],
                  r["competitor_id"])] = r

    cands: dict[tuple[int, int, int], tuple[int, float]] = {}
    for r in conn.execute(
        "SELECT id_product, id_product_attribute, competitor_id, "
        "       COUNT(*) AS n, MAX(score) AS best "
        "FROM match_candidate "
        "GROUP BY id_product, id_product_attribute, competitor_id"
    ).fetchall():
        cands[(r["id_product"], r["id_product_attribute"],
               r["competitor_id"])] = (r["n"], r["best"])

    return variants, comps, listings, cands


def _status(listings, cands, vkey, cid):
    """Liefert ein Tupel (label, payload) fuer eine (variant, competitor)-Zelle."""
    key = (*vkey, cid)
    l = listings.get(key)
    if l is not None:
        if l["match_method"] == "no-match":
            return ("NO-MATCH", None)
        if l["confirmed"]:
            return ("MATCH", l)
        return ("AUTO", l)
    cand = cands.get(key)
    if cand is not None:
        return ("REVIEW", cand)
    return ("LUECKE", None)


def _has_gap(listings, cands, vkey, comp_ids) -> bool:
    return any(_status(listings, cands, vkey, cid)[0] == "LUECKE"
               for cid in comp_ids)


def _has_unresolved_for(listings, cands, vkey, cid) -> bool:
    """Variante hat fuer DIESEN Mitbewerber etwas zu tun (Luecke oder Review)."""
    lbl, _ = _status(listings, cands, vkey, cid)
    return lbl in ("LUECKE", "REVIEW")


# --- Anzeige ------------------------------------------------------------------

def _fmt_status(label: str, payload, name_w: int) -> str:
    if label == "MATCH":
        l = payload
        price = (f"{l['last_price']:.2f} {l['last_currency'] or ''}".strip()
                 if l["last_price"] is not None else "(kein Preis erfasst)")
        url = l["comp_url"] or "(keine URL)"
        return f"MATCH     {price:<14}  {url}"
    if label == "NO-MATCH":
        return "NO-MATCH"
    if label == "AUTO":
        l = payload
        url = l["comp_url"] or "(keine URL)"
        return f"AUTO      vorgeschlagen   {url}  -> review.py"
    if label == "REVIEW":
        n, best = payload
        return f"REVIEW    {n} Kandidat(en), best {best:.2f}  -> review.py"
    if label == "LUECKE":
        return "LUECKE"
    return label


def _print_variant(v, comps, listings, cands, idx, total) -> list[dict]:
    """Zeigt eine Variante mit ihrem Status-Block.
    Liefert die geordnete Mitbewerber-Liste (so wie sie nummeriert wurde)."""
    print()
    print("─" * 80)
    title = v["name"]
    if v["variant_label"]:
        title += f"   [{v['variant_label']}]"
    price = (f"{v['price']:.2f} {v['currency']}"
             if v["price"] is not None else "(ohne Preis)")
    print(f"[{idx}/{total}]  {title}   {price}")
    print(f"  id={v['id_product']}/{v['id_product_attribute']}   "
          f"ref={v['reference'] or '-'}   ean={v['ean13'] or '-'}")
    print()

    vkey = (v["id_product"], v["id_product_attribute"])
    name_w = max(len(c["name"]) for c in comps)
    for i, c in enumerate(comps, 1):
        lbl, payload = _status(listings, cands, vkey, c["competitor_id"])
        print(f"  [{i}] {c['name']:<{name_w}}  {_fmt_status(lbl, payload, name_w)}")
    return comps


# --- Aktionen -----------------------------------------------------------------

def _safe_input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nAbbruch.")
        return None


def _ask_url(comp_name: str) -> str | None:
    while True:
        s = _safe_input(f"  URL fuer {comp_name}: ")
        if s is None:
            return None
        s = s.strip()
        if not s:
            return None
        if not (s.startswith("http://") or s.startswith("https://")):
            print("  -> Bitte eine vollstaendige http(s)://-URL eingeben.")
            continue
        return s


def _link_manual(conn, v, comp) -> bool:
    """Interaktiver Pfad: URL erfragen, Seite ueber fetch_one parsen,
    Vorschau zeigen, bei Bestaetigung confirmed=1 Listing + Preis schreiben.
    Liefert True bei Erfolg."""
    fetch_one = SINGLE_URL_FETCHERS.get(comp["platform"])
    if fetch_one is None:
        print(f"  -> Plattform '{comp['platform']}' unterstuetzt derzeit kein "
              f"manuelles URL-Hinterlegen ueber dieses Tool.")
        return False
    url = _ask_url(comp["name"])
    if not url:
        return False
    cfg_raw = comp["fetcher_config"] if "fetcher_config" in comp.keys() else None
    cfg = json.loads(cfg_raw) if cfg_raw else {}
    print(f"  ... lade {url}")
    try:
        rec = fetch_one(url, **cfg)
    except Exception as e:
        print(f"  -> Fehler beim Laden/Parsen: {e}")
        return False
    if rec is None:
        print("  -> Konnte aus der Seite keinen Preis extrahieren. Abgebrochen.")
        return False

    print()
    print(f"  Gefunden:  {rec['name']}")
    print(f"             {rec['price']:.2f} {rec['currency'] or '?'}   "
          f"stock={rec['available']}   variant_ref={rec['variant_ref']}")
    if not rec["variant_ref"]:
        print("  ! Hinweis: keine variant_ref aus URL ableitbar - kuenftige "
              "updater-Laeufe finden die Seite ggf. nicht mehr automatisch.")
    confirm = _safe_input("  Speichern? [j/N] > ")
    if confirm is None or confirm.strip().lower() not in ("j", "y", "ja", "yes"):
        print("  -> Nicht gespeichert.")
        return False

    upsert_listing(conn, {
        "id_product": v["id_product"],
        "id_product_attribute": v["id_product_attribute"],
        "competitor_id": comp["competitor_id"],
        "comp_name": rec["name"],
        "comp_reference": rec.get("reference"),
        "comp_ean13": rec.get("ean13"),
        "comp_upc": rec.get("upc"),
        "comp_url": rec["url"],
        "comp_variant_ref": rec["variant_ref"],
        "match_method": "manual",
        "confirmed": 1,
    })
    # listing_id holen, um Preis nachzuschreiben.
    lr = conn.execute(
        "SELECT listing_id FROM listing WHERE id_product=? "
        "AND id_product_attribute=? AND competitor_id=?",
        (v["id_product"], v["id_product_attribute"], comp["competitor_id"]),
    ).fetchone()
    if rec.get("price") is not None and lr is not None:
        avail = rec.get("available")
        in_stock = 1 if avail is True else (0 if avail is False else None)
        update_listing_price(conn, lr["listing_id"], float(rec["price"]),
                             rec["currency"], in_stock)
    conn.commit()
    print("  -> Gespeichert (confirmed=1, match_method=manual) inkl. aktuellem Preis.")
    return True


def _mark_no_match(conn, v, comp) -> None:
    upsert_listing(conn, {
        "id_product": v["id_product"],
        "id_product_attribute": v["id_product_attribute"],
        "competitor_id": comp["competitor_id"],
        "comp_name": None, "comp_reference": None, "comp_ean13": None,
        "comp_upc": None, "comp_url": None, "comp_variant_ref": None,
        "match_method": "no-match",
        "confirmed": 1,
    })
    conn.commit()
    print(f"  -> {comp['name']}: als 'no-match' gespeichert.")


# --- Hauptschleife ------------------------------------------------------------

def _parse_action(raw: str, n_comps: int) -> tuple[str, int | None] | None:
    """Parst 'u 3', 'u3', 'n 2', 's', 'q' usw. Liefert (verb, idx_or_None)
    oder None bei nicht-erkannter Eingabe."""
    raw = raw.strip().lower()
    if not raw:
        return ("skip", None)
    if raw == "q":
        return ("quit", None)
    if raw == "s":
        return ("skip", None)
    parts = raw.split()
    verb = parts[0][0]
    arg_str = parts[1] if len(parts) > 1 else parts[0][1:]
    if verb not in ("u", "n"):
        return None
    if not arg_str.isdigit():
        return None
    idx = int(arg_str)
    if not (1 <= idx <= n_comps):
        return None
    return (verb, idx)


def _run_dialog(conn, variants, comps, listings, cands, only_competitor: str | None) -> None:
    # Listings/Kandidaten cachen wir lokal; nach jeder Aktion targetiert
    # nachladen, damit der naechste Variant-Print frisch ist.
    def _reload_for(vkey):
        # Listings dieser Variante neu einlesen.
        for cid in (c["competitor_id"] for c in comps):
            r = conn.execute(
                "SELECT * FROM listing WHERE id_product=? "
                "AND id_product_attribute=? AND competitor_id=? AND active=1",
                (*vkey, cid),
            ).fetchone()
            key = (*vkey, cid)
            if r is None:
                listings.pop(key, None)
            else:
                listings[key] = r

    total = len(variants)
    for idx, v in enumerate(variants, 1):
        vkey = (v["id_product"], v["id_product_attribute"])
        _print_variant(v, comps, listings, cands, idx, total)
        # Pro Variante kann der User mehrere Aktionen ausfuehren, bis er
        # weiter geht (Enter/s/q).
        while True:
            prompt = ("  u <n> URL | n <n> kein-Treffer | s skip | q quit > ")
            raw = _safe_input(prompt)
            if raw is None:
                return
            parsed = _parse_action(raw, len(comps))
            if parsed is None:
                print("  -> Eingabe nicht erkannt.")
                continue
            verb, arg = parsed
            if verb == "quit":
                print("\nFortschritt gespeichert. Nochmal coverage.py fuer den Rest.")
                return
            if verb == "skip":
                break
            comp = comps[arg - 1]
            if verb == "u":
                ok = _link_manual(conn, v, comp)
                if ok:
                    _reload_for(vkey)
                    # Nach Erfolg Status neu drucken (zeigt MATCH).
                    print()
                    name_w = max(len(c["name"]) for c in comps)
                    for i, c in enumerate(comps, 1):
                        lbl, payload = _status(listings, cands, vkey,
                                               c["competitor_id"])
                        print(f"  [{i}] {c['name']:<{name_w}}  "
                              f"{_fmt_status(lbl, payload, name_w)}")
            elif verb == "n":
                _mark_no_match(conn, v, comp)
                _reload_for(vkey)


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Pro Variante Mitbewerber-Abdeckung interaktiv reviewen.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="DB-Pfad")
    ap.add_argument("--only-gaps", action="store_true",
                    help="Nur Varianten zeigen, die bei mindestens einem "
                         "Mitbewerber eine LUECKE haben.")
    ap.add_argument("--competitor", default=None,
                    help="Nur Status fuer diesen Mitbewerber zeigen; filtert "
                         "ausserdem auf Varianten mit LUECKE/REVIEW bei ihm.")
    args = ap.parse_args(argv[1:])

    if not sys.stdin.isatty():
        print("coverage.py ist interaktiv und braucht ein TTY.", file=sys.stderr)
        return 2

    conn = get_connection(args.db)
    init_db(conn)

    variants, comps, listings, cands = _load_state(conn, args.competitor)
    if variants is None:
        return 2
    if not comps:
        print("Keine aktiven Mitbewerber.", file=sys.stderr)
        return 1
    if not variants:
        print("Keine aktiven Varianten in my_variant.", file=sys.stderr)
        return 1

    comp_ids = [c["competitor_id"] for c in comps]
    if args.competitor:
        cid = comps[0]["competitor_id"]
        variants = [v for v in variants
                    if _has_unresolved_for(listings, cands,
                                           (v["id_product"], v["id_product_attribute"]),
                                           cid)]
    elif args.only_gaps:
        variants = [v for v in variants
                    if _has_gap(listings, cands,
                                (v["id_product"], v["id_product_attribute"]),
                                comp_ids)]

    if not variants:
        print("Nichts zu tun - keine Variante erfuellt den Filter.")
        return 0

    print(f"\n{len(variants)} Variante(n), {len(comps)} Mitbewerber.")
    _run_dialog(conn, variants, comps, listings, cands, args.competitor)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
