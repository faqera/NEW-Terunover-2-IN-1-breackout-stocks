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

# ---------- Main Execution with Batch Update ----------
def main():
    print("🚀 Technical Indicators Script Started (Batch Update)...")
    
    worksheet = get_google_sheet()
    all_data = worksheet.get_all_values()
    
    if len(all_data) < 2:
        print("❌ Sheet में कोई डेटा नहीं मिला। पहले update_sheet.py चलाएँ।")
        return
    
    rows = all_data[1:]  # Row 2 से आगे
    total_rows = len(rows)
    
    # हम D2:J251 (या जितनी पंक्तियाँ हैं) के लिए एक 2D लिस्ट बनाएँगे
    # Index 0 → Row 2, Index 1 → Row 3, ... Index (total_rows-1) → अंतिम Row
    batch_values = []
    
    print(f"📊 कुल {total_rows} स्टॉक्स प्रोसेस हो रहे हैं...")
    
    for idx, row in enumerate(rows, start=2):  # idx = actual row number in sheet
        if len(row) < 3 or not row[0].strip():
            # अगर A, B, C नहीं हैं तो खाली रखें
            batch_values.append(["", "", "", "", "", "", ""])
            continue
        
        symbol = row[0].strip()
        try:
            close_price = float(row[2])
        except (ValueError, IndexError):
            print(f"⚠️ Row {idx}: '{row[0]}' का Close Price सही नहीं है, स्किप कर रहे हैं")
            batch_values.append(["", "", "", "", "", "", ""])
            continue
        
        # इंडिकेटर्स निकालें
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
            print(f"⏳ {idx-1} स्टॉक्स प्रोसेस हो चुके हैं...")
        
        # API Rate Limit से बचने के लिए थोड़ा Delay (लेकिन अब यह ज़्यादा मायने नहीं रखता)
        time.sleep(0.1)
    
    # ---------- Batch Update (एक ही API Call) ----------
    if batch_values:
        # हमें D2 से शुरू करना है, और कुल पंक्तियाँ = len(batch_values)
        # रेंज: D2 से J{len(batch_values)+1} तक
        end_row = 2 + len(batch_values) - 1
        range_name = f"D2:J{end_row}"
        
        print(f"💾 {len(batch_values)} पंक्तियों को एक साथ अपडेट किया जा रहा है (रेंज: {range_name})...")
        
        try:
            # gspread के नए version में argument order: values, range_name
            # लेकिन हम named arguments का उपयोग करेंगे ताकि दोनों versions चलें
            worksheet.update(range_name=range_name, values=batch_values, value_input_option='USER_ENTERED')
            print("✅ D से J कॉलम सफलतापूर्वक अपडेट हो गए!")
        except Exception as e:
            print(f"❌ Batch Update में Error: {e}")
    else:
        print("❌ अपडेट करने के लिए कोई डेटा नहीं मिला।")

if __name__ == "__main__":
    main()
