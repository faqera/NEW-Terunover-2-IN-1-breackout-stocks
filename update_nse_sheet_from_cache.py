# ============================================================
# 🔥 UPDATED: Multiple Google Apps Script Web Apps को Call करें
# ============================================================
def trigger_google_apps_script():
    """
    सभी Google Apps Script Web Apps को कॉल करके BOT SIGNAL जनरेट करवाता है
    """
    # दोनों Web App URLs
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
