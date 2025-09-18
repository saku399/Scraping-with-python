import asyncio, re, os, hashlib, mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")

def safe_name_from_url(u: str) -> str:
    """URLから安全なファイル名を作る（拡張子が無ければContent-Typeで補完予定）"""
    p = urlparse(u)
    base = os.path.basename(p.path) or ""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not base:
        base = "img"
    # 長すぎ/重複対策にクエリのハッシュを少し混ぜる
    h = hashlib.sha1((p.path + "?" + (p.query or "")).encode("utf-8")).hexdigest()[:8]
    name, ext = os.path.splitext(base)
    return f"{name}_{h}{ext}"

def ensure_ext_by_content_type(filename: str, content_type: str | None) -> str:
    if not content_type:
        return filename
    # 既に拡張子があればそのまま
    if os.path.splitext(filename)[1]:
        return filename
    # Content-Type から拡張子推定
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    if ext == ".jpe":  # たまに .jpe が返る
        ext = ".jpg"
    return filename + ext

def rewrite_srcset(value: str, mapping: dict[str, str], page_url: str) -> str:
    parts = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        # "url 2x" や "url 800w" の形式
        bits = part.split()
        url_abs = urljoin(page_url, bits[0])
        local = mapping.get(url_abs, bits[0])
        parts.append(" ".join([local] + bits[1:]))
    return ", ".join(parts)

def rewrite_background_urls(style_value: str, mapping: dict[str, str], page_url: str) -> str:
    # url("...") をすべて置換
    def repl(m):
        u = m.group(1)
        absu = urljoin(page_url, u)
        return f'url("{mapping.get(absu, u)}")'
    return re.sub(r'url\(["\']?([^"\')]+)["\']?\)', repl, style_value)

async def main():
    import sys
    if len(sys.argv) < 2:
        print("使い方: python save_html_and_images.py <URL> [出力フォルダ]")
        sys.exit(1)
    url = sys.argv[1]
    outdir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path("saved_page")
    outdir.mkdir(parents=True, exist_ok=True)
    assets_dir = outdir / "assets"
    assets_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA,
            locale="ja-JP",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        # 読み込み待ち：必要なら要素待ちに変えてOK
        await page.goto(url, wait_until="networkidle", timeout=60_000)

        # --- 画像URLをブラウザ側で収集（currentSrc や data-* も考慮） ---
        img_urls = await page.evaluate("""
        () => {
          const urls = new Set();

          // <img>
          document.querySelectorAll('img').forEach(img => {
            const cands = [
              img.currentSrc,
              img.src,
              img.getAttribute('data-src'),
              img.getAttribute('data-original'),
              img.getAttribute('data-lazy'),
            ];
            cands.forEach(u => { if (u) urls.add(u); });

            const sets = [img.srcset, img.getAttribute('data-srcset')].filter(Boolean);
            sets.forEach(ss => ss.split(',').forEach(part => {
              const u = part.trim().split(/\\s+/)[0];
              if (u) urls.add(u);
            }));
          });

          // <picture><source>
          document.querySelectorAll('picture source').forEach(s => {
            const sets = [s.srcset, s.getAttribute('data-srcset')].filter(Boolean);
            sets.forEach(ss => ss.split(',').forEach(part => {
              const u = part.trim().split(/\\s+/)[0];
              if (u) urls.add(u);
            }));
          });

          // inline background-image
          document.querySelectorAll('[style*="background"]').forEach(el => {
            const style = el.getAttribute('style') || '';
            const re = /url\\(["']?([^"')]+)["']?\\)/g;
            let m; while ((m = re.exec(style)) !== null) { urls.add(m[1]); }
          });

          return Array.from(urls);
        }
        """)

        # 絶対URL化 & 重複除去 & http(s)のみ
        img_urls_abs = []
        seen = set()
        for u in img_urls:
            absu = urljoin(page.url, u)
            if absu.startswith("http") and absu not in seen:
                seen.add(absu)
                img_urls_abs.append(absu)

        # --- 画像をダウンロード（Cookie/認証はpage.requestが引き継ぐ） ---
        mapping = {}  # 元URL -> ローカル相対パス
        idx = 1
        for u in img_urls_abs:
            try:
                resp = await page.request.get(u, headers={"Referer": page.url}, timeout=60_000)
                if not resp.ok:
                    continue
                body = await resp.body()
                ctype = resp.headers.get("content-type", "")
                fname = safe_name_from_url(u)
                fname = ensure_ext_by_content_type(fname, ctype)
                # 拡張子がない/不明なら画像っぽい拡張子を補完
                root, ext = os.path.splitext(fname)
                if ext.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}:
                    # ヘッダ見ても不明なら .bin とかにしたくないので .img に
                    if ctype.startswith("image/"):
                        ex = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".img"
                        if ex == ".jpe": ex = ".jpg"
                        fname = root + ex
                # 同名衝突回避
                local_name = f"{idx:04d}_{fname}"
                idx += 1
                (assets_dir / local_name).write_bytes(body)
                mapping[u] = f"assets/{local_name}"
            except Exception:
                # エラーのものはスキップ
                continue

        # --- HTMLを取得してローカルパスに書き換え ---
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # <img>
        for img in soup.find_all("img"):
            # src
            src = img.get("src")
            if src:
                absu = urljoin(page.url, src)
                if absu in mapping:
                    img["src"] = mapping[absu]
            # srcset
            ss = img.get("srcset")
            if ss:
                img["srcset"] = rewrite_srcset(ss, mapping, page.url)

        # <picture><source srcset>
        for src in soup.select("picture source"):
            ss = src.get("srcset")
            if ss:
                src["srcset"] = rewrite_srcset(ss, mapping, page.url)

        # inline background-image
        for el in soup.select("[style*='background']"):
            style = el.get("style")
            if style:
                el["style"] = rewrite_background_urls(style, mapping, page.url)

        # 保存
        (outdir / "index.html").write_text(str(soup), encoding="utf-8")
        await browser.close()
        print(f"[OK] saved -> {outdir.resolve()} (HTML + {len(mapping)} images)")

if __name__ == "__main__":
    asyncio.run(main())
