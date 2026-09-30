"""Downloads the full price files of the chosen branches and writes data/prices.json.

Run nightly. Output shape:
  {"updated": "...", "stores": [{"chain","branch"}...],
   "items": {"<barcode>": ["<name>", "<maker>", [price per store or null]]}}
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


def shufersal(store_id):
    op = urllib.request.build_opener()
    page = get(op, f"https://prices.shufersal.co.il/FileObject/UpdateCategory?catID=2&storeId={store_id}").decode()
    urls = [html.unescape(u) for u in re.findall(r'https://[^"]*?PriceFull[^"]*?\.gz[^"]*', page)]
    if not urls:
        raise RuntimeError(f"Shufersal {store_id}: no PriceFull file")
    # newest file = latest timestamp in the name
    urls.sort(key=lambda u: re.search(r"PriceFull[\d-]+", u).group(0))
    return decode(get(op, urls[-1]))


def published_prices(user, chain_id, store_id):
    """url.publishedprices.co.il — the chains' public portal (public user, empty password)."""
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = "https://url.publishedprices.co.il"
    token = lambda p: re.search(r'name="csrftoken" content="([^"]+)"', p).group(1)
    t = token(get(op, base + "/login").decode())
    get(op, base + "/login/user", urllib.parse.urlencode({"username": user, "password": "", "csrftoken": t}).encode())
    t = token(get(op, base + "/file").decode())
    listing = json.loads(get(op, base + "/file/json/dir", urllib.parse.urlencode(
        {"path": "/", "csrftoken": t, "iDisplayLength": "100000", "sSearch": f"PriceFull{chain_id}"}).encode()))
    names = sorted(f["name"] for f in listing["aaData"] if re.search(rf"PriceFull{chain_id}-\d+-{store_id}-", f["name"]))
    if not names:
        raise RuntimeError(f"{user} {store_id}: no PriceFull file")
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


STORES = [
    {"chain": "שופרסל", "branch": "שלי פרדסיה", "fetch": lambda: shufersal(102)},
    {"chain": "אושר עד", "branch": "נתניה – קריית השרון", "fetch": lambda: published_prices("osherad", "7290103152017", "023")},
]


def main():
    per_store = []
    for s in STORES:
        items = parse(s["fetch"]())
        print(f"{s['chain']} {s['branch']}: {len(items)} items")
        if len(items) < 1000:
            raise RuntimeError("suspiciously few items — not overwriting data")
        per_store.append(items)

    items = {}
    for code in set().union(*per_store):
        found = [st.get(code) for st in per_store]
        first = next(x for x in found if x)
        items[code] = [
            first["name"], first["maker"],
            [x["price"] if x else None for x in found],
        ]

    israel = timezone(timedelta(hours=3))
    data = {
        "updated": datetime.now(israel).strftime("%d.%m.%Y %H:%M"),
        "stores": [{"chain": s["chain"], "branch": s["branch"]} for s in STORES],
        "items": items,
    }
    out = Path(__file__).parent / "data" / "prices.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    both = sum(1 for v in items.values() if all(p is not None for p in v[2]))
    print(f"total {len(items)} barcodes, {both} in both stores, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
