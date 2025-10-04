#!/usr/bin/env python3
r"""
使い方例:
  python parse_products_multi.py --inputs page1.html https://example.com/list -o all_products.json --base https://example.com/
  python parse_products_multi.py --dir ./saved_pages -o all_products.json
"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import hashlib

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

PLACEHOLDER_HINTS = (
    "/themes/", "placeholder", "quote.svg", "transparent.gif", "spacer.gif",
    "blank.gif", "pixel.gif", "no-image", "noimage", "default-image", "placeholder-img",
)

def _is_placeholder(url: str | None) -> bool:
    if not url:
        return True
    u = url.lower()
    return any(h in u for h in PLACEHOLDER_HINTS)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def fetch_html(url: str, timeout: int = 25, session: requests.Session | None = None) -> str:
    ses = session or requests.Session()
    ses.headers.update(HEADERS)
    try:
        p = urlparse(url)
        ses.get(f"{p.scheme}://{p.netloc}/", timeout=timeout)
    except Exception:
        pass
    r = ses.get(url, timeout=timeout)
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
            break
        cand = cand.find_previous("div", class_=lambda cs: cs and "description" in cs)
    prev = table
    while True:
        prev = prev.find_previous(["h1","h2","h3","h4","h5","p","div"])
        if prev is None:
            break
        tag = (prev.name or "").lower()
        if tag in {"h1","h2","h3","h4","h5"}:
            name = clean(prev.get_text(" "))
            if not desc:
                texts = []
                for sib in prev.next_siblings:
                    if sib is table:
                        break
                    if getattr(sib,"name",None)=="p":
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

def guess_columns(header_cells: list[str], table=None) -> dict:
    cols = {"description": None, "price": None, "image": None}
    for i,h in enumerate(header_cells):
        hl = h.lower()
        if cols["description"] is None and any(k in hl for k in ["description","desc"]):
            cols["description"]=i
        if cols["price"] is None and "price" in hl:
            cols["price"]=i
        if cols["image"] is None and any(k in hl for k in ["image","img","picture","photo","thumbnail","thumb","icon"]):
            cols["image"]=i
    if cols["image"] is None and table is not None:
        img_counts={}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            for j, td in enumerate(tds):
                if td.find("img"):
                    img_counts[j] = img_counts.get(j,0)+1
            if sum(img_counts.values())>=5:
                break
        if img_counts:
            cols["image"]=max(img_counts,key=img_counts.get)
    return cols

def extract_price(text: str) -> str | None:
    m = re.search(r"([€£$])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if not m:
        return None
    return m.group(2).replace(",","")

def make_id(text: str) -> str:
    return "ems-"+hashlib.md5(text.encode()).hexdigest()[:12]

def _pick_image_from_cell_or_link(el, base_url=None) -> str | None:
    candidates = []

    # --- imgタグの各種属性から ---
    for img in el.find_all("img"):
        for key in ("src", "data-src", "data-original", "data-lazy", "data-srcset"):
            v = img.get(key)
            if not v:
                continue
            if "," in v:  # srcsetなどカンマ区切りの場合
                parts = [p.strip().split()[0] for p in v.split(",") if p.strip()]
                candidates.extend(parts)
            else:
                candidates.append(v)

    # --- aタグのリンク先が画像なら ---
    for a in el.find_all("a", href=True):
        if re.search(r'\.(png|jpe?g|webp|gif|svg|avif|ico)(?:\?.*)?$', a["href"], re.I):
            candidates.append(a["href"])

    # --- style属性のbackground-imageから ---
    style = el.get("style") or ""
    m = re.findall(r'url\(["\']?([^"\')]+)["\']?\)', style)
    candidates.extend(m)

    # --- 絶対URL化 ---
    candidates = [urljoin(base_url, c) if base_url else c for c in candidates if c]

    # --- placeholderを除外して最初の有効なものを返す ---
    for c in candidates:
        if not _is_placeholder(c):
            return c

    # --- それ以外は最初の候補を返す ---
    return candidates[0] if candidates else None


def parse_groups_from_html(html: str, base_url: str | None = None, source_hint: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    def absu(u: str | None) -> str | None:
        if not u:
            return None
        return urljoin(base_url, u) if base_url else u

    for table in soup.find_all("table"):
        headers = [clean(th.get_text(" ")) for th in table.find_all("th")]
        if not headers:
            continue
        if not any("price" in h.lower() for h in headers):
            continue

        colmap = guess_columns(headers, table)
        group_name, group_desc = nearest_heading_and_desc(table)
        unique_subproducts = {}

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            # --- name ---
            if colmap["description"] is not None and colmap["description"] < len(tds):
                name_txt = clean(tds[colmap["description"]].get_text(" "))
                desc_header = headers[colmap["description"]] if colmap["description"] is not None else None
                name = strip_label_prefix(name_txt, [desc_header, "Description", "Desc"])
            else:
                name_txt = clean(tds[min(1, len(tds) - 1)].get_text(" "))
                name = strip_label_prefix(name_txt, ["Description", "Desc"])

            # --- price ---
            if colmap["price"] is not None and colmap["price"] < len(tds):
                price_text = clean(tds[colmap["price"]].get_text(" "))
            else:
                price_text = clean(tds[-1].get_text(" "))
            price = extract_price(price_text)

            if not name or price is None:
                continue

            # --- image ---
            img_url = None
            if colmap.get("image") is not None and colmap["image"] < len(tds):
                img_url = _pick_image_from_cell_or_link(tds[colmap["image"]], base_url)
            if not img_url:
                for td in tds:
                    img_url = _pick_image_from_cell_or_link(td, base_url)
                    if img_url:
                        break

            # --- 重複チェック ---
            key = f"{name}|{price}"
            if key in unique_subproducts:
                continue

            item = {"name": name, "price": price}
            if img_url:
                item["image"] = img_url
            unique_subproducts[key] = item

        # --- subproducts 最終化 ---
        subproducts = list(unique_subproducts.values())
        if subproducts:
            groups.append({
                "name": group_name or "",
                "desc": group_desc or "",
                "subproducts": subproducts,
                "source": source_hint or "",
            })

    return groups


def main():
    ap = argparse.ArgumentParser(
        description="製品一覧の“表”から {name, desc, subproducts{name, price, image?}} をJSON化（複数ファイル対応）"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--inputs","-i",nargs="+",help="複数のURLまたはHTMLファイルのパスを指定（スペース区切り）")
    src.add_argument("--dir",help="HTMLファイルが置いてあるディレクトリを指定（*.html を全部扱う）")
    src.add_argument("--url",help="単一URL（従来の --url と同じ）")
    src.add_argument("--file",help="単一ローカルHTMLファイル（従来の --file と同じ）")
    ap.add_argument("--base",help="ローカルHTMLの相対URL解決に使うベースURL（例: https://www.example.com/）")
    ap.add_argument("-o", "--out", default="products.json", help="出力JSON（既定: products.json）")
    args = ap.parse_args()

    sources = []
    if getattr(args, "inputs", None):
        sources.extend(args.inputs)
    elif args.dir:
        p = Path(args.dir)
        if not p.exists() or not p.is_dir():
            print(f"[ERR] --dir に存在しないディレクトリ: {args.dir}")
            return
        for f in sorted(p.glob("*.html")):
            sources.append(str(f))
    elif args.url:
        sources.append(args.url)
    elif args.file:
        sources.append(args.file)

    combined_groups = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for src_item in sources:
        base = None
        html = None
        source_hint = src_item
        if src_item.startswith("file://"):
            fp = Path(src_item[7:])
            if fp.exists():
                html = fp.read_text(encoding="utf-8", errors="ignore")
                base = args.base or None
        elif src_item.startswith("http://") or src_item.startswith("https://"):
            try:
                html = fetch_html(src_item, session=session)
                base = src_item
            except Exception as e:
                print(f"[WARN] URL を取得できませんでした: {src_item} -> {e}")
                continue
        else:
            fp = Path(src_item)
            if fp.exists():
                html = fp.read_text(encoding="utf-8", errors="ignore")
                base = args.base or None
            else:
                print(f"[WARN] 指定がファイルでもURLでもありません: {src_item}")
                continue

        groups = parse_groups_from_html(html, base_url=base, source_hint=source_hint)

        # id を source を混ぜてユニークにする
        for grp in groups:
            grp_key = f"{grp.get('source','')}|{grp.get('name','')}"
            grp_id = make_id(grp_key)
            grp["id"] = grp_id
            for sub in grp.get("subproducts", []):
                sub_key = f"{grp_key}|{sub.get('name','')}|{sub.get('price','')}"
                sub["id"] = make_id(sub_key)

        combined_groups.extend(groups)

    doc = {"products": combined_groups}
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] saved -> {Path(args.out).resolve()} (groups={len(combined_groups)})")

if __name__ == "__main__":
    main()
