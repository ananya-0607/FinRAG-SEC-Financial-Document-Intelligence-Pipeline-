import re

with open("nvda_full.htm", "r", encoding="utf-8") as f:
    html = f.read()

# all hr page-breaks, in order
hr_re = re.compile(r'<hr\b[^>]*page-break[^>]*/?>', re.IGNORECASE)
hrs = list(hr_re.finditer(html))
print(f"Total hr breaks: {len(hrs)}")

# the strict pattern that matched 24
strict_re = re.compile(r'<div\s+id="([^"]+)">\s*</div><hr\b[^>]*page-break[^>]*/?>', re.IGNORECASE)
matched_positions = {m.start() for m in strict_re.finditer(html)}

# figure out which hr's were NOT part of a strict match, and show the 200 chars before them
for i, h in enumerate(hrs, start=1):
    pos = h.start()
    # check if this hr position is the end of some strict match
    is_matched = any(abs(pos - mp) < 500 for mp in matched_positions)
    if not is_matched:
        print(f"\n--- UNMATCHED BREAK #{i} at pos {pos} ---")
        print(repr(html[pos-250:pos+50]))