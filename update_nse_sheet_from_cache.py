def fetch_live_ltp_all(symbols, security_id_map, headers):
    live_prices = {}
    tasks = []
    for sym in symbols:
        ex_map = security_id_map.get(sym, {})
        exchange = "NSE" if "NSE" in ex_map else ("BSE" if "BSE" in ex_map else None)
        if exchange:
            tasks.append((sym, exchange, ex_map[exchange]))

    print(f"📡 {len(tasks)}/{len(symbols)} symbols के लिए security ID मिला, LTP मंगा रहे हैं ({LTP_MAX_WORKERS} parallel)...")

    failed_symbols = []  # फेल हुए स्टॉक्स को यहाँ स्टोर करेंगे

    # --- पहला राउंड (तेज, LTP_MAX_WORKERS के साथ) ---
    with ThreadPoolExecutor(max_workers=LTP_MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one_ltp, headers, sym, exch, sec_id): (sym, exch, sec_id) for sym, exch, sec_id in tasks}
        done_count = 0
        for future in as_completed(futures):
            sym, price = future.result()
            if price is not None:
                live_prices[sym] = price
            else:
                failed_symbols.append(sym)  # फेल हुए को नोट करें
            done_count += 1
            if done_count % 50 == 0:
                print(f"⏳ {done_count}/{len(tasks)} LTP calls पूरी हुईं...")

    print(f"✅ पहले राउंड में {len(live_prices)}/{len(symbols)} symbols मिले, {len(failed_symbols)} फेल हुए।")

    # --- दूसरा राउंड (धीमा, सिर्फ फेल हुए स्टॉक्स के लिए) ---
    if failed_symbols:
        print(f"🔄 {len(failed_symbols)} फेल हुए स्टॉक्स को धीमी स्पीड (1 worker + 0.3s delay) से दोबारा मंगा रहे हैं...")
        time.sleep(2)  # API को थोड़ा ब्रेक दें
        retry_success = 0
        with ThreadPoolExecutor(max_workers=1) as executor:  # सिर्फ 1 worker
            futures = []
            for sym in failed_symbols:
                ex_map = security_id_map.get(sym, {})
                exchange = "NSE" if "NSE" in ex_map else ("BSE" if "BSE" in ex_map else None)
                if exchange:
                    futures.append(executor.submit(fetch_one_ltp_with_delay, headers, sym, exchange, ex_map[exchange], 0.3))
            for future in as_completed(futures):
                sym, price = future.result()
                if price is not None:
                    live_prices[sym] = price
                    retry_success += 1
        print(f"✅ Retry के बाद {retry_success} और symbols मिल गए, कुल {len(live_prices)}/{len(symbols)} हो गए।")

    print(f"✅ अंत में {len(live_prices)}/{len(symbols)} symbols का live LTP मिल गया (बाकी bhavcopy close पर fallback करेंगे)")
    return live_prices
