"""
sec_filings.py
==============

Discover investor-relevant SEC filings and surface direct document URLs
for storage, downstream parsing, or manual review.

Covers
------
    10-K / 10-K/A  — annual reports
    10-Q / 10-Q/A  — quarterly reports
    8-K  / 8-K/A   — current reports (earnings, material events)
    6-K  / 6-K/A   — foreign private issuer current reports
    20-F / 20-F/A  — foreign private issuer annual reports
    ARS  / ARS/A   — annual report to shareholders
    DEF 14A        — annual proxy statements
    13-F / 13-F/A  — institutional holdings reports
    S-1  / S-1/A   — IPO registration statements + amendments
    S-3  / S-3/A   — shelf registration statements + amendments

Access pattern
--------------
    submissions.json  →  extract_filings()   (one row per filing)
         │
         └─ for 8-K / 6-K, optionally:
            fetch_exhibit_urls(cik, accession)  →  exhibit rows

Two-step rationale
------------------
    DEF 14A, 20-F, ARS, S-1, S-3:  primary_doc_url from submissions JSON
                                    IS the document you want. No extra request.
                         IS the document you want. No extra request.

    8-K:                 primary_doc_url is the 8-K wrapper form.
                         The investor deck / press release is EX-99.1,
                         one HTTP request away (the filing index page).
                         Fetching exhibits for all 8-Ks at batch scale
                         (500 companies × 50–100 8-Ks each = 25k+ requests)
                         is expensive, so it is opt-in via fetch_exhibits=True.

Output schema (FILING_COLUMNS)
-------------------------------
    cik, name, ticker,
    form, filed, period_of_report, items,
    accession, primary_doc, doc_type, doc_format,
    filing_index_url, primary_doc_url

    For exhibit rows (fetch_exhibits=True on 8-Ks):
    same columns, primary_doc / doc_type / doc_format / primary_doc_url
    refer to the exhibit file rather than the wrapper.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

USER_AGENT = "Adqvest appservices@adqvest.com"

# All form types we care about, including amendments.
TARGET_FORMS: tuple[str, ...] = (
    "10-K",    "10-K/A",         # annual reports
    "10-Q",    "10-Q/A",         # quarterly reports
    "8-K",     "8-K/A",          # current reports (earnings, material events)
    "6-K",     "6-K/A",          # foreign private issuer current reports
    "20-F",    "20-F/A",         # foreign private issuer annual reports
    "ARS",     "ARS/A",          # annual report to shareholders
    "DEF 14A", "DEF 14A/A",      # proxy statements
    "13-F",    "13-F/A"         # institutional holdings reports
)

# Exhibit types to capture when fetching filing index pages.
TARGET_DOC_TYPES: tuple[str, ...] = (
    "EX-99.1", "EX-99.2",       # press releases, investor decks
    "EX-99",                     # older naming convention
    "10-K",    "10-K/A",         # annual report
    "10-Q",    "10-Q/A",         # quarterly report
    "8-K",     "8-K/A",          # current report
    "6-K",     "6-K/A",          # foreign current report
    "20-F",    "20-F/A",         # foreign annual report
    "ARS",     "ARS/A",          # annual report to shareholders
    "DEF 14A", "DEF 14A/A",      # proxy statement
    "13-F",    "13-F/A"        # institutional holdings
)

# Minimum filing year cutoff (inclusive).
MIN_YEAR_DEFAULT = 2010

# EDGAR base URLs.
_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
_ARCHIVES_BASE    = "https://www.sec.gov/Archives/edgar/data"

# 8-K item codes → human-readable descriptions.
# Source: SEC Form 8-K instructions.
ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",       # earnings
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events Affecting Repayment of Securities",
    "2.05": "Cost Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure / Election of Directors or Principal Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Nominations Pursuant to Exchange Act Rule 14a-19",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",                           # investor presentations
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


def describe_items(items_str: str) -> str:
    """
    Convert an 8-K items string like '2.02,9.01' into a readable description.
    Returns comma-joined descriptions, or empty string for non-8-K filings.
    """
    if not items_str:
        return ""
    parts = []
    for item in re.split(r"[,;\s]+", items_str.strip()):
        item = item.strip()
        if item:
            parts.append(ITEM_DESCRIPTIONS.get(item, f"Item {item}"))
    return "; ".join(parts)


# Keywords used to classify exhibit content. Checked against the exhibit
# description field from the filing index page (case-insensitive).
_PRESENTATION_KEYWORDS: tuple[str, ...] = (
    "presentation", "slides", "slide deck",
    "investor day", "investor presentation", "investors presentation",
    "analyst day", "analyst presentation",
    "capital markets day", "capital markets presentation",
    "strategy day", "strategic update",
    "business update", "business overview",
    "conference", "roadshow",
    "supplemental data", "supplemental financial",
    "ir deck", "earnings presentation",
    "investor conference", "shareholder letter",
)

_EARNINGS_KEYWORDS: tuple[str, ...] = (
    "press release", "earnings release", "earnings results",
    "financial results", "quarterly results", "annual results",
    "results of operations", "financial statements",
    "q1 results", "q2 results", "q3 results", "q4 results",
    "first quarter", "second quarter", "third quarter", "fourth quarter",
    "full year results", "fiscal year results",
)

# Form-level content types that don't need keyword matching.
_FORM_CONTENT_TYPES: dict[str, str] = {
    "10-K":      "Annual Report",
    "10-K/A":    "Annual Report Amendment",
    "10-Q":      "Quarterly Report",
    "10-Q/A":    "Quarterly Report Amendment",
    "8-K":       "Current Report",
    "8-K/A":     "Current Report Amendment",
    "6-K":       "Foreign Current Report",
    "6-K/A":     "Foreign Current Report Amendment",
    "20-F":      "Foreign Issuer Annual Report",
    "20-F/A":    "Foreign Issuer Annual Report Amendment",
    "ARS":       "Annual Report to Shareholders",
    "ARS/A":     "Annual Report to Shareholders Amendment",
    "DEF 14A":   "Proxy Statement",
    "DEF 14A/A": "Proxy Statement Amendment",
    "13-F":      "Form 13-F (Institutional Holdings)",
    "13-F/A":    "Form 13-F Amendment (Institutional Holdings)"
}


def classify_exhibit(form: str, description: str, items: str = "") -> str:
    """
    Classify a filing or exhibit into a human-readable content type.

    Priority order:
      1. Form-level classification (DEF 14A, ARS, S-1, S-3) — always clear.
      2. Keyword match on exhibit description — differentiates presentations
         from earnings releases inside 8-K / 6-K filings.
      3. Item code fallback for 8-K — 7.01 → presentation, 2.02 → earnings.
      4. 'Other' if nothing matches.
    """
    # Form-level — unambiguous
    if form in _FORM_CONTENT_TYPES:
        return _FORM_CONTENT_TYPES[form]

    desc_lower = description.lower()

    # Keyword match on description
    if any(k in desc_lower for k in _PRESENTATION_KEYWORDS):
        return "Investor Presentation"
    if any(k in desc_lower for k in _EARNINGS_KEYWORDS):
        return "Earnings Release"

    # Fall back to item code for 8-K / 6-K
    if items:
        item_set = set(re.split(r"[,;\s]+", items.strip()))
        if "7.01" in item_set or "8.01" in item_set:
            return "Investor Presentation"
        if "2.02" in item_set:
            return "Earnings Release"

    # Wrapper forms — label by form type
    if form.startswith("8-K"):
        return "8-K Filing"
    if form.startswith("6-K"):
        return "6-K Filing"

    return "Other"


def fiscal_year_label(period_of_report: str) -> str:
    """
    Convert a period-of-report date (YYYY-MM-DD) to fiscal year label (FY23).
    
    Uses the year from the report date. For most companies this is correct.
    Edge case: companies with Jan–Mar fiscal year-ends will show FY25 for
    a period ending 2025-01-31, which is actually their FY2024 in their
    own reporting (fiscal year ended Jan 2025). This is unavoidable without
    per-company fiscal year end calendars, but the FY25 label is still
    unambiguous (it refers to the calendar year the period ended).
    
    Returns 'FY23' format, or empty string if date is missing/invalid.
    """
    if not period_of_report or len(period_of_report) < 4:
        return ""
    year = period_of_report[:4]
    if not year.isdigit():
        return ""
    return f"FY{year[2:]}"  # FY2023 -> FY23


FILING_COLUMNS: tuple[str, ...] = (
    "Cik", "Name", "Ticker",
    "Form", "Filed", "Period_Of_Report", "Financial_Year",
    "Items", "Item_Description",
    "Accession", "Primary_Doc", "Doc_Type", "Doc_Format",
    "Content_Type",
    "Filing_Index_Url", "Primary_Doc_Url", "Hyperlinks",
    "Fiscal_Year_End",   # MMDD from SEC submissions.json e.g. "0131"=Jan 31
)


def extract_hyperlinks(html: str, base_url: str) -> list[str]:
    """
    Extract all unique hyperlinks from an HTML document.
    Filters to htm/html/pdf links (the actual documents, not JS/CSS/images).
    Returns absolute URLs.
    """
    seen: set[str] = set()
    links: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = href.strip()
        # Skip anchors, javascript, mailto, and non-document links
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        ext = href.rsplit(".", 1)[-1].lower() if "." in href else ""
        if ext not in ("htm", "html", "pdf", "txt"):
            continue
        # Build absolute URL
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = f"https://www.sec.gov{href}"
        else:
            url = f"{base_url}{href}"
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links



# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, user_agent: str, timeout: int = 30) -> bytes:
    req = Request(url, headers={"User-Agent": user_agent,
                                 "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, user_agent: str) -> dict:
    return json.loads(_get(url, user_agent))


# ---------------------------------------------------------------------------
# Submissions helpers (with pagination for full history)
# ---------------------------------------------------------------------------

def fetch_all_submissions(cik: int,
                          min_year: int = MIN_YEAR_DEFAULT,
                          user_agent: str = USER_AGENT) -> list[dict]:
    """
    Return a flat list of filing dicts for one CIK, including older filings
    from the pagination files, back to ``min_year``.

    The submissions API splits older filings into separate JSON files linked
    from the main response. We follow those links until the ``filingTo`` date
    is before our cutoff — so we never fetch data we don't need.
    """
    url = f"{_SUBMISSIONS_BASE}/CIK{int(cik):010d}.json"
    sub = _get_json(url, user_agent)

    company_name    = sub.get("name", "")
    tickers         = sub.get("tickers", [])
    ticker          = tickers[0] if tickers else None
    fiscal_year_end = sub.get("fiscalYearEnd", "1231") or "1231"  # MMDD format e.g. "0131"=Jan31

    def _parse_recent(filings_block: dict) -> list[dict]:
        """Convert the parallel-array format into a list of dicts."""
        recent = filings_block.get("recent", {})
        if not recent:
            return []
        keys = list(recent.keys())
        if not keys:
            return []
        n = len(recent[keys[0]])
        rows = []
        for i in range(n):
            row = {k: recent[k][i] for k in keys}
            # Normalise the date string to YYYY-MM-DD for comparison
            filed = row.get("filingDate", "") or ""
            if filed[:4].isdigit() and int(filed[:4]) >= min_year:
                row["_cik"]            = int(cik)
                row["_name"]           = company_name
                row["_ticker"]         = ticker
                row["_fiscal_year_end"] = fiscal_year_end
                rows.append(row)
        return rows

    all_filings = _parse_recent(sub.get("filings", {}))

    # Follow pagination files for older filings.
    for page in (sub.get("filings") or {}).get("files", []):
        filing_to = (page.get("filingTo") or "")[:4]
        if filing_to and int(filing_to) < min_year:
            continue          # this page is entirely before our cutoff
        page_url = f"{_SUBMISSIONS_BASE}/{page['name']}"
        page_data = _get_json(page_url, user_agent)
        all_filings.extend(_parse_recent(page_data.get("filings", {})))

    return all_filings


# ---------------------------------------------------------------------------
# Filing index parser (for 8-K exhibits)
# ---------------------------------------------------------------------------

def _parse_index_html(html: str, base_url: str) -> list[dict]:
    """
    Parse the document table from an EDGAR filing index page.

    The table has columns: Seq | Description | Document | Type | Size.
    Returns a list of dicts with keys: seq, description, filename,
    doc_type, doc_format, url.
    """
    rows: list[dict] = []
    # Match each <tr> block
    for row_html in re.findall(r'<tr[^>]*>.*?</tr>', html,
                               flags=re.DOTALL | re.IGNORECASE):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html,
                           flags=re.DOTALL | re.IGNORECASE)
        if len(cells) < 4:
            continue
        seq_raw = re.sub(r'<[^>]+>', '', cells[0]).strip()
        if not seq_raw.isdigit():
            continue                   # header row or filler

        description = re.sub(r'<[^>]+>', '', cells[1]).strip()
        doc_cell    = cells[2]
        href_match  = re.search(r'href="([^"]+)"', doc_cell, re.I)
        filename    = (href_match.group(1) if href_match
                       else re.sub(r'<[^>]+>', '', doc_cell).strip())
        doc_type    = re.sub(r'<[^>]+>', '', cells[3]).strip()

        if not filename:
            continue

        # Build absolute URL
        if filename.startswith("http"):
            file_url = filename
        elif filename.startswith("/"):
            file_url = f"https://www.sec.gov{filename}"
        else:
            file_url = f"{base_url}{filename}"

        # Derive format from extension
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        fmt = ext if ext in ("htm", "html", "pdf", "txt", "xml") else ext or "unknown"

        rows.append({
            "seq":         int(seq_raw),
            "description": description,
            "filename":    filename,
            "doc_type":    doc_type,
            "doc_format":  fmt,
            "url":         file_url,
        })
    return sorted(rows, key=lambda x: x["seq"])


def fetch_exhibit_urls(cik: int,
                       accession: str,
                       user_agent: str = USER_AGENT) -> list[dict]:
    """
    Fetch the filing index page for one accession and return all documents
    whose type is in TARGET_DOC_TYPES (htm/html/pdf).

    Returns a list of dicts: seq, description, filename, doc_type,
    doc_format, url.
    """
    accn_clean = accession.replace("-", "")
    base_url   = f"{_ARCHIVES_BASE}/{int(cik)}/{accn_clean}/"
    index_url  = f"{base_url}{accession}-index.htm"
    try:
        html = _get(index_url, user_agent).decode("utf-8", errors="replace")
    except Exception:
        return []

    docs = _parse_index_html(html, base_url)
    return [
        d for d in docs
        if d["doc_type"] in TARGET_DOC_TYPES
        and d["doc_format"] in ("htm", "html", "pdf", "txt")
    ]


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

def extract_filings(
    all_filings: list[dict],
    forms: tuple[str, ...] = TARGET_FORMS,
    min_year: int = MIN_YEAR_DEFAULT,
    cik: int | None = None,
    fetch_exhibits: bool = False,
    user_agent: str = USER_AGENT,
) -> pd.DataFrame:
    """
    Filter a flat filings list (from ``fetch_all_submissions``) to the
    requested form types and build one row per filing (or per exhibit when
    ``fetch_exhibits=True`` for 8-K filings).

    Columns: FILING_COLUMNS
    """
    rows: list[dict] = []

    for f in all_filings:
        form   = f.get("form", "")
        filed  = f.get("filingDate", "") or ""
        if filed[:4].isdigit() and int(filed[:4]) < min_year:
            continue
        if forms and form not in forms:
            continue

        cik_val   = f.get("_cik") or cik or ""
        name      = f.get("_name", "")
        ticker    = f.get("_ticker")
        accession = f.get("accessionNumber", "")
        period    = f.get("reportDate", "") or ""
        items     = f.get("items", "") or ""        # 8-K items e.g. "2.02,9.01"
        primary   = f.get("primaryDocument", "") or ""
        primary_desc = f.get("primaryDocDescription", "") or ""

        accn_clean = accession.replace("-", "")
        base_url   = f"{_ARCHIVES_BASE}/{int(cik_val)}/{accn_clean}/"
        index_url  = f"{base_url}{accession}-index.htm"
        primary_url = f"{base_url}{primary}" if primary else ""

        ext = primary.rsplit(".", 1)[-1].lower() if "." in primary else ""
        fmt = ext if ext in ("htm", "html", "pdf", "txt") else ext or "unknown"

        # For 8-K, 6-K with fetch_exhibits=True, go one level deeper
        if fetch_exhibits and (form.startswith("8-K") or form.startswith("6-K")):
            exhibits = fetch_exhibit_urls(int(cik_val), accession, user_agent)
            time.sleep(0.1)  # be polite between exhibit fetches
            if exhibits:
                for ex in exhibits:
                    # Fetch hyperlinks from HTML exhibits
                    hyperlinks: list[str] = []
                    if ex["doc_format"] in ("htm", "html"):
                        try:
                            html = _get(ex["url"], user_agent).decode(
                                "utf-8", errors="replace")
                            hyperlinks = extract_hyperlinks(html, base_url)
                        except Exception:
                            pass
                    rows.append({
                        "Cik":              cik_val,
                        "Name":             name,
                        "Ticker":           ticker,
                        "Form":             form,
                        "Filed":            filed,
                        "Period_Of_Report": period,
                        "Financial_Year":   fiscal_year_label(period),
                        "Items":            items,
                        "Item_Description": describe_items(items),
                        "Accession":        accession,
                        "Primary_Doc":      ex["filename"],
                        "Doc_Type":         ex["doc_type"],
                        "Doc_Format":       ex["doc_format"],
                        "Content_Type":     classify_exhibit(
                                                form,
                                                ex["description"],
                                                items),
                        "Filing_Index_Url": index_url,
                        "Primary_Doc_Url":  ex["url"],
                        "Hyperlinks":       hyperlinks,
                        "Fiscal_Year_End":  f.get("_fiscal_year_end", "1231") or "1231",
                    })
                continue

        # Default: one row per filing with the primary document URL
        rows.append({
            "Cik":              cik_val,
            "Name":             name,
            "Ticker":           ticker,
            "Form":             form,
            "Filed":            filed,
            "Period_Of_Report": period,
            "Financial_Year":   fiscal_year_label(period),
            "Items":            items,
            "Item_Description": describe_items(items),
            "Accession":        accession,
            "Primary_Doc":      primary,
            "Doc_Type":         primary_desc or form,
            "Doc_Format":       fmt,
            "Content_Type":     classify_exhibit(form, primary_desc, items),
            "Filing_Index_Url": index_url,
            "Primary_Doc_Url":  primary_url,
            "Hyperlinks":       [],   # populated only when fetch_exhibits=True
            "Fiscal_Year_End":  f.get("_fiscal_year_end", "1231") or "1231",
        })

    return pd.DataFrame(rows, columns=list(FILING_COLUMNS))


# ---------------------------------------------------------------------------
# Batch: many CIKs
# ---------------------------------------------------------------------------

def batch_filing_discovery(
    ciks: Iterable[int],
    forms: tuple[str, ...] = TARGET_FORMS,
    min_year: int = MIN_YEAR_DEFAULT,
    fetch_exhibits: bool = False,
    throttle: float = 0.15,
    user_agent: str = USER_AGENT,
    on_error: str = "skip",
) -> pd.DataFrame:
    """
    Run filing discovery across many CIKs.

    Each company costs 1–3 HTTP requests (main submissions JSON + optional
    pagination files). With ``fetch_exhibits=True``, add ~1 request per
    8-K filing — can be 50–100 extra requests per company.

    Parameters
    ----------
    ciks : list of CIK integers
    forms : form types to include
    min_year : ignore filings before this year
    fetch_exhibits : if True, fetch exhibit-level URLs for 8-K filings
    throttle : seconds to sleep between companies
    on_error : 'skip' (default) or 'raise'
    """
    ciks = list(ciks)
    chunks: list[pd.DataFrame] = []

    for i, cik in enumerate(ciks, 1):
        try:
            all_filings = fetch_all_submissions(cik, min_year=min_year,
                                                user_agent=user_agent)
            df = extract_filings(all_filings, forms=forms, min_year=min_year,
                                 cik=cik, fetch_exhibits=fetch_exhibits,
                                 user_agent=user_agent)
            chunks.append(df)
            name = df["Name"].iloc[0] if not df.empty else ""
            print(f"  [{i:>4}/{len(ciks)}] CIK {cik:>8}  "
                  f"{str(name)[:30]:<30} {len(df):>5} filings",
                  file=sys.stderr)
        except Exception as e:
            print(f"  [{i:>4}/{len(ciks)}] CIK {cik:>8}  "
                  f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
            if on_error == "raise":
                raise
        time.sleep(throttle)

    if not chunks:
        return pd.DataFrame(columns=list(FILING_COLUMNS))
    return pd.concat(chunks, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    cik = int(sys.argv[1]) if len(sys.argv) > 1 else 1045810  # NVDA default
    filings = fetch_all_submissions(cik)
    df = extract_filings(filings, cik=cik)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 220)
    print(f"\n{len(df)} filings since {MIN_YEAR_DEFAULT}\n")
    print(df[["Form","Filed","Financial_Year","Items","Item_Description",
              "Doc_Type","Doc_Format","Primary_Doc_Url"]].to_string(index=False))