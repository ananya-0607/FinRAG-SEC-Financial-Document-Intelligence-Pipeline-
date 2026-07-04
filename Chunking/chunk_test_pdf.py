"""
SEC PDF CHUNKER
================
Give a PDF URL → downloads it → chunks page by page → prints chunks.

HOW TO USE:
  Way 1: python SEC_PDF_CHUNKER.py "https://www.sec.gov/.../report.pdf"
  Way 2: Set PDF_URL below → python SEC_PDF_CHUNKER.py

OUTPUT:
  - chunks printed on screen
  - pdf_chunks.json  (structured)
  - pdf_chunks.txt   (readable)

NEEDS: pip install requests pypdfium2 pymupdf
"""

import io
import re
import sys
import json
import requests
import warnings
warnings.filterwarnings('ignore')

import pypdfium2 as pdfium
import fitz   # pymupdf — for scanned page detection

# ============================================================
# CONFIGURATION
# ============================================================

PDF_URL    = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000038/a2026-annualxreportxwebxfi.pdf"
USER_AGENT = "Adqvest appservices@adqvest.com"   # SEC requires this

# ============================================================
# DOWNLOAD PDF FROM SEC
# ============================================================

def download_pdf(url):
    """Download PDF from SEC using SEC-required User-Agent."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept":     "*/*",
    }
    print(f"📥 Downloading: {url}")
    resp = requests.get(url, headers=headers, timeout=60,
                        verify=True, allow_redirects=True)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code} — could not download PDF")
    print(f"   ✅ Downloaded {len(resp.content):,} bytes")
    return resp.content


# ============================================================
# PARSE FILENAME METADATA
# ============================================================

def parse_file_name(file_name):
    """
    Parse metadata from our generated file name format:
      NVIDIA_CORP_0_Annual_Report_FY24_BZ1.pdf
      NVIDIA_CORP_0_Quarterly_Report_Q3_FY23_BZ51.pdf

    Returns: (company_name, report_type, relevant_year, file_id)
    Falls back gracefully if filename doesn't match our format.
    """
    try:
        base  = re.sub(r'\.(htm|html|pdf)$', '', file_name, flags=re.IGNORECASE)
        parts = base.split('_')

        zero_idx     = parts.index('0')
        company_name = ' '.join(parts[:zero_idx])
        file_id      = parts[-1]

        fy_idx = next((i for i, p in enumerate(parts) if p.startswith('FY')), None)
        if fy_idx is None:
            return file_name.replace('.pdf', ''), 'PDF Document', '', file_name

        fy = parts[fy_idx]

        if fy_idx > 0 and re.match(r'^Q[1-4]$', parts[fy_idx - 1]):
            quarter       = parts[fy_idx - 1]
            relevant_year = f"{quarter}_{fy}"
            report_type   = ' '.join(parts[zero_idx + 1 : fy_idx - 1])
        else:
            relevant_year = fy
            report_type   = ' '.join(parts[zero_idx + 1 : fy_idx])

        return company_name, report_type, relevant_year, file_id

    except Exception:
        # fallback for any filename that doesn't match our format
        return file_name.replace('.pdf', ''), 'PDF Document', '', file_name


# ============================================================
# PDF TEXT EXTRACTION — PAGE BY PAGE
# ============================================================

def extract_chunks_from_pdf(pdf_content, file_name,
                             threshold=10):
    """
    Extract text from PDF bytes, one chunk per page.
    Each chunk gets a header:
      PAGE NUMBER: N
      COMPANY NAME: NVIDIA CORP
      REPORT TYPE: Annual Report
      RELEVANT YEAR: FY24
      ... page text ...

    Also detects scanned pages (image-only, no text).

    Returns:
      chunks        — list of dicts {chunk_id, page_number, content,
                                     char_count, token_count}
      scanned_pages — list of page numbers that are scanned images
    """
    company_name, report_type, relevant_year, file_id = \
        parse_file_name(file_name)

    # ── Load PDF with pdfium ──────────────────────────────────────
    pdf         = pdfium.PdfDocument(io.BytesIO(pdf_content))
    total_pages = len(pdf)
    print(f"   📄 {total_pages} pages in PDF")

    # Remove hyperlink annotations (prevents text extraction noise)
    for page in pdf:
        page_handle = page.raw
        annot_count = pdfium.raw.FPDFPage_GetAnnotCount(page_handle)
        for i in range(annot_count - 1, -1, -1):
            annot   = pdfium.raw.FPDFPage_GetAnnot(page_handle, i)
            subtype = pdfium.raw.FPDFAnnot_GetSubtype(annot)
            if subtype == pdfium.raw.FPDF_ANNOT_LINK:
                pdfium.raw.FPDFPage_RemoveAnnot(page_handle, i)

    # ── Extract text page by page ─────────────────────────────────
    page_texts = []
    for page_number in range(total_pages):
        page_text = pdf[page_number].get_textpage().get_text_range()
        page_texts.append(page_text)

    # ── Detect scanned pages using fitz (pymupdf) ────────────────
    scanned_pages = []
    pdf_fitz      = fitz.open(stream=pdf_content, filetype="pdf")

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

    pdf_fitz.close()

    is_whole_doc_scanned = (len(scanned_pages) == total_pages)

    if is_whole_doc_scanned:
        print(f"   ⚠️  Whole document is scanned (image-only) — no text to extract")
        return [], scanned_pages

    if scanned_pages:
        print(f"   ⚠️  Scanned pages (skipped): {scanned_pages}")

    # ── Build one chunk per page ───────────────────────────────────
    chunks = []
    for page_number in range(total_pages):

        # skip scanned pages (no text)
        if (page_number + 1) in scanned_pages:
            continue

        page_text = page_texts[page_number]

        # Build chunk content with header (same format as our pipeline)
        content  = f"PAGE NUMBER: {page_number + 1}\n\n"
        content += f"COMPANY NAME: {company_name.strip()}\n\n"
        content += f"REPORT TYPE: {report_type.strip()}\n\n"
        content += f"RELEVANT YEAR: {relevant_year.strip()}\n\n"
        content += page_text.strip()

        # clean up extra blank lines
        content = re.sub(r'\n{3,}', '\n\n', content).strip()

        if not content.strip():
            continue

        chunks.append({
            "chunk_id":    len(chunks) + 1,
            "page_number": page_number + 1,
            "content":     content,          # real \n — ready for embeddings
            "char_count":  len(content),
            "token_count": len(content) // 4,
        })

    return chunks, scanned_pages


def tokens(text):
    return len(text) // 4


# ============================================================
# MAIN
# ============================================================

def main():
    url = sys.argv[1] if len(sys.argv) > 1 else PDF_URL

    # derive filename from URL
    file_name = url.split('/')[-1]
    if not file_name.lower().endswith('.pdf'):
        file_name = file_name + '.pdf'

    print(f"\n{'='*65}")
    print(f"SEC PDF CHUNKER")
    print(f"{'='*65}")
    print(f"URL       : {url}")
    print(f"File name : {file_name}")
    print()

    # ── Download ─────────────────────────────────────────────────
    try:
        pdf_content = download_pdf(url)
    except Exception as e:
        print(f"❌ Download failed: {e}")
        sys.exit(1)

    # ── Parse metadata ───────────────────────────────────────────
    company_name, report_type, relevant_year, file_id = \
        parse_file_name(file_name)

    print(f"\n   Company      : {company_name}")
    print(f"   Report type  : {report_type}")
    print(f"   Relevant year: {relevant_year}")
    print(f"   File ID      : {file_id}")
    print()

    # ── Chunk ────────────────────────────────────────────────────
    print("📄 Extracting text page by page...")
    chunks, scanned_pages = extract_chunks_from_pdf(pdf_content, file_name)

    if not chunks:
        print("❌ No chunks extracted (document may be fully scanned).")
        sys.exit(1)

    # ── Screen summary ───────────────────────────────────────────
    print(f"\n✅ {len(chunks)} chunks extracted\n" + "="*65)
    for c in chunks:
        preview = " ".join(c["content"].split())[:120]
        print(f"\nPAGE {c['page_number']}  "
              f"({c['token_count']} tokens, {c['char_count']} chars)")
        print(f"   {preview}...")

    # ── Save JSON ────────────────────────────────────────────────
    with open("pdf_chunks.json", "w", encoding="utf-8") as f:
        json.dump({
            "source":        url,
            "file_name":     file_name,
            "company_name":  company_name,
            "report_type":   report_type,
            "relevant_year": relevant_year,
            "file_id":       file_id,
            "total_chunks":  len(chunks),
            "scanned_pages": scanned_pages,
            "chunks":        chunks,
        }, f, indent=2, ensure_ascii=False)

    # ── Save TXT ─────────────────────────────────────────────────
    with open("pdf_chunks.txt", "w", encoding="utf-8") as f:
        f.write(f"Source     : {url}\n")
        f.write(f"Company    : {company_name}\n")
        f.write(f"Report type: {report_type}\n")
        f.write(f"Year       : {relevant_year}\n")
        f.write(f"Chunks     : {len(chunks)}\n")
        if scanned_pages:
            f.write(f"Scanned    : {scanned_pages}\n")
        f.write("="*65 + "\n\n")
        for c in chunks:
            f.write(f"{'='*20} PAGE {c['page_number']} "
                    f"({c['token_count']} tokens) {'='*20}\n")
            f.write(c["content"] + "\n\n")

    print(f"\n{'='*65}")
    print(f"💾 Saved: pdf_chunks.json  and  pdf_chunks.txt")
    print(f"   Total chunks : {len(chunks)}")
    if scanned_pages:
        print(f"   Scanned pages skipped: {scanned_pages}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()