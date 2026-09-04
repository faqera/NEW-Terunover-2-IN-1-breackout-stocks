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

# ---------- Technical Indicators Calculation ----------
def calculate_indicators(symbol, close_price):
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="1y")
        
        if hist.empty:
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
              dma_dist is not None and dma_dist <= -0.01):
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
        
        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating = calculate_indicators(symbol, close_price)
        
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
            print(f"⏳ {sheet_name}: {idx-1}/{total_rows} स्टॉक्स प्रोसेस हो चुके हैं...")
        
        time.sleep(0.05)
    
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

# ---------- Main Execution ----------
def main():
    print("🚀 Technical Indicators Script Started (Both Tabs)...")
    
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
    
    # पहले Volume वाला टैब
    process_sheet(ws_volume, "Volume (Top 250 Stocks)")
    
    # फिर Turnover वाला टैब
    process_sheet(ws_turnover, "Turnover (Top 250 Turnover)")
    
    print("\n🎉 सारे टैब्स सफलतापूर्वक अपडेट हो गए!")

if __name__ == "__main__":
    main()
