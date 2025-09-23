import argparse, json, re
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

# プレースホルダー画像の判定ヒント（必要に応じて増やせます）
PLACEHOLDER_HINTS = ("Themes/", "placeholder", "quote.svg", "transparent.gif", "spacer.gif")

def _is_placeholder(url: str | None) -> bool:
    if not url:
        return True
    u = url.lower()
    return any(h.lower() in u for h in PLACEHOLDER_HINTS)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def fetch_html(url: str, timeout: int = 25) -> str:
    """URLからHTML文字列を取得（素のHTML。403が出るサイトはローカルHTML推奨）"""
    with requests.Session() as s:
        s.headers.update(HEADERS)
        try:
            p = urlparse(url)
            s.get(f"{p.scheme}://{p.netloc}/", timeout=timeout)  # Cookie用の軽い先踏み
        except Exception:
            pass
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        if not r.encoding:
            r.encoding = r.apparent_encoding
        return r.text

def nearest_heading_and_desc(table) -> tuple[str, str]:
    """この table に対応する見出し（h1〜h5）と説明文を推定"""
    name, desc = "", ""

    # 直前の .description を最優先
    cand = table.find_previous("div", class_=lambda cs: cs and "description" in cs)
    while cand is not None:
        next_tbl = cand.find_next("table")
        if next_tbl is table:
            desc = clean(cand.get_text(" "))
            break
        cand = cand.find_previous("div", class_=lambda cs: cs and "description" in cs)

    # 見出し → その直後〜table直前の<p>を説明として拾う
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
    """レスポンシブ表の 'Description:' みたいな先頭ラベルを除去"""
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
    """ヘッダから description/price/image の列インデックスを推定"""
    cols = {"description": None, "price": None, "image": None}
    for i, h in enumerate(header_cells):
        hl = h.lower()
        if cols["description"] is None and any(k in hl for k in ["description", "desc"]):
            cols["description"] = i
        if cols["price"] is None and "price" in hl:
            cols["price"] = i
        if cols["image"] is None and any(k in hl for k in ["image", "img", "picture", "photo", "thumbnail", "thumb", "icon"]):
            cols["image"] = i

    # フォールバック：<img> が多い列を image 列とみなす
    if cols["image"] is None and table is not None:
        img_counts = {}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            for j, td in enumerate(tds):
                if td.find("img"):
                    img_counts[j] = img_counts.get(j, 0) + 1
            if sum(img_counts.values()) >= 5:  # そこそこ溜まったら打ち切り
                break
        if img_counts:
            cols["image"] = max(img_counts, key=img_counts.get)
    return cols

def extract_price(text: str) -> str | None:
    """$1,234.50 → 1234.50 を返す。見つからなければ None"""
    m = re.search(r"([€£$])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if not m:
        return None
    return m.group(2).replace(",", "")

def parse_groups_from_html(html: str, base_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    # 相対URLを絶対化する小ヘルパ
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
        subproducts = []

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            # --- name（説明列 or フォールバック2列目） ---
            if colmap["description"] is not None and colmap["description"] < len(tds):
                name_txt = clean(tds[colmap["description"]].get_text(" "))
                desc_header = headers[colmap["description"]] if colmap["description"] is not None else None
                name = strip_label_prefix(name_txt, [desc_header, "Description", "Desc"])
            else:
                name_txt = clean(tds[min(1, len(tds)-1)].get_text(" "))
                name = strip_label_prefix(name_txt, ["Description", "Desc"])

            # --- price（指定列 or 最終列） ---
            if colmap["price"] is not None and colmap["price"] < len(tds):
                price_text = clean(tds[colmap["price"]].get_text(" "))
            else:
                price_text = clean(tds[-1].get_text(" "))
            price = extract_price(price_text)

            # 商品でない行の除外
            if not name or price is None:
                continue

            # --- image（プレースホルダー回避 + data-* 優先） ---
            img_url = None

            # 画像候補をセルから拾う
            def _pick_image_from_cell(td) -> str | None:
                # 1) <img ...>
                img = td.find("img")
                if img:
                    src      = img.get("src")
                    data_src = img.get("data-src") or img.get("data-original") or img.get("data-lazy")
                    data_ss  = img.get("data-srcset")

                    cand = None
                    if _is_placeholder(src) and data_src:
                        cand = data_src              # プレースホルダーなら data-src を採用
                    else:
                        cand = src or data_src       # ふつうは src を優先

                    # まだ無ければ srcset 系の先頭を
                    if not cand:
                        ss = data_ss or img.get("srcset")
                        if ss:
                            cand = ss.split(",")[0].strip().split()[0]

                    if cand:
                        return absu(cand)

                # 2) style="background-image:url(...)"
                style = td.get("style") or ""
                m = re.search(r'url\(["\']?([^"\')]+)["\']?\)', style)
                if m:
                    return absu(m.group(1))

                # 3) <a href="...jpg">
                a = td.find("a", href=True)  # ← herf の typo 修正
                if a and re.search(r'\.(png|jpe?g|webp|gif|svg|avif|ico)(?:\?.*)?$', a["href"], re.I):
                    return absu(a["href"])

                return None

            # image列があればそこから、無ければ行全体から
            if colmap.get("image") is not None and colmap["image"] < len(tds):
                img_url = _pick_image_from_cell(tds[colmap["image"]])
            if not img_url:
                for td in tds:
                    img_url = _pick_image_from_cell(td)
                    if img_url:
                        break

            # --- レコード追加 ---
            item = {"name": name, "price": price}
            if img_url:
                item["image"] = img_url
            subproducts.append(item)

        if subproducts:
            groups.append({
                "name": group_name or "",
                "desc": group_desc or "",
                "subproducts": subproducts
            })

    return groups

def main():
    ap = argparse.ArgumentParser(
        description="製品一覧の“表”から {name, desc, subproducts{name, price, image?}} をJSON化"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="対象ページのURL（403になる場合は --file を使う）")
    src.add_argument("--file", help="保存したHTMLファイルのパス（ブラウザで『ページを保存』）")
    ap.add_argument("--base", help="ローカルHTMLの相対URL解決に使うベースURL（例: https://www.example.com/）")
    ap.add_argument("-o", "--out", default="products.json", help="出力JSON（既定: products.json）")
    args = ap.parse_args()

    if args.url:
        html = fetch_html(args.url)
        base = args.url                           # 相対URL解決の基準
    else:
        html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        base = args.base or None                  # --file のときは --base があれば使う

    groups = parse_groups_from_html(html, base_url=base)
    doc = {"products": groups}
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] saved -> {Path(args.out).resolve()} (groups={len(groups)})")

if __name__ == "__main__":
    main()
