"""
Daily Update Script — रोज़ चलानी है (UPDATED: live CMP अब INDstocks से, exact
auth/lookup pattern main1.py से लिया गया है)
------------------------------------------------------------------------
1. Bhavcopy cache अपडेट करता है (DMA history के लिए)
2. INDstocks से हर स्टॉक का live LTP लाता है (10 parallel workers, हर स्टॉक
   के लिए अलग API call — batch endpoint उपलब्ध नहीं है)
3. Cache (DMA history) + live CMP मिलाकर DMA/CAR/OUTPUT calculate करता है
4. दोनों टैब्स के D:J कॉलम अपडेट करता है
5. दोनों Web Apps को कॉल करके BOT SIGNAL जनरेट करवाता है
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import zipfile
import io
import csv
import pandas as pd
import os
import json
import time
import pyotp
import tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONSTANTS
# ============================================================
CACHE_FILE = "bhavcopy_history_cache.csv"
FILTER_KEYWORDS = "BEES|ETF|GOLD|LIQUID|CASE|SILVER|LIQ"
SPREADSHEET_ID = "1h5DL7tnrNnukH_EzfteoePDSRyfuICdXC3SB367tfEQ"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# --- INDstocks config — main1.py जैसा ही exact ---
IND_TOTP_SECRET = os.getenv("IND_TOTP_SECRET")
IND_CLIENT_ID = os.getenv("IND_CLIENT_ID")
IND_MPIN = os.getenv("IND_MPIN")

SECURITY_ID_CACHE_FILE = "indstocks_security_id_cache.json"
LTP_MAX_WORKERS = 10  # parallel LTP calls (balance: speed vs rate-limit)


# ============================================================
# INDSTOCKS AUTH — main1.py से exact copy
# ============================================================
def generate_new_token():
    totp = pyotp.TOTP(IND_TOTP_SECRET)
    code = totp.now()
    resp = requests.post(
        "https://api.indstocks.com/generate/token",
        headers={"x-api-key": IND_CLIENT_ID, "Content-Type": "application/json"},
        json={"mpin": IND_MPIN, "totp": code}
    )
    if resp.status_code != 200:
        raise Exception(f"Token generation failed: {resp.status_code} - {resp.text}")
    data = resp.json()
    if "token" not in data:
        raise Exception(f"Token missing: {data}")
    return data["token"]


def get_indstocks_headers():
    """हर run में एक बार नया token लेकर headers बना देता है (main1.py जैसा — force_refresh हमेशा)"""
    token = generate_new_token()
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "source": "WEB"
    }


# ============================================================
# STEP 0: Security ID lookup (main1.py का get_security_id_and_exchange पैटर्न,
# यहां एक बार में सारे symbols के लिए mapping बना रहे हैं ताकि 500 बार
# /market/instruments न बुलाना पड़े)
# ============================================================
def build_security_id_map(headers):
    if os.path.exists(SECURITY_ID_CACHE_FILE):
        age = time.time() - os.path.getmtime(SECURITY_ID_CACHE_FILE)
        if age < 20 * 3600:  # 20 घंटे तक cache reuse (instruments रोज़ नहीं बदलते)
            with open(SECURITY_ID_CACHE_FILE) as f:
                return json.load(f)

    print("📥 Security ID map (/market/instruments) से बना रहे हैं...")
    resp = requests.get(
        "https://api.indstocks.com/market/instruments",
        headers=headers,
        params={"source": "equity"},
        timeout=30
    )
    if resp.status_code != 200:
        print(f"❌ Instruments API failed: {resp.status_code}")
        return {}

    reader = csv.DictReader(io.StringIO(resp.text))
    mapping = {}  # symbol -> {"NSE": sec_id, "BSE": sec_id}
    for row in reader:
        sym = str(row.get("TRADING_SYMBOL", row.get("SYMBOL", ""))).strip().upper()
        exch = str(row.get("EXCH", "")).strip().upper()
        sec_id = str(row.get("SECURITY_ID", "")).strip()
        if not sym or not sec_id:
            continue
        exch_key = "NSE" if exch.startswith("NSE") else ("BSE" if exch.startswith("BSE") else exch)
        mapping.setdefault(sym, {})[exch_key] = sec_id

    with open(SECURITY_ID_CACHE_FILE, "w") as f:
        json.dump(mapping, f)

    print(f"✅ {len(mapping)} symbols का security ID map बन गया")
    return mapping


# ============================================================
# STEP 0.5: Live LTP लाना (parallel, एक call = एक scrip-code — batch सपोर्ट नहीं है)
# ============================================================
def fetch_one_ltp(headers, symbol, exchange, security_id):
    scrip_code = f"{exchange}_{security_id}"
    try:
        resp = requests.get(
            "https://api.indstocks.com/market/quotes/ltp",
            headers=headers,
            params={"scrip-codes": scrip_code},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            item = data.get(scrip_code)
            if item and item.get("live_price") is not None:
                return symbol, float(item["live_price"])
    except Exception:
        pass
    return symbol, None


def fetch_live_ltp_all(symbols, security_id_map, headers):
    live_prices = {}
    tasks = []
    for sym in symbols:
        ex_map = security_id_map.get(sym, {})
        # NSE को प्राथमिकता, फिर BSE
        exchange = "NSE" if "NSE" in ex_map else ("BSE" if "BSE" in ex_map else None)
        if exchange:
            tasks.append((sym, exchange, ex_map[exchange]))

    print(f"📡 {len(tasks)}/{len(symbols)} symbols के लिए security ID मिला, LTP मंगा रहे हैं ({LTP_MAX_WORKERS} parallel)...")

    with ThreadPoolExecutor(max_workers=LTP_MAX_WORKERS) as executor:
        futures = [
            executor.submit(fetch_one_ltp, headers, sym, exch, sec_id)
            for sym, exch, sec_id in tasks
        ]
        done_count = 0
        for future in as_completed(futures):
            sym, price = future.result()
            if price is not None:
                live_prices[sym] = price
            done_count += 1
            if done_count % 50 == 0:
                print(f"⏳ {done_count}/{len(tasks)} LTP calls पूरी हुईं...")

    print(f"✅ {len(live_prices)}/{len(symbols)} symbols का live LTP मिल गया (बाकी bhavcopy close पर fallback करेंगे)")
    return live_prices


# ============================================================
# STEP 1: bhavcopy cache अपडेट (पुराना जैसा ही, DMA history के लिए)
# ============================================================
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
            print(f"ℹ️ {date_key} पहले से cache में है")
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


# ============================================================
# STEP 2: DMA/CAR calculate करना (अब live_cmp पैरामीटर के साथ)
# ============================================================
def calculate_indicators_from_cache(cache_df, symbol, bhavcopy_close, live_cmp=None):
    hist = cache_df[cache_df["symbol"] == symbol].sort_values("date")
    if hist.empty:
        return bhavcopy_close, None, None, None, "Data Error", None, "TICKER NOT FOUND"

    closes = hist["close"]
    cmp = live_cmp if live_cmp is not None else bhavcopy_close

    if len(closes) < 200:
        dma_50 = closes.tail(50).mean() if len(closes) >= 50 else None
        dma_100 = closes.tail(100).mean() if len(closes) >= 100 else None
        dma_200 = None
    else:
        dma_50 = closes.tail(50).mean()
        dma_100 = closes.tail(100).mean()
        dma_200 = closes.tail(200).mean()

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
        checks = sum(1 for i in range(1, 10) if last_10[i] > last_10[i - 1])
        car_rating = "Buy/Average Out" if checks == 9 else "Avoid/Hold"

    return cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating


# ============================================================
# STEP 3: Google Sheets सेटअप और अपडेट
# ============================================================
def get_google_client():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv('GCP_CREDENTIALS')
        if not creds_json:
            raise Exception("GCP_CREDENTIALS environment variable not set.")
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"❌ Google Sheet Connection Error: {e}")
        exit(1)


def process_sheet(worksheet, sheet_name, cache_df, live_prices):
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

        symbol = row[0].strip().upper()
        try:
            bhavcopy_close = float(row[2])
        except (ValueError, IndexError):
            print(f"⚠️ {sheet_name} Row {idx}: '{row[0]}' का Close Price सही नहीं है")
            batch_values.append(["", "", "", "", "", "", ""])
            continue

        live_cmp = live_prices.get(symbol)

        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating = calculate_indicators_from_cache(
            cache_df, symbol, bhavcopy_close, live_cmp
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


# ============================================================
# STEP 4: Google Apps Script Web Apps को Call करना
# ============================================================
def trigger_google_apps_script():
    web_app_urls = [
        "https://script.google.com/macros/s/AKfycbxB2vJdV2nE7kfmILJapdSqoz0zZK6UZQer-S7UrH9EZ30PB2zbOS9e260i3hzw-m57/exec",
        "https://script.google.com/macros/s/AKfycbwq9NyMCGqvpHesCueX_JfHPRCa-x4E7XTIOKbf1XldEpPcxHqrj-VVzmcocplK6YN2/exec"
    ]
    payload = {"action": "generate_signal"}
    for idx, url in enumerate(web_app_urls, 1):
        try:
            print(f"📡 Web App #{idx} को कॉल कर रहे हैं...")
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Web App #{idx} Response:", result.get("message", "Success"))
            else:
                print(f"❌ Web App #{idx} Error. Status: {response.status_code}")
                print("Response:", response.text)
        except Exception as e:
            print(f"⚠️ Web App #{idx} Exception: {e}")
    print("✅ सभी Web Apps को कॉल करने की प्रक्रिया पूरी हुई!")


# ============================================================
# STEP 5: MAIN
# ============================================================
def main():
    print("🚀 Daily Update Script Started (Bhavcopy Cache + Live LTP)...")

    fetched_date = update_cache_with_latest()
    if fetched_date is None:
        print("❌ आज का data नहीं मिल पाया — script रुक रही है")
        exit(1)

    cache_df = pd.read_csv(CACHE_FILE)
    print(f"ℹ️ Cache में कुल {cache_df['symbol'].nunique()} symbols, {len(cache_df)} rows हैं")

    client = get_google_client()
    try:
        ws_volume = client.open_by_key(SPREADSHEET_ID).worksheet("Top 250 Stocks")
        ws_turnover = client.open_by_key(SPREADSHEET_ID).worksheet("Top 250 Turnover")
        print("✅ दोनों टैब्स से कनेक्शन सफल!")
    except Exception as e:
        print(f"❌ Sheet कनेक्ट करने में Error: {e}")
        exit(1)

    all_symbols = set()
    for ws in [ws_volume, ws_turnover]:
        col_a = ws.col_values(1)[1:]
        all_symbols.update(s.strip().upper() for s in col_a if s.strip())

    print(f"\n📡 {len(all_symbols)} unique symbols के लिए live LTP मंगा रहे हैं (INDstocks)...")
    try:
        ind_headers = get_indstocks_headers()
        security_id_map = build_security_id_map(ind_headers)
        live_prices = fetch_live_ltp_all(list(all_symbols), security_id_map, ind_headers)
    except Exception as e:
        print(f"⚠️ INDstocks live LTP fetch पूरी तरह fail हुई ({e}) — सब bhavcopy close पर fallback करेंगे")
        live_prices = {}

    process_sheet(ws_volume, "Volume (Top 250 Stocks)", cache_df, live_prices)
    process_sheet(ws_turnover, "Turnover (Top 250 Turnover)", cache_df, live_prices)

    print("\n🎉 सारे टैब्स सफलतापूर्वक अपडेट हो गए!")
    print("\n📡 अब BOT SIGNAL जनरेट करने के लिए सभी Web Apps को कॉल कर रहे हैं...")
    trigger_google_apps_script()


if __name__ == "__main__":
    main()
