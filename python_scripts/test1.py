import argparse, json, re, time
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8,fr-CA;q=0.8,fr;q=0.7,ja;q=0.6",
    "Upgrade-Insecure-Requests": "1",    
}

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or"").strip()

def fetch_html(url: str, timeout : int = 25) -> str:
    with requests.Session() as s:
        s.headers.update(HEADERS)
        try:
            p = urlparse(url); s.get(f"{p.scheme}://{p.netloc}/", timeout=timeout)
        except Exception:
            pass
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        if not r.encoding:
            r.encoding = r.apparent_encoding
        return r.text
    
def nearest_heading_and_desc(table) -> tuple[str, str]:
    name, desc = "", ""

    cand = table.find_previous("div", class_=lambda cs: cs and "description" in cs)
    while cand is not None:
        next_tbl = cand.find_next("table")
        if next_tbl is table:
            desc = clean(cand.get_text(" "))
        cand = cand.find_previous("div", class_=lambda cs: cs and "description" in cs)

    prev = table
    while True:
        prev = prev.find_previous(["h1", "h2", "h3", "h4", "h5", "p", "div"])
        if prev is None:
            break
        tag = (prev.name or "").lower()
        if tag in {"h1", "h2", "h3", "h4", "h5"}:
            name = clean(prev.get_text(" "))

            if not desc:
                texts = []
                for sib in prev.next_siblings:
                    if sib is table:
                        break
                    if getattr(sib, "name", None) == "p":
                        texts.append(clean(sib.get_text(" ")))
                    desc = clean(" ".join(t for t in texts if t))
                break

    return name, desc

def strip_label_prefix(value: str, label_candidates: list[str]) -> str:
    if not value:
        return value
    s = clean(value)
    for lab in label_candidates:
        if not lab:
            continue

        lab_clean = clean(lab).rstrip(":")
        pattern = re.compile(rf"^(?:{re.escape(lab_clean)})\s*:\s*", re.IGNORECASE)
        new_s = re.sub(pattern, "", s, count=1)
        if new_s != s:
            return new_s.strip()
        
    return re.sub(r"^\s*description\s*:\s*", "", s, flags=re.IGNORECASE).strip()



def guess_columns(header_cells: list[str]) -> dict:
    cols = { "description": None, "price": None}
    for i, h in enumerate(header_cells):
        hl = h.lower()
        if cols["description"] is None and any(k in hl for k in ["description", "desc"]):
            cols["description"] = i
        if cols["price"] is None and "price" in hl:
            cols["price"] = i
    return cols

def extract_price(text: str) -> str | None:
    m = re.search(r"([€£$])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if not m:
        return None
    return m.group(2).replace(",", "")

def parse_groups_from_html(html: str, base_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    for table in soup.find_all("table"):
        headers = [clean(th.get_text(" ")) for th in table.find_all("th")]
        if not headers:
            continue
        if not any("price" in h.lower() for h in headers):
            continue

        colmap = guess_columns(headers)

        group_name, group_desc = nearest_heading_and_desc(table)
        subproducts = []

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            if colmap["description"] is not None and colmap["description"] < len(tds):
                name = clean(tds[colmap["description"]].get_text(" "))
                desc_header = headers[colmap["description"]] if colmap["description"] is not None else None
                name = strip_label_prefix(name, [desc_header, "Description", "Desc"])
            else:
                name = clean(tds[min(1, len(tds)-1)].get_text(" "))
                name = strip_label_prefix(name, ["Description", "Desc"])

            price_text = ""
            if colmap["price"] is not None and colmap["price"] < len(tds):
                price_text = clean(tds[colmap["price"]].get_text(" "))
            else:
                price_text = clean(tds[-1].get_text(" "))
            price = extract_price(price_text)

            if not name or price is None:
                continue

            subproducts.append({"name": name, "price": price})

        if subproducts:
            groups.append({
                "name": group_name or "",
                "desc": group_desc or "",
                "subproducts": subproducts
            })
        
    return groups

def main():
    ap = argparse.ArgumentParser(description="製品一覧の“表”から {name, desc, subproducts{name, price}} をJSON化")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="対象ページのURL（403になる場合は --file を使う）")
    src.add_argument("--file", help="保存したHTMLファイルのパス（ブラウザで『ページを保存』）")
    ap.add_argument("-o", "--out", default="products.json", help="出力JSON（既定: products.json）")
    ap.add_argument("--delay", type=float, default=0.0, help="将来拡張用：待機秒（今は未使用）")
    args = ap.parse_args()

    if args.url:
        html = fetch_html(args.url)
        base = None
    else: 
        html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        base = None

    groups = parse_groups_from_html(html, base_url=base)
    doc = { "products": groups }
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] saved -> {Path(args.out).resolve()} (groups={len(groups)})")

if __name__=="__main__":
    main()
        