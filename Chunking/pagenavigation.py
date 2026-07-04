"""
PAGE-BREAK ANCHOR CHUNKER (TABLE-SAFE)
======================================
Implements your logic: It finds the exact HTML tag where the page break 
occurs and extracts its native SEC locator ID.
"""

import sys
import json
import requests
import urllib.parse
from bs4 import BeautifulSoup, Comment, Tag, NavigableString

LINK = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000052/nvda-20260426.htm"
USER_AGENT = "Adqvest appservices@adqvest.com"

BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "tr", "section", "article", "header", "footer"}

def table_to_text(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if not cells:
            continue
        merged, i = [], 0
        while i < len(cells):
            if cells[i] == "$" and i + 1 < len(cells):
                merged.append("$" + cells[i + 1])
                i += 2
            elif cells[i] in (")", "%") and merged:
                merged[-1] += cells[i]
                i += 1
            else:
                merged.append(cells[i])
                i += 1
        rows.append(" | ".join(merged))
    return "\n".join(rows)

def style_of(el):
    return (el.get("style") or "").lower().replace(" ", "")

def has_break_style_before(el):
    s = style_of(el)
    return ("page-break-before:always" in s or "break-before:page" in s)

def is_decorative_hr(el):
    if el.name != "hr":
        return False
    width = (el.get("width") or "").strip()
    if width.endswith("%"):
        try: return float(width[:-1]) < 50
        except ValueError: return False
    return False

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else LINK

    print(f"🌐 Downloading: {url}")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for junk in soup(["script", "style", "meta", "link", "noscript", "title"]):
        junk.decompose()

    body = soup.body if soup.body else soup
    
    # Track the actual page break layout structures
    pages = [{"text_pieces": [], "break_element_id": None}]

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                pages[-1]["text_pieces"].append(text + " ")
            return
        if not isinstance(node, Tag):
            return

        # ---- YOUR LOGIC: Check if this tag is the page break boundary ----
        is_break = has_break_style_before(node) or (node.name == "hr" and not is_decorative_hr(node))
        
        if is_break and pages[-1]["text_pieces"]:
            # We hit a new page! Look for a locator ID on the break element itself
            anchor_id = node.get("id") or node.get("name")
            pages.append({"text_pieces": [], "break_element_id": anchor_id})

        # Capture content mapping
        if node.name == "table":
            pages[-1]["text_pieces"].append("\n" + table_to_text(node) + "\n")
        else:
            # If the current page chunk doesn't have an ID yet, check if any 
            # structural element inside it has an available SEC tracking attribute
            if pages[-1]["break_element_id"] is None and (node.get("id") or node.get("name")):
                pages[-1]["break_element_id"] = node.get("id") or node.get("name")
                
            for child in node.children:
                walk(child)
            if node.name in BLOCK_TAGS:
                pages[-1]["text_pieces"].append("\n")

    walk(body)

    chunks = []
    for i, p in enumerate(pages, 1):
        text = "".join(p["text_pieces"]).strip()
        if not text:
            continue
            
        # --- Create link directly using the discovered structural page break anchor ---
        if p["break_element_id"]:
            chunk_link = f"{url}#{p['break_element_id']}"
        else:
            # Table-resilient text fallback if the structural layout element lacked an ID
            clean_words = [w for w in text.split() if w.isalpha() and len(w) > 3][:3]
            if clean_words:
                chunk_link = f"{url}#:~:text={urllib.parse.quote(' '.join(clean_words))}"
            else:
                chunk_link = url

        chunks.append({
            "chunk_id": i,
            "page_number": i,
            "content": text,
            "chunk_link": chunk_link
        })

    with open("pages_chunks.json", "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    print(f"✅ Processed {len(chunks)} chunks linked via native page breaks.")

if __name__ == "__main__":
    main()