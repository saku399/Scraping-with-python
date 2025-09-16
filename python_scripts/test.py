import argparse
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import requests


def url_to_safe_filename(url: str) -> str:
    p = urlparse(url)

    
    path = p.path or "/"          
    if path.endswith("/"):      
        path += "index.html"

    base = f"{p.netloc}{path}"   
   
    if p.query:
        qhash = hashlib.sha1(p.query.encode("utf-8")).hexdigest()[:8]

        if "." in base.split("/")[-1]:
            base = re.sub(r"(\.[A-Za-z0-9]+)$", fr"_{qhash}\1", base)
        else:
            base = base + f"_{qhash}.html"

    if not re.search(r"\.[A-Za-z0-9]+$", base):
        base += ".html"

    safe = re.sub(r'[^A-Za-z0-9._/-]+', "_", base)

    if len(safe) > 200:
        name = safe.split("/")[-1] 
        short = name[:180] + "_cut.html"
        safe = "/".join(safe.split("/")[:-1] + [short])

    return safe


def fetch_html(url: str, timeout: int = 15) -> str:
    """URLからHTML文字列を取得する（Aの方法：静的/SSR向け）。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"        
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8,fr-CA;q=0.8,fr;q=0.7,ja;q=0.6",
        "Upgrade-Insecure-Requests": "1",    
   }

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status() 

    if not resp.encoding:
        resp.encoding = resp.apparent_encoding

    return resp.text  

def main():
    parser = argparse.ArgumentParser(
        description="静的/SSRページのHTMLソースを取得して保存します。"
    )
    parser.add_argument("url", help="取得したいページのURL（https://...）")
    parser.add_argument(
        "-o", "--out",
        help="保存先ファイル名（省略時はURLから自動決定）"
    )
    args = parser.parse_args()

    url = args.url
    out = args.out or url_to_safe_filename(url) 

    try:
        html = fetch_html(url, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 取得に失敗しました: {e}")
        return

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)  
    out_path.write_text(html, encoding="utf-8")        

    print(f"[OK] 保存しました: {out_path.resolve()}")


if __name__ == "__main__":
    main()
