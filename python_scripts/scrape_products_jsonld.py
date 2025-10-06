#!/usr/bin/env python3
import argparse, json, re
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import hashlib

PLACEHOLDER_HINTS = ("placeholder", "quote.svg", "transparent.gif", "spacer.gif", "blank.gif")

def _is_placeholder(url: str | None) -> bool:
    if not url:
        return True
    u = url.lower()
    return any(h.lower() in u for h in PLACEHOLDER_HINTS)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def extract_price(text: str) -> str | None:
    m = re.search(r"([€£$])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    return m.group(2).replace(",", "") if m else None

def strip_label_prefix(value: str, labels: list[str]) -> str:
    if not value:
        return value
    s = clean(value)
    for lab in labels:
        if not lab:
            continue
        lab_clean = clean(lab).rstrip(":")
        pattern = re.compile(rf"^(?:{re.escape(lab_clean)})\s*:\s*", re.IGNORECASE)
        new_s = re.sub(pattern, "", s, count=1)
        if new_s != s:
            return new_s.strip()
    return re.sub(r"^\s*description\s*:\s*", "", s, flags=re.IGNORECASE).strip()

def make_id(text: str) -> str:
    return "ems-" + hashlib.md5(text.encode()).hexdigest()[:8]

def parse_groups_from_html(html: str, base_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    def absu(u: str | None) -> str | None:
        if not u:
            return None
        return urljoin(base_url, u) if base_url else u

    item_grid = soup.select("div.item-grid")
    #print(f"item_grid:{len(item_grid)}")
    total_item_boxes = 0
    all_item_boxes = []
    
    for grids in item_grid:
        item_boxes = grids.find_all("div", class_="item-box")
        #print(f'item_box:{len(item_boxs)}')
        #total_item_boxes += len(item_boxes)
        all_item_boxes.extend(item_boxes)
        
    #print(len(all_item_boxes))
    
    for item in all_item_boxes:
        name_div = item.find("h2", class_="product-title")
        name = clean(name_div.get_text(" ")) if name_div else "Empty-name"
        #print(f"name:{len(name)}")
        #print(name)
        
        desc_div = item.find("div", class_="description")
        desc = clean(desc_div.get_text(" ")) if desc_div else "Empty-Desc"
        #print(f"desc:{desc}")

        # --- 画像 ---
        picture_div = item.find("div", class_="picture")
        picture_img = picture_div.find("img") if picture_div else None
        picture = picture_img.get('src') if picture_img else "Empty-Pic"
        #print(f"picture{picture}") 
        
        data_table_elem = item.find("table", class_="data-table")
        tbody_elem = data_table_elem.find("tbody") if data_table_elem else None
        tr_list = tbody_elem.find_all("tr") if tbody_elem else []
        #print(f"tr_list{len(tr_list)}")
        
        subproducts = []
        
        for tr in tr_list:
            desc_td = tr.find("td", class_="line-desc")
            subName_a = desc_td.find("a") if desc_td else None
            subName = clean(subName_a.get_text(" ")) if subName_a else "Empty-name"
            #print(subName)
            #subproducts.append(subName)        
        
                # --- 価格 ---
            price_td = tr.find("td", class_="line-price")
            price_text = clean(price_td.get_text(" ")) if price_td else "Empty-price"
            price = extract_price(price_text)
            #print(f"price{price}")
            #subproducts.append(price)

            if subName and price is not None:
                record = {"name": subName, "price": price}
                subproducts.append(record)
                #print(f"subproducts:{len(subproducts)}")


    # ここでは例としてグループ名と説明は固定
        group_name = name
        group_desc = desc

        if subproducts:
            groups.append({
                "name": group_name,
                "desc": group_desc,
                "image": picture,
                "subproducts": subproducts
            })
            #print(f"groups:{len(groups)}")

        # ID付与
        for product in groups:
            product["id"] = make_id(product["name"])
            for sub in product["subproducts"]:
                sub["id"] = make_id(product["name"] + "|" + sub["name"])

    return groups

def main():
    ap = argparse.ArgumentParser(description="複数HTMLファイルから製品データをJSON化")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="HTMLファイルが入ったフォルダ")
    src.add_argument("--file", help="単一HTMLファイル")
    ap.add_argument("--base", help="相対URL解決のベースURL（例: https://www.emsdiasum.com/）")
    ap.add_argument("-o", "--out", default="products.json", help="出力JSONファイル名")
    args = ap.parse_args()

    all_groups = []

    if args.dir:
        folder = Path(args.dir)
        html_files = sorted(folder.glob("*.html"))
        for f in html_files:
            html = f.read_text(encoding="utf-8", errors="ignore")
            base = args.base or None
            groups = parse_groups_from_html(html, base_url=base)
            for g in groups:
                g["source"] = str(f.name)
            all_groups.extend(groups)
    else:
        html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        base = args.base or None
        groups = parse_groups_from_html(html, base_url=base)
        for g in groups:
            g["source"] = str(Path(args.file).name)
        all_groups.extend(groups)

    doc = {"products": all_groups}
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] saved -> {Path(args.out).resolve()} (groups={len(all_groups)})")

if __name__ == "__main__":
    main()
