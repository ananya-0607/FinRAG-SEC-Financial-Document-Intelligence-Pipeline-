"""
PAGE-BASED HTM CHUNKER
=======================
Give a SEC htm link → Each PAGE becomes ONE CHUNK.

How pages are detected:
  SEC HTML files mark page boundaries with:
    - <hr> tags (the border line you see)
    - style="page-break-before:always" / "page-break-after:always"
    - style="break-before:page"
  This script cuts a new chunk at every such marker.

What is captured: EVERYTHING on the page.
  - all text (any tag: p, div, span, font, bare text...)
  - all headings
  - all tables (cleaned: empty spacer cells dropped, $ merged with numbers)
  - nothing is left out

HOW TO USE:
  Way 1: python chunk_by_page.py "https://www.sec.gov/.../ex99_1.htm"
  Way 2: paste link in LINK below, then: python chunk_by_page.py

OUTPUT:
  - page chunks printed on screen
  - pages_chunks.json (structured)
  - pages_chunks.txt  (readable - verify nothing is missing)

NEEDS (one time): pip install requests beautifulsoup4
"""

import sys
import json
import requests
from bs4 import BeautifulSoup, NavigableString, Tag, Comment

# ============================================================
# PUT YOUR LINK HERE (or pass via command line)
# ============================================================
LINK = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000052/nvda-20260426.htm"

USER_AGENT = "Adqvest appservices@adqvest.com"

# Tags that should create a line break after their content
BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "tr", "section", "article", "header", "footer"}


# ============================================================
# TABLE CLEANER (drops empty spacer cells, merges $ and parens)
# ============================================================

def table_to_text(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]              # drop empty spacer cells
        if not cells:
            continue
        merged, i = [], 0
        while i < len(cells):
            if cells[i] == "$" and i + 1 < len(cells):
                merged.append("$" + cells[i + 1])    # $ | 81,615 -> $81,615
                i += 2
            elif cells[i] in (")", "%") and merged:
                merged[-1] += cells[i]               # (102 | ) -> (102)
                i += 1
            else:
                merged.append(cells[i])
                i += 1
        rows.append(" | ".join(merged))
    return "\n".join(rows)


# ============================================================
# PAGE BREAK DETECTION (smart - ignores decorative <hr> lines)
# ============================================================

def style_of(el):
    return (el.get("style") or "").lower().replace(" ", "")

def has_break_style_before(el):
    s = style_of(el)
    return ("page-break-before:always" in s or "break-before:page" in s
            or "page-break-before:left" in s or "page-break-before:right" in s)

def has_break_style_after(el):
    s = style_of(el)
    return ("page-break-after:always" in s or "break-after:page" in s)

def is_decorative_hr(el):
    """A narrow <hr> (width < 50%) is decoration (footnote line, title
       underline) - NOT a page break."""
    if el.name != "hr":
        return False
    width = (el.get("width") or "").strip()
    if width.endswith("%"):
        try:
            return float(width[:-1]) < 50        # e.g. width="25%" -> decorative
        except ValueError:
            return False
    s = style_of(el)
    if "width:" in s:
        import re as _re
        m = _re.search(r"width:(\d+(?:\.\d+)?)%", s)
        if m:
            return float(m.group(1)) < 50
    return False

def doc_uses_break_styles(soup):
    """Does this document mark pages with explicit page-break CSS styles?
       If yes, we trust ONLY those and ignore bare <hr> (decoration)."""
    for el in soup.find_all(style=True):
        s = style_of(el)
        if "page-break-before:always" in s or "page-break-after:always" in s \
           or "break-before:page" in s or "break-after:page" in s:
            return True
    return False


# ============================================================
# WALK THE WHOLE DOCUMENT - capture EVERYTHING, split at pages
# ============================================================

def split_into_pages(html):
    soup = BeautifulSoup(html, "html.parser")

    # remove invisible junk only (never visible content)
    for junk in soup(["script", "style", "meta", "link", "noscript", "title"]):
        junk.decompose()

    body = soup.body if soup.body else soup
    pages = [[]]            # list of pages; each page = list of text pieces

    # Decide ONCE which convention this document uses:
    use_styles_only = doc_uses_break_styles(soup)
    # use_styles_only = True  -> break ONLY at page-break styles (hr = decoration)
    # use_styles_only = False -> no styles in doc, so full-width <hr> = page break

    def breaks_before(el):
        if has_break_style_before(el):
            return True
        if el.name == "hr" and not use_styles_only and not is_decorative_hr(el):
            return True
        return False

    def breaks_after(el):
        return has_break_style_after(el)

    def new_page():
        if pages[-1]:        # only start new page if current has content
            pages.append([])

    def walk(node):
        # ---- skip invisible HTML comments <!-- ... --> ----
        if isinstance(node, Comment):
            return
        # ---- raw text (catches bare text not inside p/div) ----
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                pages[-1].append(text + " ")
            return

        if not isinstance(node, Tag):
            return

        # ---- page break BEFORE this element? ----
        if breaks_before(node):
            new_page()

        # ---- tables: format cleanly as one block, don't go inside ----
        if node.name == "table":
            t = table_to_text(node)
            if t:
                pages[-1].append("\n" + t + "\n")
        else:
            # ---- go inside and capture all children in order ----
            for child in node.children:
                walk(child)
            # line break after block-level tags so text doesn't glue together
            if node.name in BLOCK_TAGS:
                pages[-1].append("\n")

        # ---- page break AFTER this element? ----
        if breaks_after(node):
            new_page()

    walk(body)

    # join pieces into clean page texts
    page_texts = []
    for page in pages:
        text = "".join(page)
        # collapse extra blank lines
        lines = [ln.rstrip() for ln in text.split("\n")]
        cleaned, prev_blank = [], False
        for ln in lines:
            blank = (ln.strip() == "")
            if blank and prev_blank:
                continue
            cleaned.append(ln)
            prev_blank = blank
        text = "\n".join(cleaned).strip()
        if text:
            page_texts.append(text)

    return page_texts


def tokens(text):
    return len(text) // 4


# ============================================================
# MAIN: link in → one chunk per page out
# ============================================================

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else LINK

    print(f"\n🌐 Downloading: {url}")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    print(f"   Downloaded {len(resp.text):,} characters")

    print("📄 Splitting by PAGES...")
    page_texts = split_into_pages(resp.text)

    if len(page_texts) == 1:
        print("   ⚠️  Only 1 page detected - this document may have no page-break markers.")
        print("       Whole document = 1 chunk (nothing is lost).")

    chunks = [
        {
            "chunk_id": i,
            "page_number": i,
            "content": text,
            "tokens": tokens(text),
            "char_count": len(text),
        }
        for i, text in enumerate(page_texts, 1)
    ]

    # ---- screen summary ----
    print(f"\n✅ {len(chunks)} pages → {len(chunks)} chunks\n" + "=" * 60)
    for c in chunks:
        preview = " ".join(c["content"].split())[:100]
        print(f"\nPAGE {c['page_number']}  ({c['tokens']} tokens, {c['char_count']} chars)")
        print(f"   {preview}...")

    # ---- save ----
    with open("pages_chunks.json", "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    with open("pages_chunks.txt", "w", encoding="utf-8") as f:
        f.write(f"Source: {url}\nTotal pages/chunks: {len(chunks)}\n{'='*60}\n\n")
        for c in chunks:
            f.write(f"================ PAGE {c['page_number']} "
                    f"({c['tokens']} tokens) ================\n")
            f.write(c["content"] + "\n\n")

    print("\n" + "=" * 60)
    print("💾 Saved: pages_chunks.json  and  pages_chunks.txt")
    print("   Open pages_chunks.txt and verify each page's full content")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()