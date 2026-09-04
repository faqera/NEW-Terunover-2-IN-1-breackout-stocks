import gspread
from oauth2client.service_account import ServiceAccountCredentials
import yfinance as yf
import time
import os
import json

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

# ---------- FIX #3: Per-Symbol Data Cache ----------
# एक ही स्टॉक अगर Volume और Turnover दोनों टैब में है, तो yfinance से
# सिर्फ एक ही बार डेटा लिया जाएगा और cache से reuse होगा। इससे:
# 1. एक ही स्टॉक के दोनों टैब में अलग-अलग CAR/DMA आने वाली inconsistency खत्म
# 2. yfinance API calls लगभग आधी हो जाती हैं
yf_history_cache = {}

def get_history(symbol):
    """yfinance history को cache करके देता है ताकि एक ही स्टॉक के लिए
    बार-बार अलग-अलग समय पर API call न हो (जो हल्के-हल्के अलग data देता है)"""
    if symbol in yf_history_cache:
        return yf_history_cache[symbol]
    
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        # auto_adjust=False -> raw/actual prices (GOOGLEFINANCE जैसा behavior)
        hist = ticker.history(period="1y", auto_adjust=False)
    except Exception as e:
        print(f"⚠️ {symbol} डेटा लाने में Error: {e}")
        hist = None
    
    yf_history_cache[symbol] = hist
    return hist

# ---------- Technical Indicators Calculation ----------
def calculate_indicators(symbol, close_price):
    """
    symbol: NSE Code (जैसे 'RELIANCE')
    close_price: Sheet से लिया गया CMP (कॉलम C)
    Return: (cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating)
    """
    try:
        hist = get_history(symbol)
        
        if hist is None or hist.empty:
            return close_price, None, None, None, "Data Error", None, "TICKER NOT FOUND"
        
        closes = hist['Close']
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
              # FIX #1: लोअर बाउंड जोड़ी गई (मूल formula: I2>=-10 AND I2<=-0.01)
              dma_dist is not None and -10 <= dma_dist <= -0.01):
            bull_status = "In Bear Run"
        else:
            bull_status = "Unconfirmed"
        
        # ---------- CAR Rating ----------
        if 'High' in hist.columns and not hist['High'].isna().all():
            max_high_date = hist['High'].idxmax()
            hist_slice = hist.loc[max_high_date:]
            prices = hist_slice['Close'].tolist()
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
                if last_10[i] > last_10[i-1]:
                    checks += 1
            
            car_rating = "Buy/Average Out" if checks == 9 else "Avoid/Hold"
        
        return cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating
        
    except Exception as e:
        print(f"⚠️ {symbol} के लिए Error: {e}")
        return close_price, None, None, None, "Error", None, "TICKER NOT FOUND"

# ---------- एक टैब को प्रोसेस करने वाला Function ----------
def process_sheet(worksheet, sheet_name):
    """
    एक Google Sheet टैब को पढ़ेगा, D से J कैलकुलेट करेगा, और Batch Update करेगा
    """
    print(f"\n📊 {sheet_name} टैब प्रोसेस हो रहा है...")
    
    all_data = worksheet.get_all_values()
    
    if len(all_data) < 2:
        print(f"⚠️ {sheet_name} में कोई डेटा नहीं मिला, स्किप कर रहे हैं")
        return
    
    rows = all_data[1:]  # Row 2 से आगे
    total_rows = len(rows)
    
    # Batch Update के लिए 2D लिस्ट
    batch_values = []
    
    for idx, row in enumerate(rows, start=2):  # idx = actual row number in sheet
        # अगर A, B, C में से कोई खाली है तो स्किप करें
        if len(row) < 3 or not row[0].strip():
            batch_values.append(["", "", "", "", "", "", ""])
            continue
        
        symbol = row[0].strip()
        try:
            close_price = float(row[2])  # C कॉलम
        except (ValueError, IndexError):
            print(f"⚠️ {sheet_name} Row {idx}: '{row[0]}' का Close Price सही नहीं है")
            batch_values.append(["", "", "", "", "", "", ""])
            continue
        
        # इंडिकेटर्स निकालें (cache से, अगर पहले भी fetch हो चुका हो)
        was_cached = symbol in yf_history_cache
        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating = calculate_indicators(symbol, close_price)
        
        # D से J कॉलम (7 कॉलम)
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
        
        # Progress दिखाएँ
        if (idx - 1) % 50 == 0:
            print(f"⏳ {sheet_name}: {idx-1}/{total_rows} स्टॉक्स प्रोसेस हो चुके हैं...")
        
        # Rate Limit से बचने के लिए थोड़ा Delay — सिर्फ तभी जब असल में नया API call हुआ हो
        if not was_cached:
            time.sleep(0.1)
    
    # ---------- Batch Update (एक ही API Call) ----------
    if batch_values:
        end_row = 2 + len(batch_values) - 1
        range_name = f"D2:J{end_row}"
        
        print(f"💾 {sheet_name}: {len(batch_values)} पंक्तियों को अपडेट किया जा रहा है (रेंज: {range_name})...")
        
        try:
            worksheet.update(range_name=range_name, values=batch_values, value_input_option='USER_ENTERED')
            print(f"✅ {sheet_name}: D से J कॉलम सफलतापूर्वक अपडेट हो गए!")
        except Exception as e:
            print(f"❌ {sheet_name} Batch Update में Error: {e}")
    else:
        print(f"❌ {sheet_name}: अपडेट करने के लिए कोई डेटा नहीं मिला।")

# ---------- Main Execution ----------
def main():
    print("🚀 Technical Indicators Script Started (Both Tabs)...")
    
    client = get_google_client()
    
    spreadsheet_id = "1h5DL7tnrNnukH_EzfteoePDSRyfuICdXC3SB367tfEQ"  # NEW sheet ID
    
    try:
        # दोनों टैब कनेक्ट करें
        ws_volume = client.open_by_key(spreadsheet_id).worksheet("Top 250 Stocks")
        ws_turnover = client.open_by_key(spreadsheet_id).worksheet("Top 250 Turnover")
        print("✅ दोनों टैब्स से कनेक्शन सफल!")
    except Exception as e:
        print(f"❌ Sheet कनेक्ट करने में Error: {e}")
        exit(1)
    
    # पहले Volume वाला टैब प्रोसेस करें
    process_sheet(ws_volume, "Volume (Top 250 Stocks)")
    
    # फिर Turnover वाला टैब प्रोसेस करें (common stocks cache से आएँगे — दोबारा fetch नहीं होगा)
    process_sheet(ws_turnover, "Turnover (Top 250 Turnover)")
    
    print(f"\n🎉 सारे टैब्स सफलतापूर्वक अपडेट हो गए! (कुल {len(yf_history_cache)} unique symbols fetch किए गए)")

if __name__ == "__main__":
    main()
