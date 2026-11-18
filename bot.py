#!/usr/bin/env python3
"""
Bot cerca-offerte per attrezzatura da montagna.

Ogni volta che viene eseguito:
  1. (ogni N ore) cerca sui negozi le pagine prodotto dei modelli in config.yaml
  2. apre le pagine prodotto e legge prezzo + disponibilita' per taglia
     (dati strutturati schema.org che i negozi espongono per Google)
  3. manda un messaggio Telegram se trova un'offerta sotto la soglia
  4. avvisa se un negozio smette di funzionare, e una volta al giorno
     manda un riepilogo con i migliori prezzi attuali.

Uso:
  python bot.py                        controllo normale
  python bot.py --dry-run              stampa i messaggi invece di mandarli
  python bot.py --mode test-telegram   manda solo un messaggio di prova
  python bot.py --mode riscopri        forza la ricerca di nuovi prodotti
  python bot.py --mode riepilogo       manda subito il riepilogo
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml
from curl_cffi import requests

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Rome")
SEARCH_PATH_WORDS = {"search", "ricerca", "catalogsearch", "suche", "busqueda", "recherche", "pesquisa"}
BLOCK_MARKERS = ("cf-chl", "challenge-platform", "just a moment...", "client challenge", "captcha", "access denied")


def log(*a):
    print(*a, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- testo

def tokens(s: str) -> set[str]:
    s = (s or "").lower().replace("ø", "o").replace("æ", "ae").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return set(re.findall(r"[a-z0-9]+", s))


def norm_size(s: str) -> str:
    s = str(s).upper()
    s = re.sub(r"\(.*?\)", "", s)
    s = s.replace("½", ".5").replace("1/2", ".5").replace(",", ".")
    s = re.sub(r"\b(EU|EUR|TAGLIA|SIZE|GR\.?|TG\.?|LUNGHEZZA)\b", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip(".-:")
    return re.sub(r"^(\d+(?:\.\d+)?)(?:M|MT|METRI|METER|METERS|METRES)$", r"\1M", s)


def diameters(name: str) -> list[float]:
    """Diametri plausibili di una corda nel nome, es. 'Joker 9.1', '8,6 mm'."""
    out = []
    for m in re.finditer(r"(?<![\d.,])(\d{1,2}[.,]\d{1,2})\s*(mm)?(?![\d])", name):
        v = float(m.group(1).replace(",", "."))
        if 6.5 <= v <= 11.5:
            out.append(v)
    return out


def euro(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def domain(url_or_host: str) -> str:
    h = urlsplit(url_or_host).netloc if "//" in url_or_host else url_or_host
    return h.lower().removeprefix("www.")


# --------------------------------------------------------------------------- HTTP

class Fetcher:
    def __init__(self, delay: float):
        self.delay = delay
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({"Accept-Language": "it-IT,it;q=0.9,en;q=0.7"})
        self._first = True

    def get(self, url: str) -> tuple[int, str, str]:
        """Ritorna (status, testo, url_finale). status 0 = errore di rete, -1 = bloccato."""
        if not self._first:
            time.sleep(self.delay)
        self._first = False
        last_err = None
        for attempt in range(2):
            try:
                r = self.session.get(url, timeout=30)
                text = r.text
                low = text[:20000].lower()
                if r.status_code in (403, 429) or (
                    len(text) < 30000 and any(m in low for m in BLOCK_MARKERS)
                ):
                    return -1, text, str(r.url)
                if r.status_code >= 500 and attempt == 0:
                    time.sleep(5)
                    continue
                return r.status_code, text, str(r.url)
            except Exception as e:  # timeout, DNS, TLS...
                last_err = e
                time.sleep(5)
        log(f"   ! errore rete {url}: {last_err!r}"[:200])
        return 0, "", url


# --------------------------------------------------------------------------- lettura pagine prodotto

LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
# Nencini Sport: taglie nell'HTML con la quantita' disponibile
NENCINI_SIZE_RE = re.compile(r'data-disponibilita="(\d+)"[^>]*?aria-label="[^"]*?taglia\s+([^"]+?)\s*"', re.S)


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _type(o) -> str:
    t = o.get("@type", "")
    if isinstance(t, list):
        t = " ".join(map(str, t))
    return str(t)


def _price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("value") or "")
    if isinstance(v, list):
        return ", ".join(_text(x) for x in v)
    return str(v or "")


def extract_variants(page: str, page_url: str) -> list[dict]:
    objs = []
    for m in LD_RE.finditer(page):
        raw = m.group(1).strip()
        for candidate in (raw, html.unescape(raw)):
            try:
                objs.extend(_walk(json.loads(candidate, strict=False)))
                break
            except json.JSONDecodeError:
                continue
    group = next((o.get("name") for o in objs if "ProductGroup" in _type(o) and o.get("name")), "")
    variants = []
    for p in objs:
        t = _type(p)
        if "Product" not in t or "ProductGroup" in t:
            continue
        pname = str(p.get("name") or group or "")
        offers = p.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if offers else []
        for of in offers:
            if not isinstance(of, dict):
                continue
            spec = of.get("priceSpecification")
            spec = spec[0] if isinstance(spec, list) and spec else spec
            spec = spec if isinstance(spec, dict) else {}
            price = _price(of.get("price") or of.get("lowPrice") or spec.get("price"))
            if price is None or price <= 0:
                continue
            oname = str(of.get("name") or "")
            name = pname
            if oname and oname != pname:
                name = oname if pname and pname in oname else f"{pname} - {oname}" if oname not in pname else pname
            avail = str(of.get("availability") or "")
            variants.append({
                "name": name,
                "group": str(group or pname),
                "size": _text(p.get("size")),
                "color": _text(p.get("color")),
                "sku": str(of.get("sku") or p.get("sku") or ""),
                "price": price,
                "currency": str(of.get("priceCurrency") or spec.get("priceCurrency") or "EUR").upper(),
                "available": (not avail) or any(k in avail for k in ("InStock", "LimitedAvailability", "OnlineOnly")),
                "avail_unknown": not avail,
                "url": html.unescape(str(of.get("url") or p.get("url") or page_url)),
            })

    # pagina con un solo prezzo ma taglie elencate nell'HTML (es. Nencini)
    if len(variants) == 1 and not variants[0]["size"]:
        sizes = NENCINI_SIZE_RE.findall(page)
        if sizes:
            base = variants.pop()
            for qty, size in sizes:
                variants.append({**base, "size": size, "name": f"{base['name']} - {size}",
                                 "sku": f"{base['sku']}-{size}", "available": int(qty) > 0, "avail_unknown": False})
    return variants


SPLIT_RE = re.compile(r"[/|]|, | - | – ")


def size_shape(s: str) -> str:
    """Tipo di taglia: lunghezza corda, numero scarpa, lettera."""
    if re.fullmatch(r"\d{2,3}M", s):
        return "corda"
    if re.fullmatch(r"\d{2}(\.5|\.33|\.66)?", s):
        return "scarpa"
    if re.fullmatch(r"X{0,3}[SML]|XXL|XXXL", s):
        return "lettera"
    return ""


def variant_sizes(v: dict) -> set[str]:
    out = set()
    if v["size"]:
        out.add(norm_size(v["size"]))
    name = v["name"]
    if v["group"] and name.startswith(v["group"]):
        name = name[len(v["group"]):]
    for part in SPLIT_RE.split(name):
        n = norm_size(part)
        if n:
            out.add(n)
    m = re.search(r"(?:taglia|size|tg\.?)\s*([0-9][0-9.,½ ]*|[XSML]{1,4})\b", v["name"], re.I)
    if m:
        out.add(norm_size(m.group(1)))
    for m in re.finditer(r"(?<![\d.,])(\d{2,3})\s*(?:m|mt|metri)\b", v["name"], re.I):  # corde: 60 m
        out.add(f"{m.group(1)}M")
    out.discard("")
    return out


def variant_label(v: dict) -> str:
    label = v["name"].replace(v["group"], "") if v["group"] else v["name"]
    label = re.sub(r"\s+", " ", label).strip(" -|·,")
    if v["size"] and norm_size(v["size"]) not in {norm_size(x) for x in SPLIT_RE.split(label)}:
        label = f"{label} · {v['size']}" if label else v["size"]
    return label or v["name"]


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    cfg.setdefault("impostazioni", {})
    always_excl = {str(x).lower() for x in cfg.get("escludi_sempre", [])}
    for s in cfg["negozi"]:
        s["dominio"] = domain(s.get("sito") or s["ricerca"])
    for it in cfg["articoli"]:
        it["taglie_norm"] = {norm_size(t) for t in it["taglie"]}
        it["escludi_tok"] = always_excl | {str(x).lower() for x in it.get("escludi", [])}
        for mod in it["modelli"]:
            mod["parole_tok"] = {str(x).lower() for x in mod.get("parole") or tokens(mod["cerca"])}
            mod["filtro_tok"] = {str(x).lower() for x in mod.get("filtro_link") or tokens(mod["cerca"])}
            mod["escludi_tok"] = it["escludi_tok"] | {str(x).lower() for x in mod.get("escludi", [])}
    return cfg


def config_hash(cfg: dict) -> str:
    key = json.dumps({"n": [(s["nome"], s.get("ricerca")) for s in cfg["negozi"]],
                      "a": [(i["id"], [m["cerca"] for m in i["modelli"]], i.get("link_extra")) for i in cfg["articoli"]]},
                     sort_keys=True, default=str)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def shop_for_url(cfg: dict, url: str) -> dict:
    d = domain(url)
    for s in cfg["negozi"]:
        if d == s["dominio"] or d.split(".")[-2:-1] == s["dominio"].split(".")[-2:-1]:
            return s
    return {"nome": d, "dominio": d}


# --------------------------------------------------------------------------- scoperta prodotti

def find_product_links(page: str, final_url: str, filtro: set[str]) -> list[str]:
    host = urlsplit(final_url).netloc
    raw = re.findall(r'href=["\']([^"\']+)["\']', page) + re.findall(r'"url"\s*:\s*"(https?:[^"]+)"', page)
    out = []
    for h in raw:
        u = urljoin(final_url, html.unescape(h).replace("\\/", "/"))
        sp = urlsplit(u)
        if sp.netloc != host:
            continue
        ptok = tokens(sp.path)
        if not filtro <= ptok or ptok & SEARCH_PATH_WORDS or re.search(r"\.(jpe?g|png|webp|gif|svg)$", sp.path, re.I):
            continue
        clean = urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
        if clean not in out:
            out.append(clean)
    return out[:12]


def discover(cfg: dict, health: dict) -> dict[str, list[str]]:
    delay = cfg["impostazioni"].get("pausa_tra_richieste_secondi", 2)
    found: dict[str, list[str]] = {it["id"]: list(it.get("link_extra") or []) for it in cfg["articoli"]}
    shops = [s for s in cfg["negozi"] if s.get("ricerca")]

    def per_shop(shop):
        f = Fetcher(delay)
        res, ok, tot = [], 0, 0
        for it in cfg["articoli"]:
            for mod in it["modelli"]:
                url = shop["ricerca"].format(q=quote_plus(mod["cerca"]))
                status, page, final = f.get(url)
                tot += 1
                if status != 200:
                    log(f"   ricerca {shop['nome']} '{mod['cerca']}': HTTP {status}")
                    continue
                ok += 1
                links = find_product_links(page, final, mod["filtro_tok"])
                res += [(it["id"], u) for u in links]
        log(f"   ricerca su {shop['nome']:20} {ok}/{tot} ok, {len(res)} pagine candidate")
        return shop["nome"], res, ok, tot

    with ThreadPoolExecutor(max_workers=len(shops)) as ex:
        for name, res, ok, tot in ex.map(per_shop, shops):
            h = health.setdefault(name, {"ok": 0, "tot": 0})
            h["ok"] += ok
            h["tot"] += tot
            for item_id, u in res:
                if u not in found[item_id]:
                    found[item_id].append(u)
    return found


# --------------------------------------------------------------------------- controllo prezzi

def match_model(item: dict, v: dict):
    name_tok = tokens(v["name"]) | tokens(v["group"])
    for mod in item["modelli"]:
        if mod["parole_tok"] <= name_tok and not (mod["escludi_tok"] & name_tok):
            if "diametro" in item:
                lo, hi = item["diametro"]
                if not any(lo <= d <= hi for d in diameters(v["group"] + " " + v["name"])):
                    continue
            return mod
    return None


def check_products(cfg: dict, products: dict[str, list[str]], health: dict) -> tuple[list[dict], dict]:
    delay = cfg["impostazioni"].get("pausa_tra_richieste_secondi", 2)
    url_items: dict[str, list[str]] = {}
    for item_id, urls in products.items():
        for u in urls:
            url_items.setdefault(u, []).append(item_id)
    by_shop: dict[str, tuple[dict, list[str]]] = {}
    for u in url_items:
        s = shop_for_url(cfg, u)
        by_shop.setdefault(s["nome"], (s, []))[1].append(u)
    items = {it["id"]: it for it in cfg["articoli"]}
    keep: set[tuple[str, str]] = set()  # (articolo, url) pagine pertinenti o non lette (da ricontrollare)

    def per_shop(arg):
        shop, urls = arg
        f = Fetcher(delay)
        out, ok, parsed, kept = [], 0, 0, []
        for u in urls:
            status, page, final = f.get(u)
            if status == 404 or status == 410:
                continue  # prodotto rimosso
            if status != 200:
                kept += [(i, u) for i in url_items[u]]
                continue
            ok += 1
            variants = extract_variants(page, final)
            if variants:
                parsed += 1
            for item_id in url_items[u]:
                it = items[item_id]
                for v in variants:
                    mod = match_model(it, v)
                    if not mod:
                        continue
                    kept.append((item_id, u))
                    sizes = variant_sizes(v)
                    size_unknown = False
                    if not (sizes & it["taglie_norm"]):
                        if not shop.get("taglie_non_leggibili"):
                            continue
                        # se il nome indica chiaramente un'altra taglia (es. corda da 50 m), scarta
                        wanted_shapes = {size_shape(t) for t in it["taglie_norm"]} - {""}
                        if any(size_shape(s) in wanted_shapes for s in sizes):
                            continue
                        size_unknown = True
                    out.append({**v, "item": item_id, "shop": shop["nome"], "page": u,
                                "size_unknown": size_unknown})
        return shop["nome"], out, ok, len(urls), parsed, kept

    offers = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_shop))) as ex:
        for name, out, ok, tot, parsed, kept in ex.map(per_shop, by_shop.values()):
            h = health.setdefault(name, {"ok": 0, "tot": 0})
            h["ok"] += ok
            h["tot"] += tot
            h["parsed"] = h.get("parsed", 0) + parsed
            h["pages"] = h.get("pages", 0) + tot
            keep.update(kept)
            offers += out
            log(f"   {name:20} {ok}/{tot} pagine lette, {parsed} con prezzi, {len(out)} varianti nella vostra taglia")
    # tieni solo le pagine utili: le altre (accessori, modelli diversi) si ritrovano alla prossima ricerca
    pruned = {iid: [u for u in urls if (iid, u) in keep or u in (items[iid].get("link_extra") or [])]
              for iid, urls in products.items()}
    return offers, pruned


# --------------------------------------------------------------------------- Telegram

class Notifier:
    def __init__(self, dry: bool):
        self.dry = dry
        self.token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.sent = 0
        if not dry and not (self.token and self.chat):
            log("!! TELEGRAM_TOKEN / TELEGRAM_CHAT_ID mancanti: stampo i messaggi invece di inviarli")
            self.dry = True

    def send(self, text: str):
        self.sent += 1
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        for c in chunks:
            if self.dry:
                log("\n----- MESSAGGIO TELEGRAM -----\n" + html.unescape(re.sub(r"<[^>]+>", "", c)).rstrip()
                    + "\n------------------------------")
                continue
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={
                "chat_id": self.chat, "text": c, "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=30)
            if r.status_code != 200:
                log(f"!! Telegram ha risposto {r.status_code}: {r.text[:300]}")
                raise SystemExit(1)
            time.sleep(1)


# --------------------------------------------------------------------------- logica avvisi

def offer_key(o: dict) -> str:
    return f"{o['item']}|{o['shop']}|{o['name']}|{o['size']}|{o['color']}|{o['sku']}"


def is_deal(o: dict, item: dict) -> bool:
    return o["available"] and o["currency"] == "EUR" and o["price"] <= item["prezzo_max"]


def deal_messages(cfg, offers, state) -> list[str]:
    items = {it["id"]: it for it in cfg["articoli"]}
    seen = state.setdefault("seen", {})
    to_notify: dict[tuple, list[dict]] = {}
    current = set()
    for o in offers:
        k = offer_key(o)
        if k in current:
            continue
        current.add(k)
        it = items[o["item"]]
        rec = seen.setdefault(k, {"notified": None})
        rec.update(price=o["price"], available=o["available"], last=now_utc().isoformat())
        if is_deal(o, it):
            prev = rec.get("notified")
            if prev is None or o["price"] < prev - 0.5:
                rec["notified"] = o["price"]
                o["prev"] = prev
                to_notify.setdefault((o["item"], o["shop"], o["page"]), []).append(o)
        else:
            rec["notified"] = None  # se torna in offerta, riavvisiamo
    cutoff = (now_utc() - timedelta(days=3)).isoformat()
    for k in [k for k, r in seen.items() if k not in current and r.get("last", "") < cutoff]:
        del seen[k]

    # un messaggio per articolo, una riga per pagina prodotto (la variante piu' economica + le altre)
    per_item: dict[str, list[list[dict]]] = {}
    for (item_id, _shop, _page), group in to_notify.items():
        per_item.setdefault(item_id, []).append(sorted(group, key=lambda o: o["price"]))
    msgs = []
    max_lines = 10
    for item_id, groups in per_item.items():
        it = items[item_id]
        groups.sort(key=lambda g: g[0]["price"])
        top = it.get("prezzo_top")
        fire = "🔥🔥" if top and groups[0][0]["price"] <= top else "🔥"
        n = len(groups)
        lines = [f"{fire} <b>{esc(it['nome'])}</b> — {n} {'nuova offerta' if n == 1 else 'nuove offerte'} "
                 f"(soglia {euro(it['prezzo_max'])})", ""]
        for g in groups[:max_lines]:
            o = g[0]
            hot = "🔥🔥 " if top and o["price"] <= top else ""
            extra = f" <s>{euro(o['prev'])}</s>" if o.get("prev") else ""
            variants = ", ".join(dict.fromkeys(variant_label(x) for x in g[:4]))
            if len(g) > 4:
                variants += f" +{len(g) - 4}"
            warn = ""
            if o["size_unknown"]:
                warn = "\n   ⚠️ taglia da verificare sul sito"
            elif o["avail_unknown"]:
                warn = "\n   ⚠️ disponibilità da verificare sul sito"
            title = o["group"] or o["name"]
            var_line = "" if variants.strip() == title.strip() else f'\n   <i>{esc(variants[:120])}</i>'
            lines.append(f'{hot}<b>{euro(o["price"])}</b>{extra} · {esc(o["shop"])}\n'
                         f'   <a href="{esc(o["url"])}">{esc(title[:80])}</a>{var_line}{warn}')
        if n > max_lines:
            lines.append(f"\n…e altre {n - max_lines} offerte più care (le vedete nel riepilogo).")
        msgs.append("\n".join(lines))
    return msgs


def health_messages(cfg, health, state) -> list[str]:
    limit = cfg["impostazioni"].get("avvisa_se_negozio_non_funziona_dopo", 3)
    hs = state.setdefault("health", {})
    msgs = []
    for shop, h in health.items():
        broken = h["tot"] > 0 and (h["ok"] == 0 or (h.get("pages", 0) >= 3 and h.get("parsed", 0) == 0))
        rec = hs.setdefault(shop, {"fails": 0, "alerted": False})
        if broken:
            rec["fails"] += 1
            if rec["fails"] >= limit and not rec["alerted"]:
                rec["alerted"] = True
                why = "il sito blocca il bot o non risponde" if h["ok"] == 0 else "le pagine si aprono ma non riesco a leggere i prezzi"
                msgs.append(f"⚠️ <b>{esc(shop)}</b> non funziona da {rec['fails']} controlli di fila ({why}).\n"
                            f"Su questo negozio al momento sono cieco: controllatelo a mano finché non vi dico che è tornato ok.")
        else:
            if rec["alerted"]:
                msgs.append(f"✅ <b>{esc(shop)}</b> è tornato a funzionare.")
            rec.update(fails=0, alerted=False)
    return msgs


def summary_message(cfg, offers, products, health) -> str:
    lines = ["☀️ <b>Riepilogo giornaliero</b> — bot attivo ✅", ""]
    for it in cfg["articoli"]:
        avail = [o for o in offers if o["item"] == it["id"] and o["available"] and o["currency"] == "EUR"]
        lines.append(f"<b>{esc(it['nome'])}</b> (taglia {esc('/'.join(map(str, it['taglie'])))}, soglia {euro(it['prezzo_max'])})")
        if avail:
            shown = []
            for o in sorted(avail, key=lambda o: o["price"]):
                if (o["shop"], o["group"]) in shown:
                    continue
                shown.append((o["shop"], o["group"]))
                mark = "🔥" if o["price"] <= it["prezzo_max"] else "·"
                warn = " (taglia da verificare)" if o["size_unknown"] else ""
                lines.append(f' {mark} {euro(o["price"])} — <a href="{esc(o["url"])}">{esc(o["group"][:55])}</a> '
                             f'({esc(o["shop"])}){warn}')
                if len(shown) >= 5:
                    break
        else:
            n = len(products.get(it["id"], []))
            lines.append(f" nessuna disponibilità nella vostra taglia ({n} pagine controllate)")
        lines.append("")
    ok = sorted(s for s, h in health.items() if h["ok"] > 0)
    ko = sorted(s for s, h in health.items() if h["ok"] == 0)
    lines.append(f"Negozi ok: {esc(', '.join(ok) or '-')}")
    if ko:
        lines.append(f"⚠️ Negozi con problemi: {esc(', '.join(ko))}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--state", default=str(ROOT / "state.json"))
    ap.add_argument("--dry-run", action="store_true", help="stampa i messaggi senza inviarli")
    ap.add_argument("--mode", default="normale", choices=["normale", "test-telegram", "riscopri", "riepilogo"])
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    notifier = Notifier(args.dry_run)

    if args.mode == "test-telegram":
        notifier.send("✅ Il bot delle offerte è collegato a questa chat!")
        log("Messaggio di prova inviato.")
        return

    state_path = Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {}
    first_run = not state
    health: dict = {}
    t0 = time.time()

    # 1) scoperta prodotti
    every = cfg["impostazioni"].get("ogni_quante_ore_cerco_nuovi_prodotti", 6)
    last = state.get("discovered_at")
    if (args.mode == "riscopri" or not last or state.get("config_hash") != config_hash(cfg)
            or now_utc() - datetime.fromisoformat(last) > timedelta(hours=every)):
        log("== Cerco le pagine prodotto sui negozi...")
        state.update(products=discover(cfg, health), discovered_at=now_utc().isoformat(),
                     config_hash=config_hash(cfg))
    products = state.get("products", {})
    for it in cfg["articoli"]:
        log(f"   {it['nome']}: {len(products.get(it['id'], []))} pagine da controllare")

    # 2) controllo prezzi
    log("== Controllo prezzi e disponibilità...")
    offers, state["products"] = check_products(cfg, products, health)

    # 3) messaggi
    msgs = []
    if first_run:
        msgs.append(f"🤖 <b>Bot offerte avviato!</b>\nControllo {len(cfg['articoli'])} articoli su "
                    f"{len(cfg['negozi'])} negozi. Vi scrivo quando trovo qualcosa sotto soglia nella vostra "
                    f"taglia, e ogni mattina vi mando un riepilogo dei prezzi migliori.")
    msgs += deal_messages(cfg, offers, state)
    msgs += health_messages(cfg, health, state)

    today = datetime.now(TZ)
    hour = cfg["impostazioni"].get("ora_riepilogo_giornaliero", 9)
    if args.mode == "riepilogo" or first_run or (
        today.hour >= hour and state.get("last_summary") != today.date().isoformat()
    ):
        msgs.append(summary_message(cfg, offers, state["products"], health))
        state["last_summary"] = today.date().isoformat()

    for m in msgs:
        notifier.send(m)

    state["last_run"] = now_utc().isoformat()
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"== Fatto in {time.time() - t0:.0f}s: {len(offers)} varianti nella vostra taglia, {notifier.sent} messaggi.")


if __name__ == "__main__":
    main()
