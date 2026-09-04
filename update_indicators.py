import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import pandas as pd
import time
import os
import json
from datetime import datetime, timedelta

# ---------- NSE Session Setup ----------
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
}


def get_nse_session():
    """NSE cookies पाने के लिए पहले होमपेज/quote पेज खोलना ज़रूरी है,
    वरना historical API 401/403 देगा।"""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
        session.get("https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE", timeout=10)
        time.sleep(1)
    except Exception as e:
        print(f"⚠️ NSE session बनाने में दिक्कत: {e}")
    return session


def fetch_nse_history(session, symbol, days=380, retries=3):
    """NSE historical API से पिछले ~1 साल का daily close/high data लाता है।
    लौटाता है: (dataframe, session)  -- session refresh हो सकता है तो वापस भी करते हैं।"""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    url = "https://www.nseindia.com/api/historical/cm/equity"
    params = {
        "symbol": symbol,
        "series": '["EQ"]',
        "from": from_date.strftime("%d-%m-%Y"),
        "to": to_date.strftime("%d-%m-%Y"),
    }

    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data", [])
                if not data:
                    return None, session
                rows = []
                for d in data:
                    try:
                        rows.append({
                            "date": d.get("CH_TIMESTAMP"),
                            "close": float(d.get("CH_CLOSING_PRICE")),
                            "high": float(d.get("CH_TRADE_HIGH_PRICE")),
                        })
                    except (TypeError, ValueError):
                        continue
                if not rows:
                    return None, session
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
                return df, session
            elif resp.status_code in (401, 403, 429):
                # cookies expire हो गए या rate-limit लगा — session refresh करके फिर कोशिश
                time.sleep(2)
                session = get_nse_session()
            else:
                time.sleep(1)
        except Exception as e:
            print(f"⚠️ {symbol} NSE fetch error (attempt {attempt + 1}): {e}")
            time.sleep(2)

    return None, session


# ---------- Google Sheets Authentication ----------
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


# ---------- Technical Indicators Calculation (अब NSE data से) ----------
def calculate_indicators(session, symbol, close_price):
    try:
        hist, session = fetch_nse_history(session, symbol)

        if hist is None or hist.empty:
            return close_price, None, None, None, "Data Error", None, "TICKER NOT FOUND", session

        closes = hist['close']

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
        if 'high' in hist.columns and not hist['high'].isna().all():
            max_high_idx = hist['high'].idxmax()
            hist_slice = hist.loc[max_high_idx:]
            prices = hist_slice['close'].tolist()
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

        return cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating, session

    except Exception as e:
        print(f"⚠️ {symbol} के लिए Error: {e}")
        return close_price, None, None, None, "Error", None, "TICKER NOT FOUND", session


# ---------- एक टैब को प्रोसेस करने वाला Function ----------
def process_sheet(worksheet, sheet_name, session):
    print(f"\n📊 {sheet_name} टैब प्रोसेस हो रहा है...")

    all_data = worksheet.get_all_values()

    if len(all_data) < 2:
        print(f"⚠️ {sheet_name} में कोई डेटा नहीं मिला, स्किप कर रहे हैं")
        return session

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

        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating, session = calculate_indicators(
            session, symbol, close_price
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

        # NSE rate-limit से बचने के लिए yfinance वाले 0.05s से ज़्यादा gap
        time.sleep(0.4)

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

    return session


# ---------- Main Execution ----------
def main():
    print("🚀 Technical Indicators Script Started (NSE data source, Both Tabs)...")

    client = get_google_client()

    # ⚠️ अपनी Sheet ID यहाँ डालें (वही ID जो update_sheet.py में है)
    spreadsheet_id = "1h5DL7tnrNnukH_EzfteoePDSRyfuICdXC3SB367tfEQ"

    try:
        ws_volume = client.open_by_key(spreadsheet_id).worksheet("Top 250 Stocks")
        ws_turnover = client.open_by_key(spreadsheet_id).worksheet("Top 250 Turnover")
        print("✅ दोनों टैब्स से कनेक्शन सफल!")
    except Exception as e:
        print(f"❌ Sheet कनेक्ट करने में Error: {e}")
        exit(1)

    # NSE session एक बार बनाकर पूरे run में reuse करेंगे
    session = get_nse_session()

    # पहले Volume वाला टैब
    session = process_sheet(ws_volume, "Volume (Top 250 Stocks)", session)

    # फिर Turnover वाला टैब
    session = process_sheet(ws_turnover, "Turnover (Top 250 Turnover)", session)

    print("\n🎉 सारे टैब्स सफलतापूर्वक अपडेट हो गए!")


if __name__ == "__main__":
    main()
