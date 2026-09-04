"""
Backfill Script — एक बार चलानी है
----------------------------------
पिछले ~400 दिनों की NSE bhavcopy zip files एक-एक करके download करके
सारे EQ-series stocks का Date, Symbol, Close, High एक local cache CSV
(bhavcopy_history_cache.csv) में जमा करती है।

इस cache को बाद में update_nse_sheet_from_cache.py रोज़ इस्तेमाल करेगी
DMA/CAR calculate करने के लिए — बिना NSE historical API (जिसे cookies चाहिए)
या yfinance के, सिर्फ रोज़ की एक नई bhavcopy file download करके।

चलाने का तरीका: python backfill_bhavcopy_cache.py
(इसमें समय लगेगा — ~400 requests, हर एक के बीच थोड़ा gap)
"""

import requests
import zipfile
import io
import pandas as pd
import os
import time
from datetime import datetime, timedelta

CACHE_FILE = "bhavcopy_history_cache.csv"
BACKFILL_DAYS = 400  # calendar days पीछे तक जाएंगे (~260-270 trading days मिलेंगे)
FILTER_KEYWORDS = "BEES|ETF|GOLD|LIQUID|CASE|SILVER|LIQ"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def fetch_bhavcopy_for_date(date_obj):
    """किसी एक तारीख की bhavcopy zip download करके EQ-series rows का
    DataFrame (Date, Symbol, Close, High) लौटाती है। न मिले तो None।"""
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
            # कुछ पुराने formats में High column अलग नाम से हो सकता है — न मिले तो Close से भर दो
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


def main():
    print("🚀 Backfill शुरू हो रहा है... (इसमें समय लगेगा)")

    existing_dates = set()
    if os.path.exists(CACHE_FILE):
        existing = pd.read_csv(CACHE_FILE)
        existing_dates = set(existing["date"].unique())
        print(f"ℹ️ मौजूदा cache में {len(existing_dates)} दिनों का data पहले से है — वो दिन skip होंगे")

    all_frames = []
    today = datetime.now()
    found_days = 0
    checked_days = 0

    for i in range(BACKFILL_DAYS):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:  # Sat/Sun skip
            continue

        checked_days += 1
        date_key = day.strftime("%Y-%m-%d")

        if date_key in existing_dates:
            continue

        df_day = fetch_bhavcopy_for_date(day)
        if df_day is not None and not df_day.empty:
            all_frames.append(df_day)
            found_days += 1
            print(f"✅ {date_key}: {len(df_day)} stocks मिले ({found_days} दिन जमा हो चुके)")
        else:
            print(f"➖ {date_key}: file नहीं मिली (holiday या error) — skip")

        time.sleep(0.5)  # NSE को अच्छा rate रखने के लिए

    if not all_frames:
        print("\n⚠️ कोई नया data नहीं मिला (शायद cache पहले से पूरा है)।")
        return

    new_data = pd.concat(all_frames, ignore_index=True)

    if os.path.exists(CACHE_FILE):
        existing = pd.read_csv(CACHE_FILE)
        combined = pd.concat([existing, new_data], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date", "symbol"], keep="last")
    else:
        combined = new_data

    combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
    combined.to_csv(CACHE_FILE, index=False)

    print(f"\n🎉 Backfill पूरा हुआ! कुल {checked_days} दिन चेक किए, {found_days} नए दिन जोड़े।")
    print(f"💾 Cache file: {CACHE_FILE} ({len(combined)} rows, {combined['symbol'].nunique()} unique symbols)")


if __name__ == "__main__":
    main()
