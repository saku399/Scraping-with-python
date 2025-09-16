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
    return re.sub(r"\s+", " ", s or "").strip()

def fetch_html(url: str, timeout: int = 25) -> str:
    """URLからHTML取得（403が出るサイトはローカルHTMLで）"""
    with requests.Session() as s:
        s.headers.update(HEADERS)
        # ルートを先に踏んでクッキー（気休めだけど礼儀）
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
    """
    table の直前にある見出し(h1-h5)をグループ名に、
    説明は優先的に <div class="description"> を採用。
    無ければ見出し〜tableの間の <p> を説明として拾う。
    """
    name, desc = "", ""

    # --- 1) まず .description を優先して探す（直近のものだけ採用） ---
    # 「この description の“次に現れる table”が今の table」であることを確認して関連性を担保
    cand = table.find_previous("div", class_=lambda cs: cs and "description" in cs)
    while cand is not None:
        next_tbl = cand.find_next("table")
        if next_tbl is table:
            desc = clean(cand.get_text(" "))
            break
        cand = cand.find_previous("div", class_=lambda cs: cs and "description" in cs)

    # --- 2) 見出しを探す（h1〜h5の一番近いもの） ---
    prev = table
    while True:
        prev = prev.find_previous(["h1", "h2", "h3", "h4", "h5", "p", "div"])
        if prev is None:
            break
        tag = (prev.name or "").lower()
        if tag in {"h1", "h2", "h3", "h4", "h5"}:
            name = clean(prev.get_text(" "))
            # まだ desc が空なら、見出し〜table の間の <p> をつなげて説明に
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



def guess_columns(header_cells: list[str]) -> dict:
    """ヘッダから 'description' と 'price' の列インデックスを推定"""
    cols = { "description": None, "price": None }
    for i, h in enumerate(header_cells):
        hl = h.lower()
        if cols["description"] is None and any(k in hl for k in ["description", "desc"]):
            cols["description"] = i
        if cols["price"] is None and "price" in hl:
            cols["price"] = i
    return cols

def extract_price(text: str) -> str | None:
    """$125.40 → 125.40 など数字部分を取り出して文字列で返す"""
    m = re.search(r"([€£$])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
    if not m:
        return None
    return m.group(2).replace(",", "")

def parse_groups_from_html(html: str, base_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    # 候補となる「価格列を含むテーブル」を総当たり
    for table in soup.find_all("table"):
        # th が無いテーブルはスキップ
        headers = [clean(th.get_text(" ")) for th in table.find_all("th")]
        if not headers:
            continue
        if not any("price" in h.lower() for h in headers):
            continue  # 価格列が無ければ商品表ではないとみなす

        colmap = guess_columns(headers)
        # ヘッダが特殊でも、各行の最後のセルから価格を拾うフォールバックあり

        group_name, group_desc = nearest_heading_and_desc(table)
        subproducts = []

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            # 説明セル
            if colmap["description"] is not None and colmap["description"] < len(tds):
                name = clean(tds[colmap["description"]].get_text(" "))
            else:
                # 2列目を説明と仮定（Cat#/Description/Pack/Price…という並び想定）
                name = clean(tds[min(1, len(tds)-1)].get_text(" "))
            # 価格セル
            price_text = ""
            if colmap["price"] is not None and colmap["price"] < len(tds):
                price_text = clean(tds[colmap["price"]].get_text(" "))
            else:
                price_text = clean(tds[-1].get_text(" "))
            price = extract_price(price_text)

            # 名前が空 or 価格が無い行はスキップ（区切り行など）
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
        base = args.url
    else:
        html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        base = None

    groups = parse_groups_from_html(html, base_url=base)
    doc = { "products": groups }
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] saved -> {Path(args.out).resolve()} (groups={len(groups)})")

if __name__ == "__main__":
    main()
