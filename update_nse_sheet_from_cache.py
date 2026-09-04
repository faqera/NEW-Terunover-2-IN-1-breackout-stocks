"""
Daily Update Script — रोज़ चलानी है
------------------------------------
1. आज की (या पिछले 7 दिन में जो भी मिले) NSE bhavcopy zip download करके
   cache CSV (bhavcopy_history_cache.csv) में append करती है (duplicate-safe)
2. Cache से हर stock का DMA (50/100/200), OUTPUT (Bull/Bear/Unconfirmed),
   % diff from 200 DMA, और CAR rating calculate करती है
3. "Top 250 Stocks" और "Top 250 Turnover" टैब्स के D:J columns update करती है

NSE historical API (cookies वाली) या yfinance की कोई ज़रूरत नहीं —
सिर्फ रोज़ की एक नई bhavcopy file चाहिए, जो बिना session के मिल जाती है।

चलाने का तरीका: python update_nse_sheet_from_cache.py
(GitHub Actions/cron में रोज़ मार्केट बंद होने के बाद चलाएं)
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import zipfile
import io
import pandas as pd
import os
import json
import time
from datetime import datetime, timedelta

CACHE_FILE = "bhavcopy_history_cache.csv"
FILTER_KEYWORDS = "BEES|ETF|GOLD|LIQUID|CASE|SILVER|LIQ"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


# ---------- Step 1: आज की bhavcopy लाकर cache में जोड़ना ----------
def fetch_bhavcopy_for_date(date_obj):
    date_str = date_obj.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return None

        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            csv_filename = z.namelist()[0]
            with z.open(csv_filename) as f:
                df = pd.read_csv(f)

        sym_col = 'TckrSymb' if 'TckrSymb' in df.columns else 'SYMBOL'
        close_col = 'ClsPric' if 'ClsPric' in df.columns else 'CLOSE'
        high_col = 'HghPric' if 'HghPric' in df.columns else 'HIGH'
        series_col = 'SctySrs' if 'SctySrs' in df.columns else 'SERIES'

        if series_col in df.columns:
            df = df[df[series_col].astype(str).str.strip() == 'EQ']

        df = df[~df[sym_col].astype(str).str.contains(FILTER_KEYWORDS, case=False, na=False)]

        if high_col not in df.columns:
            df[high_col] = df[close_col]

        out = df[[sym_col, close_col, high_col]].copy()
        out.columns = ["symbol", "close", "high"]
        out["date"] = date_obj.strftime("%Y-%m-%d")
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out["high"] = pd.to_numeric(out["high"], errors="coerce")
        out = out.dropna(subset=["close"])

        return out[["date", "symbol", "close", "high"]]

    except Exception as e:
        print(f"⚠️ {date_obj.strftime('%d-%m-%Y')}: Error — {e}")
        return None


def update_cache_with_latest():
    """पिछले 7 दिन में जो भी latest trading-day bhavcopy मिले, cache में जोड़ो।
    लौटाता है: fetched_date_str (जिस दिन का data मिला)"""
    existing = pd.DataFrame(columns=["date", "symbol", "close", "high"])
    if os.path.exists(CACHE_FILE):
        existing = pd.read_csv(CACHE_FILE)

    existing_dates = set(existing["date"].unique()) if not existing.empty else set()

    today = datetime.now()
    for i in range(7):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        date_key = day.strftime("%Y-%m-%d")

        if date_key in existing_dates:
            print(f"ℹ️ {date_key} पहले से cache में है — नया fetch नहीं करना")
            return date_key

        df_day = fetch_bhavcopy_for_date(day)
        if df_day is not None and not df_day.empty:
            combined = pd.concat([existing, df_day], ignore_index=True)
            combined = combined.drop_duplicates(subset=["date", "symbol"], keep="last")
            combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
            combined.to_csv(CACHE_FILE, index=False)
            print(f"✅ {date_key}: {len(df_day)} stocks cache में जोड़े गए")
            return date_key

    print("⚠️ पिछले 7 दिनों में कोई नई bhavcopy नहीं मिली")
    return None


# ---------- Step 2: Cache से DMA/CAR calculate करना ----------
def calculate_indicators_from_cache(cache_df, symbol, close_price):
    hist = cache_df[cache_df["symbol"] == symbol].sort_values("date")

    if hist.empty:
        return close_price, None, None, None, "Data Error", None, "TICKER NOT FOUND"

    closes = hist["close"]

    if len(closes) < 200:
        dma_50 = closes.tail(50).mean() if len(closes) >= 50 else None
        dma_100 = closes.tail(100).mean() if len(closes) >= 100 else None
        dma_200 = None
    else:
        dma_50 = closes.tail(50).mean()
        dma_100 = closes.tail(100).mean()
        dma_200 = closes.tail(200).mean()

    cmp = close_price

    if dma_200 is not None and dma_200 > 0:
        dma_dist = ((cmp - dma_200) / dma_200) * 100
    else:
        dma_dist = None

    if (dma_50 is not None and dma_100 is not None and dma_200 is not None and
            cmp > dma_50 and cmp > dma_100 and cmp > dma_200 and
            dma_dist is not None and 0.01 <= dma_dist <= 10):
        bull_status = "In Bull Run"
    elif (dma_50 is not None and dma_100 is not None and dma_200 is not None and
          cmp < dma_50 and cmp < dma_100 and cmp < dma_200 and
          dma_dist is not None and dma_dist <= -0.01):
        bull_status = "In Bear Run"
    else:
        bull_status = "Unconfirmed"

    # ---------- CAR Rating ----------
    if "high" in hist.columns and not hist["high"].isna().all():
        max_high_idx = hist["high"].idxmax()
        hist_slice = hist.loc[max_high_idx:].sort_values("date")
        prices = hist_slice["close"].tolist()
    else:
        prices = closes.tolist()

    if len(prices) < 10:
        car_rating = "Short History"
    else:
        cum_avg = []
        cumulative_sum = 0
        for i, price in enumerate(prices, 1):
            cumulative_sum += price
            cum_avg.append(cumulative_sum / i)

        last_10 = cum_avg[-10:]
        checks = 0
        for i in range(1, 10):
            if last_10[i] > last_10[i - 1]:
                checks += 1

        car_rating = "Buy/Average Out" if checks == 9 else "Avoid/Hold"

    return cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating


# ---------- Google Sheets ----------
def get_google_client():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv('GCP_CREDENTIALS')
        if not creds_json:
            raise Exception("GCP_CREDENTIALS environment variable not set.")

        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"❌ Google Sheet Connection Error: {e}")
        exit(1)


def process_sheet(worksheet, sheet_name, cache_df):
    print(f"\n📊 {sheet_name} टैब प्रोसेस हो रहा है...")

    all_data = worksheet.get_all_values()

    if len(all_data) < 2:
        print(f"⚠️ {sheet_name} में कोई डेटा नहीं मिला, स्किप कर रहे हैं")
        return

    rows = all_data[1:]
    total_rows = len(rows)
    batch_values = []

    for idx, row in enumerate(rows, start=2):
        if len(row) < 3 or not row[0].strip():
            batch_values.append(["", "", "", "", "", "", ""])
            continue

        symbol = row[0].strip()
        try:
            close_price = float(row[2])
        except (ValueError, IndexError):
            print(f"⚠️ {sheet_name} Row {idx}: '{row[0]}' का Close Price सही नहीं है")
            batch_values.append(["", "", "", "", "", "", ""])
            continue

        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating = calculate_indicators_from_cache(
            cache_df, symbol, close_price
        )

        row_data = [
            cmp if cmp is not None else "",
            dma_50 if dma_50 is not None else "",
            dma_100 if dma_100 is not None else "",
            dma_200 if dma_200 is not None else "",
            bull_status,
            dma_dist if dma_dist is not None else "",
            car_rating
        ]
        batch_values.append(row_data)

        if (idx - 1) % 50 == 0:
            print(f"⏳ {sheet_name}: {idx - 1}/{total_rows} स्टॉक्स प्रोसेस हो चुके हैं...")

    if batch_values:
        end_row = 2 + len(batch_values) - 1
        range_name = f"D2:J{end_row}"

        print(f"💾 {sheet_name}: {len(batch_values)} पंक्तियों को अपडेट किया जा रहा है...")

        try:
            worksheet.update(range_name=range_name, values=batch_values, value_input_option='USER_ENTERED')
            print(f"✅ {sheet_name}: D से J कॉलम सफलतापूर्वक अपडेट हो गए!")
        except Exception as e:
            print(f"❌ {sheet_name} Batch Update में Error: {e}")
    else:
        print(f"❌ {sheet_name}: अपडेट करने के लिए कोई डेटा नहीं मिला।")


def main():
    print("🚀 Daily Update Script Started (Bhavcopy Cache Source)...")

    fetched_date = update_cache_with_latest()
    if fetched_date is None:
        print("❌ आज का data नहीं मिल पाया — script रुक रही है (पुराना cache untouched है)")
        exit(1)

    cache_df = pd.read_csv(CACHE_FILE)
    print(f"ℹ️ Cache में कुल {cache_df['symbol'].nunique()} symbols, {len(cache_df)} rows हैं")

    client = get_google_client()

    spreadsheet_id = "1h5DL7tnrNnukH_EzfteoePDSRyfuICdXC3SB367tfEQ"

    try:
        ws_volume = client.open_by_key(spreadsheet_id).worksheet("Top 250 Stocks")
        ws_turnover = client.open_by_key(spreadsheet_id).worksheet("Top 250 Turnover")
        print("✅ दोनों टैब्स से कनेक्शन सफल!")
    except Exception as e:
        print(f"❌ Sheet कनेक्ट करने में Error: {e}")
        exit(1)

    process_sheet(ws_volume, "Volume (Top 250 Stocks)", cache_df)
    process_sheet(ws_turnover, "Turnover (Top 250 Turnover)", cache_df)

    print("\n🎉 सारे टैब्स सफलतापूर्वक अपडेट हो गए!")


if __name__ == "__main__":
    main()
