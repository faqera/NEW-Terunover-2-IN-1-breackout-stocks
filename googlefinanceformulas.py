import gspread
from oauth2client.service_account import ServiceAccountCredentials
import yfinance as yf
import time
import os
import json

# ---------- Google Sheets Authentication ----------
def get_google_sheet():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv('GCP_CREDENTIALS')
        if not creds_json:
            raise Exception("GCP_CREDENTIALS environment variable not set.")
        
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        spreadsheet_id = "1h5DL7tnrNnukH_EzfteoePDSRyfuICdXC3SB367tfEQ"  # ⚠️ अपनी Sheet ID यहाँ डालें
        sheet = client.open_by_key(spreadsheet_id)
        worksheet = sheet.worksheet("Top 250 Stocks")
        return worksheet
    except Exception as e:
        print(f"❌ Google Sheet Connection Error: {e}")
        exit(1)

# ---------- Technical Indicators Calculation ----------
def calculate_indicators(symbol, close_price):
    """
    symbol: NSE Code (जैसे 'RELIANCE')
    close_price: Sheet से लिया गया CMP (कॉलम C)
    Return: (cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating)
    """
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="1y")  # 1 साल का डेटा
        
        if hist.empty:
            return close_price, None, None, None, "Data Error", None, "TICKER NOT FOUND"
        
        # ---------- 1. DMA (50, 100, 200) निकालें ----------
        closes = hist['Close']
        if len(closes) < 200:
            dma_50 = closes.tail(50).mean() if len(closes) >= 50 else None
            dma_100 = closes.tail(100).mean() if len(closes) >= 100 else None
            dma_200 = None
        else:
            dma_50 = closes.tail(50).mean()
            dma_100 = closes.tail(100).mean()
            dma_200 = closes.tail(200).mean()
        
        cmp = close_price  # Sheet वाला CMP
        
        # ---------- 2. Bull Run Check और DMA Distance ----------
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
        
        # ---------- 3. CAR Rating (Complex - ब्लॉग के फॉर्मूले के अनुसार) ----------
        # STEP A: सबसे बड़ी High वाली Date ढूँढें
        if 'High' in hist.columns and not hist['High'].isna().all():
            max_high_date = hist['High'].idxmax()
            # STEP B: उस Date से आज तक का Close डेटा लें
            hist_slice = hist.loc[max_high_date:]
            prices = hist_slice['Close'].tolist()
        else:
            prices = closes.tolist()  # Fallback
        
        # STEP C: अगर प्राइसेस 10 से कम हैं, तो "Short History"
        if len(prices) < 10:
            car_rating = "Short History"
        else:
            # STEP D: Cumulative Average (SCAN) निकालें
            cum_avg = []
            cumulative_sum = 0
            for i, price in enumerate(prices, 1):
                cumulative_sum += price
                cum_avg.append(cumulative_sum / i)
            
            # STEP E: आखिरी 10 Cumulative Averages लें
            last_10 = cum_avg[-10:]
            
            # STEP F: चेक करें कि क्या हर बार नई > पुरानी है?
            # मतलब last_10[1] > last_10[0], last_10[2] > last_10[1] ... (कुल 9 चेक)
            checks = 0
            for i in range(1, 10):  # i = 1 to 9
                if last_10[i] > last_10[i-1]:
                    checks += 1
            
            # STEP G: अगर सभी 9 चेक सही हैं, तो "Buy/Average Out" वरना "Avoid/Hold"
            if checks == 9:
                car_rating = "Buy/Average Out"
            else:
                car_rating = "Avoid/Hold"
        
        return cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating
        
    except Exception as e:
        print(f"⚠️ {symbol} के लिए Error: {e}")
        return close_price, None, None, None, "Error", None, "TICKER NOT FOUND"

# ---------- Main Execution ----------
def main():
    print("🚀 Technical Indicators Script Started (with complex CAR logic)...")
    
    worksheet = get_google_sheet()
    all_data = worksheet.get_all_values()
    
    if len(all_data) < 2:
        print("❌ Sheet में कोई डेटा नहीं मिला। पहले update_sheet.py चलाएँ।")
        return
    
    headers = all_data[0]
    rows = all_data[1:]
    
    updated_rows = []
    print(f"📊 कुल {len(rows)} स्टॉक्स प्रोसेस हो रहे हैं...")
    
    for idx, row in enumerate(rows, start=2):
        if len(row) < 3 or not row[0].strip():
            continue
        
        symbol = row[0].strip()
        try:
            close_price = float(row[2])
        except (ValueError, IndexError):
            print(f"⚠️ Row {idx}: '{row[0]}' का Close Price सही नहीं है, स्किप कर रहे हैं")
            continue
        
        cmp, dma_50, dma_100, dma_200, bull_status, dma_dist, car_rating = calculate_indicators(symbol, close_price)
        
        # D से J कॉलम तक (कुल 7 कॉलम)
        updated_row = [
            cmp if cmp is not None else "",
            dma_50 if dma_50 is not None else "",
            dma_100 if dma_100 is not None else "",
            dma_200 if dma_200 is not None else "",
            bull_status,
            dma_dist if dma_dist is not None else "",
            car_rating
        ]
        
        updated_rows.append({
            "range": f"D{idx}:J{idx}",
            "values": [updated_row]
        })
        
        time.sleep(0.3)
        
        if idx % 50 == 0:
            print(f"⏳ {idx-1} स्टॉक्स प्रोसेस हो चुके हैं...")
    
    if updated_rows:
        print("💾 Sheet में डेटा अपडेट हो रहा है...")
        for update in updated_rows:
            worksheet.update(update["range"], update["values"], value_input_option='USER_ENTERED')
        print("✅ D से J कॉलम सफलतापूर्वक अपडेट हो गए!")
    else:
        print("❌ अपडेट करने के लिए कोई डेटा नहीं मिला।")

if __name__ == "__main__":
    main()
