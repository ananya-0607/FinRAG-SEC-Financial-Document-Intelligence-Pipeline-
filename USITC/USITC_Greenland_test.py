"""
usitc_local_test.py
────────────────────────────────────────────────────────────────
Local test version of the USITC trade data collector.
- No ClickHouse / MySQL / job-log dependencies
- You manually supply year, country code, country name
- Saves output to local CSV files
- Prints a status summary at the end
────────────────────────────────────────────────────────────────
Usage:
    python usitc_local_test.py
Then edit the TEST CONFIG section below before running.
"""

import warnings
warnings.filterwarnings('ignore')

import re
import time
import datetime
import requests
import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

# ── TEST CONFIG — edit these before running ───────────────────────────────────
TOKEN = ''

# Explicit list of exact combos to test — only these will run, no cross-product.
# country_code / country_desc must come from getAllCountries (don't guess codes —
# see the note at the bottom of this section for how to look them up).
TEST_COMBOS = [
    {'country_code': '1010', 'country_desc': 'Greenland - GL - GRL',               'granularity': '4', 'trade_type': 'ForeignExp', 'year': '2019'},
    {'country_code': '1010', 'country_desc': 'Greenland - GL - GRL',               'granularity': '4', 'trade_type': 'GenImp',     'year': '2020'},
    {'country_code': '1010', 'country_desc': 'Greenland - GL - GRL',               'granularity': '4', 'trade_type': 'Import',     'year': '2020'},
    {'country_code': '1220', 'country_desc': 'Canada - CA - CAN',                  'granularity': '4', 'trade_type': 'Import',     'year': '2018'},
    {'country_code': '1220', 'country_desc': 'Canada - CA - CAN',                  'granularity': '4', 'trade_type': 'Import',     'year': '2019'},
    {'country_code': '1610', 'country_desc': 'Saint Pierre and Miquelon - PM - SPM', 'granularity': '4', 'trade_type': 'Export',     'year': '2026'},
    {'country_code': '1610', 'country_desc': 'Saint Pierre and Miquelon - PM - SPM', 'granularity': '4', 'trade_type': 'ForeignExp', 'year': '2018'},
]

OUTPUT_DIR = '.'   # folder where CSVs are saved; '.' = same folder as this script
SLEEP_BETWEEN_VARS = 3   # seconds between variable API calls (keep low for testing)
# ─────────────────────────────────────────────────────────────────────────────
#
# HOW TO LOOK UP THE CORRECT country_code / country_desc (don't guess these):
#   import requests
#   resp = requests.get(
#       'https://datawebws.usitc.gov/dataweb/api/v2/country/getAllCountries',
#       headers={"Authorization": "Bearer " + TOKEN}
#   )
#   for c in resp.json()['options']:
#       print(c['value'], '→', c['name'])
#
# Then copy the exact 'value' (code) and 'name' (desc) pair into TEST_COMBOS above.
# Typing a code from memory/guesswork is exactly what caused the Japan/China mixup.
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = 'https://datawebws.usitc.gov/dataweb'
HEADERS  = {
    "Content-Type" : "application/json; charset=utf-8",
    "Authorization": "Bearer " + TOKEN
}

MONTH_COLS = ['January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']

TRADE_CONFIG = {
    'Export': {
        'category'              : 'Domestic',
        'dataToReport'          : ['FAS_VALUE', 'FIRST_UNIT_QUANTITY', 'SECOND_UNIT_QUANTITY'],
        'countries_aggregation' : 'Break Out Countries',
        'has_country_col'       : True,
    },
    'ForeignExp': {
        'category'              : 'Foreign',
        'dataToReport'          : ['FAS_VALUE', 'FIRST_UNIT_QUANTITY', 'SECOND_UNIT_QUANTITY'],
        'countries_aggregation' : 'Break Out Countries',
        'has_country_col'       : True,
    },
    'Import': {
        'category'              : 'Consumption',
        'dataToReport'          : [
            'CONS_CUSTOMS_VALUE', 'CONS_FIR_UNIT_QUANT', 'CONS_SEC_UNIT_QUANT',
            'CONS_COST_INS_FREIGHT+CONS_CALC_DUTY', 'CONS_CUSTOMS_VALUE_SUB_DUTY',
            'CONS_CALC_DUTY', 'CONS_CHARGES_INS_FREIGHT', 'CONS_COST_INS_FREIGHT'
        ],
        'countries_aggregation' : 'Break Out Countries',
        'has_country_col'       : True,
    },
    'GenImp': {
        'category'              : 'General',
        'dataToReport'          : [
            'GEN_CUSTOMS_VALUE', 'GEN_FIR_UNIT_QUANTITY', 'GEN_SEC_UNIT_QUANTITY',
            'GEN_COST_INS_FREIGHT', 'GEN_CHARGES_INS_FREIGHT'
        ],
        'countries_aggregation' : 'Aggregate Countries',
        'has_country_col'       : False,
    },
}

FINAL_COLS = ['HTS_Number', 'Description', 'Country', 'Variable', 'Trade_Type',
              'Category', 'Granularity', 'Value', 'Quantity_Description',
              'Month', 'Year', 'Relevant_Date', 'Runtime']


# ── API helpers ───────────────────────────────────────────────────────────────

def getColumns(columnGroups, prevCols=None):
    if prevCols is None:
        columns = []
    else:
        columns = prevCols
    for group in columnGroups:
        if isinstance(group, dict) and 'columns' in group:
            getColumns(group['columns'], columns)
        elif isinstance(group, dict) and 'label' in group:
            columns.append(group['label'])
        elif isinstance(group, list):
            getColumns(group, columns)
    return columns


def getData(dataGroups):
    return [[field['value'] for field in row['rowEntries']] for row in dataGroups]


def fetchQueryResults(requestData, max_retries=3, retry_delay=30):
    for attempt in range(1, max_retries + 1):
        response = requests.post(
            BASE_URL + "/api/v2/report2/runReport",
            headers=HEADERS, json=requestData, verify=True
        )
        print(f"    HTTP {response.status_code}  (attempt {attempt})")
        if response.status_code in (429, 503):
            wait = int(response.headers.get('Retry-After', retry_delay))
            print(f"    Server busy ({response.status_code}), waiting {wait}s...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        payload = response.json()
        if 'error' in payload or 'data load mode' in str(payload).lower():
            print(f"    DataWeb in data load mode, waiting {retry_delay}s...")
            time.sleep(retry_delay)
            continue
        tables = payload['dto']['tables']
        all_non_empty = all(
            table.get('row_groups') and table['row_groups'][0].get('rowsNew')
            for table in tables
        )
        if not all_non_empty:
            if attempt == max_retries:
                print(f"    Empty response after {max_retries} attempts — no data for this variable.")
                return pd.DataFrame()
            print(f"    Empty table(s), retrying in {retry_delay}s...")
            time.sleep(retry_delay)
            continue
        columns  = getColumns(tables[0]['column_groups'])
        final_df = pd.DataFrame()
        for table in tables:
            data = getData(table['row_groups'][0]['rowsNew'])
            df   = pd.DataFrame(data, columns=columns)
            df['Variable'] = table['tableInfo']['dataToReportDesc']
            final_df = pd.concat([final_df, df], ignore_index=True)
        return final_df
    raise RuntimeError(f"fetchQueryResults failed after {max_retries} attempts.")


def build_query(trade_type, country_code, country_desc, granularity, year, data_to_report):
    cfg             = TRADE_CONFIG[trade_type]
    has_country_col = cfg['has_country_col']
    hts_label       = f"HTS{granularity} & DESCRIPTION"
    column_order    = [hts_label, 'YEAR'] + (['COUNTRY'] if has_country_col else [])
    full_col_order  = [
        {"checked": False, "disabled": False, "hasChildren": False,
         "name": hts_label, "value": hts_label,
         "classificationSystem": "", "groupUUID": "", "items": [], "tradeType": ""},
        {"checked": False, "disabled": False, "hasChildren": False,
         "name": "Year", "value": "YEAR",
         "classificationSystem": "", "groupUUID": "", "items": [], "tradeType": ""},
    ]
    if has_country_col:
        full_col_order.append(
            {"checked": False, "disabled": False, "hasChildren": False,
             "name": "Countries", "value": "COUNTRY",
             "classificationSystem": "", "groupUUID": "", "items": [], "tradeType": ""}
        )
    return {
        "savedQueryType"   : "",
        "isOwner"          : True,
        "unitConversion"   : "0",
        "manualConversions": [],
        "reportOptions"    : {
            "tradeType"            : trade_type,
            "classificationSystem" : "HTS"
        },
        "searchOptions": {
            "MiscGroup": {
                "districts": {
                    "aggregation"        : "Aggregate District",
                    "districtGroups"     : {},
                    "districts"          : [],
                    "districtsExpanded"  : [{"name": "All Districts", "value": "all"}],
                    "districtsSelectType": "all"
                },
                "importPrograms": {
                    "aggregation"       : None,
                    "importPrograms"    : [],
                    "programsSelectType": "all"
                },
                "extImportPrograms": {
                    "aggregation"             : "Aggregate CSC",
                    "extImportPrograms"        : [],
                    "extImportProgramsExpanded": [],
                    "programsSelectType"       : "all"
                },
                "provisionCodes": {
                    "aggregation"               : "Aggregate RPCODE",
                    "provisionCodesSelectType"  : "all",
                    "rateProvisionCodes"        : [],
                    "rateProvisionCodesExpanded": [
                        {"name": "All Rate Provision Codes", "value": "all"}
                    ],
                    "rateProvisionGroups": {"systemGroups": []}
                }
            },
            "commodities": {
                "aggregation"        : "Break Out Commodities",
                "codeDisplayFormat"  : "YES",
                "commodities"        : [],
                "commoditiesExpanded": [],
                "commoditiesManual"  : "",
                "commodityGroups"    : {"systemGroups": [], "userGroups": []},
                "commoditySelectType": "all",
                "granularity"        : granularity,
                "groupGranularity"   : None,
                "searchGranularity"  : None,
                "showHTSValidDetails": ""
            },
            "componentSettings": {
                "dataToReport"       : [data_to_report],
                "scale"              : "1",
                "timeframeSelectType": "fullYears",
                "years"              : [year],
                "startDate"          : None,
                "endDate"            : None,
                "startMonth"         : None,
                "endMonth"           : None,
                "yearsTimeline"      : "Monthly"
            },
            "countries": {
                "aggregation"        : cfg['countries_aggregation'],
                "countries"          : [country_code],
                "countriesExpanded"  : [{"name": country_desc, "value": country_code}],
                "countriesSelectType": "list",
                "countryGroups"      : {"systemGroups": [], "userGroups": []}
            }
        },
        "sortingAndDataFormat": {
            "DataSort": {
                "columnOrder"    : column_order,
                "fullColumnOrder": full_col_order,
                "sortOrder"      : [{"sortData": hts_label, "orderBy": "asc", "year": ""}]
            },
            "reportCustomizations": {
                "exportCombineTables": False,
                "totalRecords"       : "20000",
                "exportRawData"      : False
            }
        },
        "deletedCountryUserGroups"  : [],
        "deletedCommodityUserGroups": [],
        "deletedDistrictUserGroups" : []
    }


def process_df(raw_df, trade_type, granularity, country):
    """
    Clean and melt a single-variable raw DataFrame into long format.
    Returns the processed DataFrame (may be empty if all months are zero).
    """
    cfg = TRADE_CONFIG[trade_type]
    if 'Country' not in raw_df.columns:
        raw_df['Country'] = country.split(' - ')[0]
    raw_df = raw_df.rename(
        {'HTS Number': 'HTS_Number', 'Quantity Description': 'Quantity_Description'},
        axis=1
    )
    raw_df['Trade_Type']  = trade_type
    raw_df['Category']    = cfg['category']
    raw_df['Granularity'] = granularity

    # Value-only variables (e.g. FAS_VALUE, CONS_CUSTOMS_VALUE) don't return a
    # "Quantity Description" column from the API since there's no unit involved.
    # Add it as None so melt doesn't fail and FINAL_COLS shape stays consistent.
    if 'Quantity_Description' not in raw_df.columns:
        raw_df['Quantity_Description'] = None

    id_cols            = ['HTS_Number', 'Description', 'Year', 'Country',
                          'Quantity_Description', 'Variable',
                          'Trade_Type', 'Category', 'Granularity']
    present_month_cols = [m for m in MONTH_COLS if m in raw_df.columns]

    def to_numeric_col(series):
        return pd.to_numeric(
            series.astype(str).str.replace(',', '', regex=False),
            errors='coerce'
        ).fillna(0)

    published_months = [
        m for m in present_month_cols
        if to_numeric_col(raw_df[m]).gt(0).any()
    ]
    if not published_months:
        return pd.DataFrame()   # all zeros — treat as missing

    temp = pd.melt(raw_df, id_vars=id_cols, value_vars=published_months,
                   value_name='Value', var_name='Month')
    temp['Value'] = pd.to_numeric(
        temp['Value'].astype(str).str.replace(',', '', regex=False),
        errors='coerce'
    )
    temp['Year'] = temp['Year'].astype(str)
    temp['Relevant_Date'] = (
        pd.to_datetime(temp['Month'] + ', ' + temp['Year'])
        .dt.date + relativedelta(months=1, days=-1, day=1)
    )
    temp['Runtime'] = datetime.datetime.now()
    temp = temp[FINAL_COLS]
    temp['HTS_Number']  = temp['HTS_Number'].astype(int)
    temp['Granularity'] = temp['Granularity'].astype(int)
    temp = temp.replace({np.nan: None})
    return temp


# ── Main local test runner ────────────────────────────────────────────────────

def run_local_test():
    all_data_frames = []   # collects processed rows for CSV
    status_rows     = []   # collects one status dict per combo

    for combo in TEST_COMBOS:
        c_code      = combo['country_code']
        c_desc      = combo['country_desc']
        granularity = combo['granularity']
        trade       = combo['trade_type']
        year        = combo['year']

        print(f"\n{'='*65}")
        print(f"  Year={year} | Gran={granularity} | Trade={trade} | Country={c_desc}")
        print(f"{'='*65}")

        vars_to_fetch   = TRADE_CONFIG[trade]['dataToReport']
        missing_vars    = []
        inserted_rows   = 0
        max_date        = None
        combo_dfs       = []

        for data_var in vars_to_fetch:
            print(f"\n  → Variable: {data_var}")
            try:
                query  = build_query(trade, c_code, c_desc, granularity, year, data_var)
                raw_df = fetchQueryResults(query)

                if raw_df.empty:
                    print(f"    ✗ No data returned from API")
                    missing_vars.append(data_var)
                else:
                    processed = process_df(raw_df, trade, granularity, c_desc)
                    if processed.empty:
                        print(f"    ✗ All months zero/null")
                        missing_vars.append(data_var)
                    else:
                        print(f"    ✓ {len(processed):,} rows  |  months: {processed['Month'].unique().tolist()}")
                        combo_dfs.append(processed)

            except Exception as e:
                print(f"    ERROR fetching {data_var}: {e}")
                missing_vars.append(data_var)

            time.sleep(SLEEP_BETWEEN_VARS)

        # ── Determine status for this combo ───────────────────────
        if combo_dfs:
            combo_df      = pd.concat(combo_dfs, ignore_index=True)
            inserted_rows = len(combo_df)
            max_date      = combo_df['Relevant_Date'].max()
            status        = 'Completed'
            all_data_frames.append(combo_df)
        else:
            status = 'No_Data'

        missing_str = ','.join(missing_vars) if missing_vars else None

        status_rows.append({
            'Country_Code'     : c_code,
            'Country'          : c_desc,
            'Granularity'      : granularity,
            'Trade_Type'       : trade,
            'Year'             : year,
            'Status'           : status,
            'Missing_Variables': missing_str,
            'Relevant_Date'    : max_date,
            'Last_Collected'   : datetime.datetime.now(),
            'Runtime'          : datetime.datetime.now(),
        })

        print(f"\n  ── Combo result ──────────────────────────────────")
        print(f"  Status           : {status}")
        print(f"  Rows collected   : {inserted_rows:,}")
        print(f"  Max Relevant_Date: {max_date}")
        print(f"  Missing Variables: {missing_str or 'None'}")

    # ── Save outputs ──────────────────────────────────────────────────────────
    print(f"\n\n{'='*65}")
    print("  SAVING OUTPUT FILES")
    print(f"{'='*65}")

    # 1. ClickHouse data CSV
    if all_data_frames:
        final_data_df = pd.concat(all_data_frames, ignore_index=True)
        data_path = f"{OUTPUT_DIR}/usitc_test_data.csv"
        final_data_df.to_csv(data_path, index=False)
        print(f"\n  ✓ Data CSV   → {data_path}  ({len(final_data_df):,} rows)")
    else:
        print(f"\n  ✗ No data rows collected — data CSV not saved.")

    # 2. Status table CSV
    status_df   = pd.DataFrame(status_rows)
    status_path = f"{OUTPUT_DIR}/usitc_test_status.csv"
    status_df.to_csv(status_path, index=False)
    print(f"  ✓ Status CSV → {status_path}  ({len(status_df)} combos)")

    # 3. Print status summary table
    print(f"\n{'='*65}")
    print("  STATUS SUMMARY")
    print(f"{'='*65}")
    print(status_df[['Country', 'Trade_Type', 'Year', 'Status', 'Missing_Variables']].to_string(index=False))
    print(f"\nDone.")


if __name__ == '__main__':
    run_local_test()