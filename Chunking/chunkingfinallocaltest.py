"""
SEC EDGAR CHUNKING - LOCAL TEST SCRIPT
=======================================
Tests the core chunking + parsing logic WITHOUT:
  - S3 (you supply the file directly)
  - MySQL / ClickHouse (output goes to CSV + console)
  - Voyage AI embeddings (stubbed out)
  - adqvest_db / JobLog (removed)

HOW TO USE:
  1. Put your test file (e.g. NVIDIA_CORP_0_Annual_Report_FY24_BZ1.htm
     or APPLE_CORP_0_Quarterly_Report_Q3_FY24_BZ3.pdf) anywhere on disk.
  2. Set TEST_FILE_PATH below to that path.
  3. Run:  python sec_chunking_local_test.py
  4. Chunks are printed to console and saved to  output_chunks.csv
"""

import warnings
warnings.filterwarnings('ignore')

import os
import io
import re
import csv
import sys
import datetime
from pathlib import Path
from dateutil.relativedelta import relativedelta
from datetime import timedelta

import fitz                          # PyMuPDF
import pypdfium2 as pdfium
import pdfquery
from lxml import etree
from bs4 import BeautifulSoup, NavigableString, Tag, Comment

# ──────────────────────────────────────────────────────────────
# CONFIGURE YOUR TEST HERE
# ──────────────────────────────────────────────────────────────

TEST_FILE_PATH = r"C:\path\to\your\NVIDIA_CORP_0_Annual_Report_FY24_BZ1.htm"

# Optional metadata to simulate what would come from the DB source table.
# Leave as empty strings if you just want to test parsing/chunking.
TEST_TICKER          = "NVDA"
TEST_FORM            = "10-K"
TEST_PERIOD_OF_REPORT = "2024-01-28"   # or "" to leave blank
TEST_FILED_DATE      = "2024-02-21"   # or "" to leave blank

# Where to write the output CSV
OUTPUT_CSV = "output_chunks.csv"

# ──────────────────────────────────────────────────────────────


# ============================================================
# FILENAME PARSING HELPER (identical to production)
# ============================================================

def parse_file_name(file_name):
    """
    Parse metadata from file name format:
      NVIDIA_CORP_0_Annual_Report_FY24_BZ1.htm
      NVIDIA_CORP_0_Quarterly_Report_Q3_FY23_BZ51.htm
      APPLE_CORP_0_Earnings_Release_Q2_FY26_BZ3.pdf

    Returns: (company_name, report_type, relevant_year, file_id)
    """
    base = re.sub(r'\.(htm|html|pdf)$', '', file_name, flags=re.IGNORECASE)
    parts = base.split('_')

    zero_idx = parts.index('0')
    company_name = ' '.join(parts[:zero_idx])

    file_id = parts[-1]

    fy_idx = next((i for i, p in enumerate(parts) if p.startswith('FY')), None)
    if fy_idx is None:
        raise ValueError(f"No FY found in filename: {file_name}")

    fy = parts[fy_idx]

    if fy_idx > 0 and re.match(r'^Q[1-4]$', parts[fy_idx - 1]):
        quarter       = parts[fy_idx - 1]
        relevant_year = f"{quarter}_{fy}"
        report_type   = ' '.join(parts[zero_idx + 1 : fy_idx - 1])
    else:
        relevant_year = fy
        report_type   = ' '.join(parts[zero_idx + 1 : fy_idx])

    return company_name, report_type, relevant_year, file_id


# ============================================================
# FORMAT HELPERS (identical to production)
# ============================================================

def format_document_year(relevant_year):
    """'Q3_FY24' -> 'Q3 FY2024',  'FY24' -> 'FY2024'"""
    relevant_year = str(relevant_year)
    if '_' in relevant_year and re.match(r'^Q[1-4]_FY', relevant_year):
        q, fy = relevant_year.split('_')
        return f"{q} FY20{fy[2:]}"
    elif relevant_year.startswith('FY') and len(relevant_year) == 4:
        return f"FY20{relevant_year[2:]}"
    return relevant_year


def parse_quarter_year(qy_str):
    """Convert 'Q3_FY23' or 'FY24' to an actual date."""
    qy_str = qy_str.lower().replace('_', ' ')
    if 'q' in qy_str:
        parts   = qy_str.split(' ')
        q       = parts[0]
        fy      = parts[1]
        quarter = int(q[1])
        year    = int(fy.replace('fy', '20'))
        if quarter in [1, 2, 3]:
            month = 3 + (quarter * 3)
            year  = year - 1
        elif quarter == 4:
            month = 3
        else:
            raise ValueError(f"Invalid quarter: {quarter}")
        return (datetime.datetime(year, month, 1).date()
                + relativedelta(months=1) - timedelta(days=1))
    else:
        year = int(qy_str.replace('fy', '20'))
        return (datetime.datetime(year, 3, 1).date()
                + relativedelta(months=1) - timedelta(days=1))


# ============================================================
# HTM TABLE HELPER (identical to production)
# ============================================================

def _table_to_text(table):
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


def _style_of(el):
    return (el.get("style") or "").lower().replace(" ", "")


def _doc_uses_break_styles(soup):
    for el in soup.find_all(style=True):
        s = _style_of(el)
        if ("page-break-before:always" in s or
                "page-break-after:always" in s or
                "break-before:page" in s or
                "break-after:page" in s):
            return True
    return False


def _is_decorative_hr(el):
    if el.name != "hr":
        return False
    width = (el.get("width") or "").strip()
    if width.endswith("%"):
        try:
            return float(width[:-1]) < 50
        except ValueError:
            return False
    s = _style_of(el)
    if "width:" in s:
        m = re.search(r"width:(\d+(?:\.\d+)?)%", s)
        if m:
            return float(m.group(1)) < 50
    return False


def _split_htm_into_pages(html):
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "meta", "link", "noscript", "title"]):
        junk.decompose()

    body = soup.body if soup.body else soup
    pages = [[]]

    use_styles_only = _doc_uses_break_styles(soup)

    def breaks_before(el):
        s = _style_of(el)
        if ("page-break-before:always" in s or "break-before:page" in s):
            return True
        if el.name == "hr" and not use_styles_only and not _is_decorative_hr(el):
            return True
        return False

    def breaks_after(el):
        s = _style_of(el)
        return ("page-break-after:always" in s or "break-after:page" in s)

    def new_page():
        if pages[-1]:
            pages.append([])

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                pages[-1].append(text + " ")
            return
        if not isinstance(node, Tag):
            return
        if breaks_before(node):
            new_page()
        if node.name == "table":
            t = _table_to_text(node)
            if t:
                pages[-1].append("\n" + t + "\n")
        else:
            for child in node.children:
                walk(child)
            if node.name in {"p", "div", "br", "h1", "h2", "h3",
                             "h4", "h5", "h6", "li", "tr",
                             "section", "header", "footer"}:
                pages[-1].append("\n")
        if breaks_after(node):
            new_page()

    walk(body)

    page_texts = []
    for page in pages:
        raw   = "".join(page)
        lines = [ln.rstrip() for ln in raw.split("\n")]
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


# ============================================================
# HTM EXTRACTION (identical to production)
# ============================================================

def extract_text_from_htm(files_content, file_name):
    company_name, report_type, relevant_year, file_id = \
        parse_file_name(file_name)

    html_content = files_content[file_name]
    if isinstance(html_content, bytes):
        html_content = html_content.decode("utf-8", errors="replace")

    page_texts = _split_htm_into_pages(html_content)

    if not page_texts:
        page_texts = [html_content]

    text = ''
    for page_number, page_text in enumerate(page_texts):
        text += 'PAGE NUMBER: '   + str(page_number + 1) + '\n\n'
        text += 'COMPANY NAME: '  + str(company_name.strip()) + '\n\n'
        text += 'REPORT TYPE: '   + str(report_type.strip()) + '\n\n'
        text += 'RELEVANT YEAR: ' + str(relevant_year.strip()) + '\n\n'
        text += page_text + '\n\n'

    text = re.sub('\n\n\n', '\n\n', text)

    return (company_name, report_type, relevant_year, file_id,
            text, [], False)


# ============================================================
# PDF EXTRACTION (identical to production)
# ============================================================

def pdf_to_text(pdf_file):
    pdf = pdfquery.PDFQuery(io.BytesIO(pdf_file))
    pdf.load()
    pages = pdf.extract([['pages', 'LTPage']])
    page_elements = pages['pages']

    def extract_text_from_element(element):
        text_list = []
        if element.text and element.text.strip():
            text_list.append(element.text.strip())
        for child in element:
            text_list.extend(extract_text_from_element(child))
        return text_list

    text_per_page = []
    for page in page_elements:
        xml_data = etree.tostring(page, pretty_print=True).decode('utf-8')
        root = etree.fromstring(xml_data)
        text_list = extract_text_from_element(root)
        extracted_text = '\n'.join(text_list)
        text_per_page.append(extracted_text)
    return text_per_page


def extract_text_from_pdf(files_content, file_name,
                           threshold=10, image_coverage_threshold=0.8):
    company_name, report_type, relevant_year, file_id = parse_file_name(file_name)

    pdf_content = files_content[file_name]
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_content))

    for page in pdf:
        page_handle = page.raw
        annot_count = pdfium.raw.FPDFPage_GetAnnotCount(page_handle)
        for i in range(annot_count - 1, -1, -1):
            annot   = pdfium.raw.FPDFPage_GetAnnot(page_handle, i)
            subtype = pdfium.raw.FPDFAnnot_GetSubtype(annot)
            if subtype == pdfium.raw.FPDF_ANNOT_LINK:
                pdfium.raw.FPDFPage_RemoveAnnot(page_handle, i)

    text        = ''
    total_pages = len(pdf)
    page_texts  = []

    for page_number in range(total_pages):
        page_text = pdf[page_number].get_textpage().get_text_range()
        page_texts.append(page_text)

        text += 'PAGE NUMBER: '   + str(page_number + 1) + '\n\n'
        text += 'COMPANY NAME: '  + str(company_name.strip()) + '\n\n'
        text += 'REPORT TYPE: '   + str(report_type.strip()) + '\n\n'
        text += 'RELEVANT YEAR: ' + str(relevant_year.strip()) + '\n\n'
        text += page_text + '\n\n'

    text = re.sub('\n\n\n', '\n\n', text)

    scanned_pages   = []
    pdf_fitz        = fitz.open(stream=pdf_content, filetype="pdf")
    for page_num, page in enumerate(pdf_fitz):
        text_len  = len(page_texts[page_num].strip())
        low_text  = text_len < threshold
        large_img = False
        for img in page.get_images(full=True):
            xref = img[0]
            pix  = fitz.Pixmap(pdf_fitz, xref)
            if (pix.width  >= page.rect.width  * 0.9 and
                    pix.height >= page.rect.height * 0.9):
                large_img = True
                break
        if low_text and large_img:
            scanned_pages.append(page_num + 1)

    is_whole_doc_scanned = (len(scanned_pages) == total_pages)
    pdf_fitz.close()

    return (company_name, report_type, relevant_year, file_id,
            text, scanned_pages, is_whole_doc_scanned)


# ============================================================
# CHUNKING HELPERS (identical to production)
# ============================================================

def extract_chunks_from_text(text):
    chunks = text.split("PAGE NUMBER: ")
    return chunks[1:]


def analyse_chunk(chunk):
    patterns = [
        r"\bQ[1-4] & Year (201[0-9]|202[0-4]) \b",
        r"\bQ[1-4] (201[0-9]|202[0-4]) \b",
        r"\bQ[1-4](201[0-9]|202[0-4]) \b",
        r"\bQ[1-4] (1[0-9]|2[0-4]) \b",
        r"\bQ[1-4](1[0-9]|2[0-4]) \b",
    ]
    return any(re.findall(p, chunk) for p in patterns)


# ============================================================
# EMBEDDING STUB  (no Voyage AI call — returns dummy vector)
# ============================================================

def embed_with_fallback(chunks):
    """Stub: returns a list of zero-vectors so all other logic runs unchanged."""
    print(f"  [EMBED STUB] Skipping real embedding for {len(chunks)} chunk(s)")
    return [[0.0] * 1024 for _ in chunks]


def batchify(data, batch_size):
    for i in range(0, len(data), batch_size):
        yield data[i:i + batch_size]


# ============================================================
# LOCAL TEST RUNNER
# ============================================================

def run_local_test(file_path: str,
                   ticker: str = "",
                   form: str = "",
                   period_of_report: str = "",
                   filed_date: str = ""):

    file_path   = Path(file_path)
    file_name   = file_path.name

    print("=" * 60)
    print(f"FILE:   {file_name}")
    print(f"TICKER: {ticker or '(none)'}  |  FORM: {form or '(none)'}")
    print("=" * 60)

    # ── 1. Read file from disk ────────────────────────────────
    raw_bytes = file_path.read_bytes()
    files_content = {file_name: raw_bytes}

    # ── 2. Parse filename ─────────────────────────────────────
    try:
        company_name, report_type, relevant_year, file_id = \
            parse_file_name(file_name)
    except Exception as e:
        print(f"[ERROR] Filename parsing failed: {e}")
        return

    print(f"\nParsed from filename:")
    print(f"  company_name  = {company_name!r}")
    print(f"  report_type   = {report_type!r}")
    print(f"  relevant_year = {relevant_year!r}")
    print(f"  file_id       = {file_id!r}")

    document_year = format_document_year(relevant_year)
    print(f"  document_year = {document_year!r}")

    # ── 3. Year relevance check (FY20–FY29) ──────────────────
    fy_match = re.search(r'FY(\d{2})', relevant_year)
    fy_num   = int(fy_match.group(1)) if fy_match else 0
    if not (20 <= fy_num < 30):
        print(f"\n[SKIP] FY{fy_num:02d} is outside the relevant range (FY20–FY29).")
        return

    # ── 4. Extract text ───────────────────────────────────────
    is_htm = file_name.lower().endswith(('.htm', '.html'))

    try:
        if is_htm:
            (company_name, report_type, relevant_year,
             file_id, text_content,
             scanned_pages, is_whole_doc_scanned
             ) = extract_text_from_htm(files_content, file_name)
        else:
            (company_name, report_type, relevant_year,
             file_id, text_content,
             scanned_pages, is_whole_doc_scanned
             ) = extract_text_from_pdf(files_content, file_name)
    except Exception as e:
        print(f"[ERROR] Text extraction failed: {e}")
        return

    if not is_htm and is_whole_doc_scanned:
        print("[SKIP] Entire document appears to be scanned images — no text.")
        return

    if scanned_pages:
        print(f"[WARN] Scanned pages detected (no text): {scanned_pages}")

    # ── 5. Split into chunks ──────────────────────────────────
    extracted_chunks = extract_chunks_from_text(text_content)
    total_chunks = len(extracted_chunks)

    if total_chunks == 0:
        print("[SKIP] No chunks extracted — file may be too small.")
        return

    print(f"\nTotal chunks (pages): {total_chunks}")

    # ── 6. Build rows (embed stub, no DB write) ───────────────
    prepared_rows = []
    for page_number, chunk in enumerate(extracted_chunks):
        chunk_clean = chunk.replace("\\", "\\\\")
        chunk_clean = chunk_clean.replace("\n", "\\n")
        chunk_clean = chunk_clean.replace("\r", "\\r")
        chunk_clean = chunk_clean.replace("'", "\\'")
        chunk_clean = 'PAGE NUMBER: ' + chunk_clean
        page = page_number + 1
        prepared_rows.append({"chunk": chunk_clean, "page": page})

    BATCH_SIZE = 16
    all_output_rows = []

    for batch in batchify(prepared_rows, BATCH_SIZE):
        texts      = [row["chunk"] for row in batch]
        embeddings = embed_with_fallback(texts)   # stub — zero vectors

        for row, embed in zip(batch, embeddings):
            chunk_id     = f"{file_id}_{row['page']}"
            embed_preview = str(embed[:3])[:-1] + ", ...]"   # first 3 dims for display

            output_row = {
                "Chunk_Id":        chunk_id,
                "Document_Id":     file_id,
                "Document_Company": company_name.title(),
                "Document_Type":   report_type,
                "Document_Year":   document_year,
                "Document_Date":   period_of_report,
                "Page_Number":     row["page"],
                "Ticker":          ticker,
                "Form":            form,
                "Published_Date":  filed_date,
                "Embedding_Preview": embed_preview,
                # full chunk text (un-escaped for CSV readability)
                "Document_Content": extracted_chunks[row["page"] - 1].strip(),
            }
            all_output_rows.append(output_row)

    # ── 7. Print summary to console ───────────────────────────
    print("\n" + "=" * 60)
    print("CHUNK SUMMARY")
    print("=" * 60)
    for r in all_output_rows:
        print(f"\n--- Page {r['Page_Number']} | Chunk_Id: {r['Chunk_Id']} ---")
        preview = r["Document_Content"][:300].replace("\n", " ")
        print(f"  Preview: {preview}{'...' if len(r['Document_Content']) > 300 else ''}")
        print(f"  Embed:   {r['Embedding_Preview']}")

    # ── 8. Save to CSV ────────────────────────────────────────
    fieldnames = [
        "Chunk_Id", "Document_Id", "Document_Company",
        "Document_Type", "Document_Year", "Document_Date",
        "Page_Number", "Ticker", "Form", "Published_Date",
        "Embedding_Preview", "Document_Content",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_output_rows)

    print(f"\n{'=' * 60}")
    print(f"✓  {total_chunks} chunks saved to: {OUTPUT_CSV}")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # You can also pass a path as a command-line arg:
    #   python sec_chunking_local_test.py path/to/file.htm
    file_path = sys.argv[1] if len(sys.argv) > 1 else TEST_FILE_PATH

    run_local_test(
        file_path        = file_path,
        ticker           = TEST_TICKER,
        form             = TEST_FORM,
        period_of_report = TEST_PERIOD_OF_REPORT,
        filed_date       = TEST_FILED_DATE,
    )