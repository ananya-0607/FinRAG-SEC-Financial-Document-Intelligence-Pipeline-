"""
SEC EDGAR FILINGS LINKS CORPUS - Local CSV Collection
======================================================
Collects SEC filings for top 1000 companies from 2020 onwards.
Saves to LOCAL CSV only. No database.

Forms: 10-K, 10-Q, 8-K, 6-K, 20-F, ARS (+ amendments)

Output: 25 columns
"""

import re
import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime

sys.path.insert(0, '/Users/pushkar/Adqvest/new_codes/DEVELOPMENT')
from sec_filings import batch_filing_discovery, classify_exhibit

USER_AGENT = "Adqvest appservices@adqvest.com"

# ============================================================================
# CONFIGURATION
# ============================================================================

TOP_N_COMPANIES = 10
MIN_YEAR        = 2020
OUTPUT_FOLDER   = "./sec_filings_output"
OUTPUT_CSV      = f"{OUTPUT_FOLDER}/SEC_EDGAR_FILINGS_LINKS_CORPUS.csv"

# S3 details — same for every row
S3_BUCKET_NAME = "adqvest-data-bucket"           # ← change to your bucket
S3_FOLDER_NAME = "US_SEC_INVESTOR_PRESENTATIONS"  # ← change to your folder

OUR_TARGET_FORMS = (
    "10-K",  "10-K/A",
    "10-Q",  "10-Q/A",
    "8-K",   "8-K/A",
    "6-K",   "6-K/A",
    "20-F",  "20-F/A",
    "ARS",   "ARS/A",
)

# 25 columns (no Hyperlinks)
ALL_COLUMNS = [
    "File_ID",
    "Cik",
    "Company_Name",
    "Company_Name_Clean",
    "Ticker",
    "Generated_File_Name",
    "Form",
    "Filed",
    "Period_Of_Report",
    "Financial_Year",
    "Quarter",
    "Items",
    "Item_Description",
    "Accession",
    "Primary_Doc",
    "Doc_Type",
    "Link_File_Type",
    "Content_Type",
    "Filing_Index_Url",
    "File_Link",
    "S3_Upload_Status",
    "S3_Upload_Comments",
    "S3_Upload_Count",
    "S3_Bucket_Name",
    "S3_Folder_Name",
    "Chunking_Status",
    "Chunking_Comments",
    "Runtime",
]

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def clean_company_name(name):
    """NVIDIA CORP -> NVIDIA_CORP"""
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(name))
    return clean.strip().replace(' ', '_')


def get_financial_year(period, fiscal_year_end="1231"):
    """Get fiscal year label using company's actual fiscal year end.
    period          = "2024-01-28"  (YYYY-MM-DD)
    fiscal_year_end = "0131"        (MMDD from SEC, e.g. Jan 31 for NVIDIA)
    Returns: "FY24", "FY23" etc.
    Uses same rule as get_fiscal_quarter:
      period month > fy_end_month → FY ends next year
      period month <= fy_end_month → FY ends this year
    """
    if not period or len(period) < 4:
        return ""
    try:
        period_month = int(str(period)[5:7]) if len(period) >= 7 else 1
        period_year  = int(str(period)[:4])
        fy_end_month = int(str(fiscal_year_end)[:2]) if fiscal_year_end else 12
        if period_month > fy_end_month:
            fy_year = period_year + 1   # FY ends next calendar year
        else:
            fy_year = period_year       # FY ends this calendar year
        return f"FY{fy_year % 100}"
    except (ValueError, TypeError):
        return ""


# Annual forms — no quarter needed, just FY
ANNUAL_FORMS    = ("10-K", "10-K/A", "20-F", "20-F/A", "ARS", "ARS/A")

# Quarterly/current forms — quarter needed
QUARTERLY_FORMS = ("10-Q", "10-Q/A", "8-K", "8-K/A", "6-K", "6-K/A")


def get_fiscal_quarter(period, fiscal_year_end="1231"):
    """
    Get fiscal quarter and fiscal year label using the company's
    actual fiscal year end from SEC submissions.json.

    period          = "2022-10-30"   (Period_Of_Report, YYYY-MM-DD)
    fiscal_year_end = "0131"         (MMDD from SEC e.g. Jan 31 for NVIDIA)

    Returns: (quarter, fy_label)  e.g. ("Q3", "FY23")

    Example — NVIDIA (FY ends Jan 31):
      fiscal year runs Feb → Jan
      Q1=Feb,Mar,Apr  Q2=May,Jun,Jul  Q3=Aug,Sep,Oct  Q4=Nov,Dec,Jan
      Oct 30 2022 → Q3 of FY ending Jan 2023 → Q3_FY23  ✅
    """
    if not period or len(period) < 7:
        return "", ""
    try:
        period_month = int(str(period)[5:7])
        period_year  = int(str(period)[:4])
        fy_end_month = int(str(fiscal_year_end)[:2]) if fiscal_year_end else 12

        # Q1 starts the month AFTER fiscal year end
        q1_start = fy_end_month % 12 + 1

        # Build the 4 quarter start months (wraps around year boundary)
        q_starts = [((q1_start - 1 + (i * 3)) % 12) + 1 for i in range(4)]

        # Find which quarter the period_month falls in
        quarter_num = 4  # default
        for i in range(4):
            this_q = q_starts[i]
            next_q = q_starts[(i + 1) % 4]
            if this_q < next_q:
                if this_q <= period_month < next_q:
                    quarter_num = i + 1
                    break
            else:   # wraps around year end e.g. Nov,Dec,Jan
                if period_month >= this_q or period_month < next_q:
                    quarter_num = i + 1
                    break

        # FY label = calendar year the fiscal year ENDS in
        if period_month <= fy_end_month:
            fy_year = period_year
        else:
            fy_year = period_year + 1

        return f"Q{quarter_num}", f"FY{fy_year % 100}"

    except (ValueError, TypeError):
        return "", ""


def get_date_part(form, period, fiscal_year_end="1231"):
    """Return date string for file name.
    Annual forms  → 'FY24'
    Other forms   → 'Q3_FY23'
    """
    quarter, fy = get_fiscal_quarter(period, fiscal_year_end)
    if form in ANNUAL_FORMS:
        return fy
    return f"{quarter}_{fy}" if quarter and fy else fy


def generate_file_name(company_clean, content_type, form, period,
                       file_id, ext, fiscal_year_end="1231"):
    """NVIDIA_CORP_0_Quarterly_Report_Q3_FY23_BZ51.htm"""
    content_clean = str(content_type).replace(" ", "_")
    date_part     = get_date_part(form, period, fiscal_year_end)
    return f"{company_clean}_0_{content_clean}_{date_part}_{file_id}.{ext}"


def save_csv(rows, path):
    """Overwrite local CSV with all rows so far (real-time progress)."""
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    df = pd.DataFrame(rows, columns=ALL_COLUMNS)
    df.to_csv(path, index=False, encoding='utf-8')
    return len(df)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("\n" + "="*80)
    print("SEC EDGAR FILINGS CORPUS - TOP 1000 COMPANIES FROM 2020")
    print("Saving to LOCAL CSV only. No database.")
    print("="*80 + "\n")

    # ── Step 1: get top 1000 CIKs ─────────────────────────────────────────
    print(f"📡 Fetching top {TOP_N_COMPANIES} companies from SEC...")
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": USER_AGENT}
        )
        tickers_df = pd.DataFrame(r.json()).T
        ciks       = tickers_df["cik_str"].head(TOP_N_COMPANIES).astype(int).tolist()
        print(f"✅ Got {len(ciks)} companies\n")
    except Exception as e:
        print(f"❌ Error fetching companies: {e}")
        return

    all_rows        = []
    file_id_counter = 1
    run_time        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total           = len(ciks)

    # ── Step 2: process company by company ────────────────────────────────
    print(f"📥 Processing companies one by one...\n")
    print(f"💾 CSV: {OUTPUT_CSV}\n")
    print("="*80 + "\n")

    for idx, cik in enumerate(ciks, 1):
        try:
            df = batch_filing_discovery(
                [cik],
                forms=OUR_TARGET_FORMS,
                min_year=MIN_YEAR,
                fetch_exhibits=False
            )

            if df.empty:
                print(f"  [{idx:>4}/{total}] CIK {cik:<10} no filings")
                time.sleep(0.15)
                continue

            company_name  = df["Name"].iloc[0]
            rows_this_co  = []

            for _, filing in df.iterrows():
                cik_val       = int(filing["Cik"])
                form          = filing["Form"]
                accession     = filing["Accession"]
                company_name  = filing["Name"]
                company_clean = clean_company_name(company_name)
                ticker        = filing["Ticker"]
                filed         = filing.get("Filed", "")          or ""
                period        = filing.get("Period_Of_Report","") or ""
                items         = filing.get("Items", "")           or ""
                item_desc     = filing.get("Item_Description","") or ""
                fiscal_year_end = filing.get("Fiscal_Year_End", "1231") or "1231"
                # Financial_Year: uses company fiscal year end for correct FY label
                fy              = get_financial_year(period, fiscal_year_end) or get_financial_year(filed, fiscal_year_end)
                primary_doc     = filing.get("Primary_Doc", "")    or ""
                doc_type      = filing.get("Doc_Type", form)      or form
                doc_format    = filing.get("Doc_Format", "htm")   or "htm"
                primary_url   = filing.get("Primary_Doc_Url","")  or ""
                content_type  = classify_exhibit(form, doc_type, items)

                accn_clean    = accession.replace("-", "")
                index_url     = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_val}/{accn_clean}/{accession}-index.htm"
                )

                file_id   = f"BZ{file_id_counter}"
                file_name = generate_file_name(
                    company_clean, content_type, form, period,
                    file_id, doc_format, fiscal_year_end
                )

                rows_this_co.append({
                    "File_ID":            file_id,
                    "Cik":                cik_val,
                    "Company_Name":       company_name,
                    "Company_Name_Clean": company_clean,
                    "Ticker":             ticker,
                    "Generated_File_Name": file_name,
                    "Form":               form,
                    "Filed":              filed,
                    "Period_Of_Report":   period,
                    "Financial_Year":     fy,
                    "Quarter":            get_fiscal_quarter(period, fiscal_year_end)[0],
                    "Items":              items,
                    "Item_Description":   item_desc,
                    "Accession":          accession,
                    "Primary_Doc":        primary_doc,
                    "Doc_Type":           doc_type,
                    "Link_File_Type":     doc_format,
                    "Content_Type":       content_type,
                    "Filing_Index_Url":   index_url,
                    "File_Link":          primary_url,
                    "S3_Upload_Status":   None,
                    "S3_Upload_Comments": None,
                    "S3_Upload_Count":    0,
                    "S3_Bucket_Name":     S3_BUCKET_NAME,
                    "S3_Folder_Name":     S3_FOLDER_NAME,
                    "Chunking_Status":    None,
                    "Chunking_Comments":  None,
                    "Runtime":            run_time,
                })
                file_id_counter += 1

            all_rows.extend(rows_this_co)
            total_rows = save_csv(all_rows, OUTPUT_CSV)

            print(f"  [{idx:>4}/{total}] {str(company_name)[:30]:<30} "
                  f"{len(rows_this_co):>5} filings  |  CSV total: {total_rows}",
                  flush=True)

        except Exception as e:
            print(f"  [{idx:>4}/{total}] CIK {cik}  ERROR: {e}", flush=True)

        time.sleep(0.15)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "="*80)
    print("✅ DONE!")
    print("="*80)
    print(f"\n📊 Results:")
    print(f"   Total rows  : {len(all_rows)}")
    print(f"   Last File_ID: BZ{file_id_counter - 1}")
    print(f"   CSV saved at: {OUTPUT_CSV}")

    if all_rows:
        df_final = pd.DataFrame(all_rows)
        print(f"\n📋 Breakdown by Form:")
        print(df_final["Form"].value_counts().to_string())
        print(f"\n🏢 Top 10 companies by row count:")
        print(df_final["Company_Name"].value_counts().head(10).to_string())

    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()