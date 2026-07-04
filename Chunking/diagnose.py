"""
DIAGNOSTIC — run this locally and paste the output
====================================================
python diagnose.py "https://www.sec.gov/.../nvda-20260426.htm"
"""
import sys
import re
import requests
from bs4 import BeautifulSoup, NavigableString, Tag, Comment

USER_AGENT = "Adqvest appservices@adqvest.com"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000052/nvda-20260426.htm"

print(f"Downloading {URL} ...")
html = requests.get(URL, headers={"User-Agent": USER_AGENT}, timeout=30).text
print(f"Downloaded {len(html):,} chars\n")

soup = BeautifulSoup(html, "html.parser")
for junk in soup(["script", "style", "meta", "link", "noscript", "title"]):
    junk.decompose()

body = soup.body if soup.body else soup

# ── 1. What does the overall body structure look like? ──
print("=" * 60)
print("BODY DIRECT CHILDREN (first 30):")
print("=" * 60)
for i, child in enumerate(body.children):
    if not isinstance(child, Tag):
        continue
    style = (child.get("style") or "")[:80].replace("\n", " ")
    kids  = len([c for c in child.children if isinstance(c, Tag)])
    has_id = child.get("id", "")
    a_name = child.find("a", attrs={"name": True})
    hr_inside = child.find("hr")
    print(f"  [{i:>3}] <{child.name}> "
          f"id={repr(has_id)[:20]} "
          f"a_name={repr(a_name['name'] if a_name else '')[:30]} "
          f"children={kids} "
          f"hr_inside={hr_inside is not None} "
          f"style={repr(style)[:60]}")
    if i > 30:
        print("  ... (truncated)")
        break

# ── 2. Where are the page breaks? ──
print("\n" + "=" * 60)
print("PAGE BREAK ELEMENTS (first 20):")
print("=" * 60)

def style_of(el):
    return (el.get("style") or "").lower().replace(" ", "")

def is_page_break(el):
    s = style_of(el)
    if any(x in s for x in ["page-break-before:always", "break-before:page",
                              "page-break-after:always",  "break-after:page"]):
        return True
    if el.name == "hr":
        return True
    return False

breaks_found = 0
for i, child in enumerate(body.children):
    if not isinstance(child, Tag):
        continue
    if is_page_break(child):
        style = (child.get("style") or "")[:100].replace("\n", " ")
        print(f"  [{i:>3}] <{child.name}> style={repr(style)}")
        breaks_found += 1
        if breaks_found >= 20:
            break

print(f"\n  Total page breaks at top level: {breaks_found}")

# ── 3. Are page breaks NESTED inside divs/tables? ──
print("\n" + "=" * 60)
print("PAGE BREAKS NESTED DEEP (not at top level):")
print("=" * 60)
deep_breaks = []
for el in soup.find_all(style=True):
    s = style_of(el)
    if any(x in s for x in ["page-break-before:always", "break-before:page",
                              "page-break-after:always",  "break-after:page"]):
        # how deep is it?
        depth = 0
        parent = el.parent
        while parent and parent != body:
            depth += 1
            parent = parent.parent
        deep_breaks.append((depth, el.name, (el.get("style") or "")[:60]))

for depth, name, style in sorted(deep_breaks)[:20]:
    print(f"  depth={depth} <{name}> style={repr(style)}")

print(f"\n  Total deep page-break elements: {len(deep_breaks)}")
if deep_breaks:
    depths = [d for d, _, _ in deep_breaks]
    print(f"  Depth distribution: min={min(depths)} max={max(depths)} avg={sum(depths)/len(depths):.1f}")

# ── 4. Show raw HTML around first page break ──
print("\n" + "=" * 60)
print("RAW HTML AROUND FIRST PAGE BREAK (200 chars each side):")
print("=" * 60)
for marker in ["page-break-before:always", "break-before:page", "page-break-after:always"]:
    idx = html.find(marker)
    if idx > 0:
        print(f"\nMarker: '{marker}' at position {idx}")
        print(html[max(0,idx-200):idx+200])
        break

# ── 5. Check existing anchors/ids ──
print("\n" + "=" * 60)
print("EXISTING IDs AND <a name> IN DOCUMENT (first 30):")
print("=" * 60)
ids_found = 0
for el in soup.find_all(id=True):
    print(f"  <{el.name}> id={repr(el['id'])[:50]}")
    ids_found += 1
    if ids_found >= 15:
        break

names_found = 0
for el in soup.find_all("a", attrs={"name": True}):
    print(f"  <a name={repr(el['name'])[:50]}>")
    names_found += 1
    if names_found >= 15:
        break

print(f"\n  Total id= elements: {len(soup.find_all(id=True))}")
print(f"  Total <a name=> elements: {len(soup.find_all('a', attrs={'name': True}))}")