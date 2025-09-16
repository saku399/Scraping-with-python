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