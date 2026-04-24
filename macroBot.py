import pandas as pd
import numpy as np
import time
import os
import hmac
import hashlib
import base64
import requests
from datetime import datetime
from dotenv import load_dotenv 

load_dotenv()

# ==========================================
# 1. PARAMETRI BOT MACRO (BTC DAILY)
# ==========================================
API_KEY = os.getenv('API_KEY') or 'LA_TUA_API_KEY'
SECRET_KEY = os.getenv('SECRET_KEY') or 'IL_TUO_SECRET_KEY'
PASSPHRASE = os.getenv('PASSPHRASE') or 'LA_TUA_PASSPHRASE'

SYMBOL = 'BTCUSDT'           
PRODUCT_TYPE = 'SPOT'        # Operiamo su Spot, NESSUN FUNDING RATE!
TIMEFRAME = '1day'             # Una candela al giorno
SMA_FAST = 7
SMA_SLOW = 40
SMA_TREND = 100
STOP_LOSS_PCT = 0.05         # -5% Hard Stop Loss per massimizzare i profitti

# Colori UI
C_GREEN = '\033[92m'
C_RED = '\033[91m'
C_CYAN = '\033[96m'
C_YELLOW = '\033[93m'
C_RESET = '\033[0m'

# ==========================================
# 2. FUNZIONI API E DATI
# ==========================================
def bitget_request(method, endpoint, params=None, body=None):
    base_url = "https://api.bitget.com"
    timestamp = str(int(time.time() * 1000))
    path = endpoint
    if params:
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        path = f"{endpoint}?{query_string}"
        
    body_str = ""
    if body:
        import json
        body_str = json.dumps(body)
        
    message = timestamp + method + path + body_str
    mac = hmac.new(bytes(SECRET_KEY, 'utf-8'), bytes(message, 'utf-8'), hashlib.sha256)
    sign = base64.b64encode(mac.digest()).decode('utf-8')
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': sign,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }
    
    try:
        url = base_url + path
        if method == 'GET':
            resp = requests.get(url, headers=headers)
        else:
            resp = requests.post(url, headers=headers, data=body_str)
        return resp.json()
    except Exception as e:
        print(f"{C_RED}[!] Errore di rete Bitget: {e}{C_RESET}")
        return None

def get_daily_candles():
    """Scarica le ultime 200 candele daily per calcolare le medie in sicurezza"""
    res = bitget_request('GET', '/api/v2/spot/market/candles', params={
        'symbol': SYMBOL, 'granularity': TIMEFRAME, 'limit': 200
    })
    
    if res and res.get('code') == '00000':
        data = res.get('data', [])
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'base_volume', 'quote_volume', 'usdt_volume'])
        df = df.astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df
    else:
        # Se Bitget risponde con un errore, ce lo facciamo dire chiaro e tondo
        msg = res.get('msg') if res else "Nessuna risposta dal server"
        print(f"{C_RED}[!] Errore API Candele: {msg}{C_RESET}")
        return None

def get_spot_balance(coin):
    """Restituisce il saldo disponibile di una determinata moneta sullo Spot"""
    res = bitget_request('GET', '/api/v2/spot/account/assets', params={'coin': coin})
    if res and res.get('code') == '00000':
        for asset in res.get('data', []):
            if asset.get('coin') == coin:
                return float(asset.get('available', 0))
    return 0.0

# ==========================================
# 3. MOTORE LOGICO E ORCHESTRATORE (ONE-SHOT)
# ==========================================
def run_macro_bot():
    print(f"{C_CYAN}╭────────────────────────────────────────────────────────────╮{C_RESET}")
    print(f"{C_CYAN}│{C_RESET} 🌍 BITGET MACRO BOT (SPOT) - BTCUSDT{C_RESET}                       {C_CYAN}│{C_RESET}")
    print(f"{C_CYAN}╰────────────────────────────────────────────────────────────╯{C_RESET}")
    
    df = get_daily_candles()
    if df is None or len(df) < SMA_TREND:
        print(f"{C_RED}[!] Impossibile scaricare abbastanza dati per la SMA {SMA_TREND}{C_RESET}")
        return
        
    # Calcolo Medie Mobili
    df['SMA_FAST'] = df['close'].rolling(window=SMA_FAST).mean()
    df['SMA_SLOW'] = df['close'].rolling(window=SMA_SLOW).mean()
    df['SMA_TREND'] = df['close'].rolling(window=SMA_TREND).mean()
    
    # Valori di ieri (Chiusura confermata) per calcolare il segnale
    ieri = df.iloc[-2]
    altro_ieri = df.iloc[-3]
    
    # Prezzo Attuale (Candela di oggi in formazione)
    oggi = df.iloc[-1]
    current_price = oggi['close']
    
    # LOGICA SEGNALI
    # Ieri ha incrociato a rialzo e il prezzo era sopra il trend?
    bullish_cross_yesterday = (ieri['SMA_FAST'] > ieri['SMA_SLOW']) and (ieri['close'] > ieri['SMA_TREND'])
    bullish_cross_day_before = (altro_ieri['SMA_FAST'] > altro_ieri['SMA_SLOW']) and (altro_ieri['close'] > altro_ieri['SMA_TREND'])
    
    # Se ieri è scattato il segnale (1) e l'altro ieri no (0) -> E' ORA DI COMPRARE
    buy_signal = bullish_cross_yesterday and not bullish_cross_day_before
    
    # Se ieri la veloce è scesa sotto la lenta OR il prezzo è rotto sotto il trend -> E' ORA DI VENDERE
    sell_signal = (ieri['SMA_FAST'] < ieri['SMA_SLOW']) or (ieri['close'] < ieri['SMA_TREND'])

    usdt_balance = get_spot_balance('USDT')
    btc_balance = get_spot_balance('BTC')
    
    btc_value_in_usdt = btc_balance * current_price
    is_in_position = btc_value_in_usdt > 50 # Consideriamo "in posizione" se abbiamo più di 50$ in BTC

    print(f"📊 Prezzo BTC : {current_price:.2f} $")
    print(f"📈 SMA 7      : {ieri['SMA_FAST']:.2f} $")
    print(f"📉 SMA 40     : {ieri['SMA_SLOW']:.2f} $")
    print(f"🏔️  SMA 100    : {ieri['SMA_TREND']:.2f} $\n")
    
    print(f"💰 Portafoglio: {usdt_balance:.2f} USDT | {btc_balance:.4f} BTC")
    print(f"🎯 Stato      : {'🟢 IN HOLD' if is_in_position else '⏳ IN ATTESA (CASH)'}\n")

    # ================= ESECUZIONE ORDINI =================
    if buy_signal and not is_in_position:
        print(f"{C_GREEN}🔥 SEGNALE BULLISH CONFERMATO! Preparazione Acquisto...{C_RESET}")
        if usdt_balance > 10:
            usdt_to_spend = usdt_balance * 0.99 # Usiamo il 99% per sicurezza fee
            print(f"💸 Investo: {usdt_to_spend:.2f} USDT a mercato.")
            # Chiamata API Ordine BUY MARKET:
            # bitget_request('POST', '/api/v2/spot/trade/place-order', body={
            #    "symbol": "BTCUSDT", "side": "buy", "orderType": "market", "quoteAmount": str(round(usdt_to_spend, 2))
            # })
            
            # Subito dopo calcoliamo e piazziamo l'ordine OCO (Stop Loss al -5%)
            stop_price = current_price * (1 - STOP_LOSS_PCT)
            print(f"🛡️ Piazzerò un ordine di Stop Loss a: {stop_price:.2f} $ (-5%)")
            # bitget_request('POST', '/api/v2/spot/trade/place-plan-order', body={...})
        else:
            print(f"{C_RED}Saldo insufficiente per acquistare.{C_RESET}")
            
    elif sell_signal and is_in_position:
        print(f"{C_YELLOW}⚠️ SEGNALE BEARISH / TREND ROTTO! Chiusura Posizione...{C_RESET}")
        print(f"📦 Vendo: {btc_balance:.4f} BTC a mercato per tornare in USDT.")
        # Chiamata API Ordine SELL MARKET:
        # bitget_request('POST', '/api/v2/spot/trade/place-order', body={
        #    "symbol": "BTCUSDT", "side": "sell", "orderType": "market", "baseAmount": str(btc_balance)
        # })
        
    else:
        print(f"💤 Nessuna azione richiesta oggi. Ci vediamo domani a mezzanotte.")

if __name__ == "__main__":
    run_macro_bot()