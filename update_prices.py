"""Downloads the full price files of the chosen branches and writes data/prices.json
(+ data/first_seen.json and data/new.json for the "new in store" page).

Run nightly. Output shape:
  {"updated": "...", "stores": [{"chain","branch"}...],
   "items": {"<barcode>": ["<name>", "<maker>", [price per store or null], [promo per store or null]]}}
  promo = [min qty, total price, "end dd.mm", description, 1 if club-only else 0]
"""
import gzip, html, http.cookiejar, json, re, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}


def get(opener, url, data=None):
    req = urllib.request.Request(url, data=data, headers=UA)
    return opener.open(req, timeout=120).read()


def decode(raw):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("unknown encoding")


def shufersal(store_id, kind):
    op = urllib.request.build_opener()
    cat = {"PriceFull": 2, "PromoFull": 4}[kind]
    page = get(op, f"https://prices.shufersal.co.il/FileObject/UpdateCategory?catID={cat}&storeId={store_id}").decode()
    urls = [html.unescape(u) for u in re.findall(rf'https://[^"]*?{kind}[^"]*?\.gz[^"]*', page)]
    if not urls:
        raise RuntimeError(f"Shufersal {store_id}: no {kind} file")
    # newest file = latest timestamp in the name
    urls.sort(key=lambda u: re.search(rf"{kind}[\d-]+", u).group(0))
    return decode(get(op, urls[-1]))


def published_prices(user, chain_id, store_id, kind):
    """url.publishedprices.co.il — the chains' public portal (public user, empty password)."""
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = "https://url.publishedprices.co.il"
    token = lambda p: re.search(r'name="csrftoken" content="([^"]+)"', p).group(1)
    t = token(get(op, base + "/login").decode())
    get(op, base + "/login/user", urllib.parse.urlencode({"username": user, "password": "", "csrftoken": t}).encode())
    t = token(get(op, base + "/file").decode())
    listing = json.loads(get(op, base + "/file/json/dir", urllib.parse.urlencode(
        {"path": "/", "csrftoken": t, "iDisplayLength": "100000", "sSearch": f"{kind}{chain_id}"}).encode()))
    names = sorted(f["name"] for f in listing["aaData"] if re.search(rf"{kind}{chain_id}-\d+-{store_id}-", f["name"]))
    if not names:
        raise RuntimeError(f"{user} {store_id}: no {kind} file")
    return decode(get(op, base + "/file/d/" + names[-1]))


def parse(xml_text):
    root = ET.fromstring(xml_text)
    out = {}
    for it in root.iter():
        if it.tag.lower() != "item":
            continue
        f = {c.tag.lower(): (c.text or "").strip() for c in it}
        code = f.get("itemcode", "").lstrip("0")
        if not code or not f.get("itemprice"):
            continue
        out[code] = {
            "name": f.get("itemname", ""),
            "maker": f.get("manufacturername") or f.get("manufacturename", ""),
            "price": float(f["itemprice"]),
        }
    return out


def parse_promos(xml_text, prices, now):
    """Best current promo per barcode. Skips coupons, gifts and anything not cheaper than the shelf price."""
    root = ET.fromstring(xml_text)
    best = {}
    for p in root.iter("Promotion"):
        g = lambda t: (p.findtext(t) or "").strip()
        if g("AdditionalIsCoupon") == "1":
            continue
        start, end = g("PromotionStartDateTime")[:19], g("PromotionEndDateTime")[:19]
        if not end or end < now or (start and start > now):
            continue
        club = 0 if g("ClubID").split(" ")[0] in ("", "0") else 1
        for it in p.iter("PromotionItem"):
            if (it.findtext("RewardType") or "").strip() not in ("1", "3", "10"):
                continue
            code = (it.findtext("ItemCode") or "").strip().lstrip("0")
            try:
                qty = float(it.findtext("MinQty") or 1) or 1
                total = float(it.findtext("DiscountedPrice") or 0)
            except ValueError:
                continue
            shelf = prices.get(code, {}).get("price")
            if not shelf or total <= 0 or total / qty >= shelf:
                continue
            if code in best and best[code][1] / best[code][0] <= total / qty:
                continue
            best[code] = [int(qty) if qty == int(qty) else qty, total,
                          f"{end[8:10]}.{end[5:7]}", g("PromotionDescription"), club]
    return best


STORES = [
    {"chain": "שופרסל", "branch": "שלי פרדסיה", "fetch": lambda kind: shufersal(102, kind)},
    {"chain": "אושר עד", "branch": "נתניה – קריית השרון", "fetch": lambda kind: published_prices("osherad", "7290103152017", "023", kind)},
]


NEW_DAYS = 14  # how long a product counts as "new"


def store_key(s):
    return f"{s['chain']}|{s['branch']}"


def main():
    israel = timezone(timedelta(hours=3))
    now_dt = datetime.now(israel)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    today = now_dt.strftime("%Y-%m-%d")
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    prices_path = data_dir / "prices.json"
    old = json.loads(prices_path.read_text(encoding="utf-8")) if prices_path.exists() else None
    old_keys = [store_key(s) for s in old["stores"]] if old else []

    per_store, promos, fresh = [], [], []
    for s in STORES:
        try:
            items = parse(s["fetch"]("PriceFull"))
            if len(items) < 1000:
                raise RuntimeError(f"only {len(items)} items")
        except Exception as e:
            # keep the last good prices of this store rather than losing it (or the other store's update)
            if store_key(s) not in old_keys:
                raise
            i = old_keys.index(store_key(s))
            print(f"{s['chain']} {s['branch']}: download failed ({e}) — keeping previous prices")
            per_store.append({c: {"name": v[0], "maker": v[1], "price": v[2][i]} for c, v in old["items"].items() if v[2][i] is not None})
            promos.append({c: v[3][i] for c, v in old["items"].items() if len(v) > 3 and v[3][i]})
            fresh.append(False)
            continue
        try:
            pr = parse_promos(s["fetch"]("PromoFull"), items, now)
        except Exception as e:  # promos are a bonus; never lose the prices over them
            print(f"  promos failed: {e}")
            pr = {}
        print(f"{s['chain']} {s['branch']}: {len(items)} items, {len(pr)} on promo")
        per_store.append(items)
        promos.append(pr)
        fresh.append(True)
    if not any(fresh):
        raise RuntimeError("no store downloaded — nothing to update")

    items = {}
    for code in set().union(*per_store):
        found = [st.get(code) for st in per_store]
        first = next(x for x in found if x)
        items[code] = [
            first["name"], first["maker"],
            [x["price"] if x else None for x in found],
            [pr.get(code) for pr in promos],
        ]
    data = {
        "updated": now_dt.strftime("%d.%m.%Y %H:%M"),
        "stores": [{"chain": s["chain"], "branch": s["branch"]} for s in STORES],
        "items": items,
    }
    prices_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    both = sum(1 for v in items.values() if all(p is not None for p in v[2]))
    print(f"total {len(items)} barcodes, {both} in both stores, {prices_path.stat().st_size // 1024} KB")

    # first-seen date per store. The first run of a store is the baseline ("0" = was already there).
    # Dates are never removed, so a product that disappears for a while and returns is not "new" again.
    seen_path = data_dir / "first_seen.json"
    seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    cutoff = (now_dt - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d")
    new = {}
    for s, st, ok in zip(STORES, per_store, fresh):
        k = store_key(s)
        baseline = k not in seen
        if ok:  # the baseline must come from a real download, never from kept-over prices
            known = seen.setdefault(k, {})
            for code in st:
                known.setdefault(code, "0" if baseline else today)
        known = seen.get(k, {})
        # [barcode, name, price, first seen]; only products the store still sells
        new[k] = sorted(([c, st[c]["name"], st[c]["price"], d] for c, d in known.items() if d != "0" and d >= cutoff and c in st),
                        key=lambda x: (x[3], x[2]), reverse=True)
        print(f"  {k}: {len(new[k])} new in the last {NEW_DAYS} days" + (" (baseline today)" if baseline and ok else " (no baseline yet)" if baseline else ""))
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (data_dir / "new.json").write_text(json.dumps({"updated": data["updated"], "days": NEW_DAYS, "stores": new},
                                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
