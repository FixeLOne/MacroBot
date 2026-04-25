"""
₿ BTC Macro Investor — Bitget SPOT
Strategia SMA 7/40/100 con dashboard Live aggiornata al secondo.

Dipendenze: pip install rich pandas numpy requests python-dotenv
"""

import pandas as pd
import numpy as np
import time, os, hmac, hashlib, base64, requests, json, math, logging, threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

from rich.console   import Console
from rich.table     import Table
from rich.panel     import Panel
from rich.text      import Text
from rich.live      import Live
from rich.layout    import Layout
from rich.align     import Align
from rich.columns   import Columns
from rich.spinner   import Spinner
from rich           import box
from rich.style     import Style
from rich.rule      import Rule

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════════════════════
API_KEY       = os.getenv('API_KEY')
SECRET_KEY    = os.getenv('SECRET_KEY')
PASSPHRASE    = os.getenv('PASSPHRASE')

SYMBOL            = 'BTCUSDT'
TIMEFRAME         = '1day'
SMA_FAST          = 7
SMA_SLOW          = 40
SMA_TREND         = 100
STOP_LOSS_PCT     = 0.05
PRICE_INTERVAL    = 5             # secondi tra fetch del ticker live
CHECK_UTC_HOUR    = 0             # ora UTC della candela daily Bitget (mezzanotte)
CHECK_UTC_MINUTE  = 5             # buffer post-chiusura candela (minuti)
MAX_BAL_RETRY     = 8
BAL_RETRY_SLEEP   = 4
LIVE_REFRESH_RATE = 1             # refresh schermo (secondi)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "macro_state.json")
LOG_FILE   = os.path.join(BASE_DIR, "macrobot.log")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger("macroBot")

# ── Stato condiviso tra thread (accesso protetto da lock) ─────────────────────
_lock = threading.Lock()

snapshot: dict = {
    "ready":             False,   # False finché il primo ciclo non completa
    "usdt":              0.0,
    "btc":               0.0,
    "has_btc":           False,
    "bull_ieri":         False,
    "bull_altro":        False,
    "price_below_trend": False,
    "ieri":              {},       # dict con SMA_FAST/SLOW/TREND e close
    "live_price":        0.0,     # aggiornato ogni PRICE_INTERVAL secondi
    "prev_price":        0.0,     # per la freccia su/giù
    "state":             {},
    "next_check":        datetime.now(),
    "iteration":         0,
    "last_log":          "—",
    "error":             "",
}

console = Console()

# ══════════════════════════════════════════════════════════════════════════════
# 2. API BITGET
# ══════════════════════════════════════════════════════════════════════════════
def bitget_request(method: str, endpoint: str, params=None, body=None):
    base_url  = "https://api.bitget.com"
    timestamp = str(int(time.time() * 1000))
    path      = endpoint
    if params:
        path = f"{endpoint}?" + '&'.join(f"{k}={v}" for k, v in params.items())
    body_str = json.dumps(body) if body else ""
    message  = timestamp + method + path + body_str
    mac      = hmac.new(bytes(SECRET_KEY, 'utf-8'), bytes(message, 'utf-8'), digestmod=hashlib.sha256)
    sign     = base64.b64encode(mac.digest()).decode('utf-8')
    headers  = {
        'ACCESS-KEY': API_KEY, 'ACCESS-SIGN': sign,
        'ACCESS-PASSPHRASE': PASSPHRASE, 'ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json',
    }
    try:
        url  = base_url + path
        resp = requests.get(url, headers=headers, timeout=8) if method == 'GET' \
               else requests.post(url, headers=headers, data=body_str, timeout=8)
        return resp.json()
    except Exception as e:
        log.error(f"API {method} {endpoint}: {e}")
        return None

def get_ticker_price() -> float:
    """Endpoint pubblico — non richiede firma. Usato per aggiornamento live."""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/spot/market/tickers",
            params={"symbol": SYMBOL}, timeout=5,
        )
        data = r.json()
        if data and data.get('code') == '00000':
            return float(data['data'][0]['lastPr'])
    except Exception as e:
        log.warning(f"Ticker fetch: {e}")
    return 0.0

def get_daily_candles():
    res = bitget_request('GET', '/api/v2/spot/market/candles',
                         params={'symbol': SYMBOL, 'granularity': TIMEFRAME, 'limit': 150})
    if res and res.get('code') == '00000':
        df = pd.DataFrame(res['data'],
                          columns=['timestamp','open','high','low','close','base_v','quote_v','usdt_v'])
        df = df.astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df.sort_values('timestamp').reset_index(drop=True)
    log.warning("Candele non disponibili.")
    return None

def get_spot_balance(coin: str) -> float:
    res = bitget_request('GET', '/api/v2/spot/account/assets', params={'coin': coin})
    if res and res.get('code') == '00000':
        for a in res['data']:
            if a.get('coin') == coin:
                return float(a.get('available', 0))
    return 0.0

# ══════════════════════════════════════════════════════════════════════════════
# 3. STATO PERSISTENTE
# ══════════════════════════════════════════════════════════════════════════════
def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    log.info(f"Stato salvato: {state}")

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        "is_in_trade": False, "last_entry_price": 0.0,
        "last_exit_price": 0.0, "last_trade_date": "",
        "total_trades": 0, "total_pnl_pct": 0.0,
    }

# ══════════════════════════════════════════════════════════════════════════════
# 4. INDICATORI & SEGNALI
# ══════════════════════════════════════════════════════════════════════════════
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df['SMA_FAST']  = df['close'].rolling(SMA_FAST).mean()
    df['SMA_SLOW']  = df['close'].rolling(SMA_SLOW).mean()
    df['SMA_TREND'] = df['close'].rolling(SMA_TREND).mean()
    return df

def is_bull(row) -> bool:
    return row['SMA_FAST'] > row['SMA_SLOW'] and row['close'] > row['SMA_TREND']

# ══════════════════════════════════════════════════════════════════════════════
# 5. ORDINI
# ══════════════════════════════════════════════════════════════════════════════
def _poll_btc_balance(min_usd: float, price: float) -> float:
    for attempt in range(MAX_BAL_RETRY):
        btc = get_spot_balance('BTC')
        if btc * price >= min_usd:
            log.info(f"Balance BTC confermato: {btc:.6f} (try {attempt+1})")
            return btc
        time.sleep(BAL_RETRY_SLEEP)
    log.warning("Balance BTC non confermato dopo tutti i retry.")
    return 0.0

def execute_buy(usdt_amount: float, price: float) -> bool:
    log.info(f"BUY MARKET {usdt_amount} USDT @ ~{price:.2f}")
    with _lock:
        snapshot['last_log'] = f"▶ BUY {usdt_amount:.2f} USDT @ {price:,.2f} $"

    # [DE-COMMENTARE PER API REALE]
    # res = bitget_request('POST', '/api/v2/spot/trade/place-order', body={
    #     "symbol": SYMBOL, "side": "buy", "orderType": "market", "quoteAmount": str(usdt_amount)
    # })
    # if not res or res.get('code') != '00000':
    #     log.error(f"BUY fallito: {res}"); return False

    sl_price        = price * (1 - STOP_LOSS_PCT)
    btc_bal         = _poll_btc_balance(min_usd=10, price=price)
    size_to_protect = math.floor(btc_bal * 10000) / 10000

    if size_to_protect < 0.0001:
        log.error(f"BTC ricevuto ({size_to_protect}) sotto minQty.")
        with _lock:
            snapshot['last_log'] = "⚠ Stop Loss NON piazzato — BTC insufficiente!"
        return False

    log.info(f"STOP LOSS piazzato: trigger={sl_price:.2f} size={size_to_protect}")
    with _lock:
        snapshot['last_log'] += f"  🛡 SL @ {sl_price:,.2f} $"

    # [DE-COMMENTARE PER API REALE]
    # res_sl = bitget_request('POST', '/api/v2/spot/trade/place-plan-order', body={
    #     "symbol": SYMBOL, "side": "sell", "orderType": "market",
    #     "triggerPrice": str(round(sl_price, 2)), "baseAmount": str(size_to_protect)
    # })
    # if not res_sl or res_sl.get('code') != '00000':
    #     log.error(f"SL fallito: {res_sl}"); return False

    return True

def execute_sell(btc_amount: float, reason: str) -> bool:
    log.info(f"SELL MARKET {btc_amount} BTC — {reason}")
    with _lock:
        snapshot['last_log'] = f"▼ SELL {btc_amount} BTC — {reason}"

    # [DE-COMMENTARE PER API REALE]
    # bitget_request('POST', '/api/v2/spot/trade/cancel-plan-order', body={"symbol": SYMBOL})
    # res = bitget_request('POST', '/api/v2/spot/trade/place-order', body={
    #     "symbol": SYMBOL, "side": "sell", "orderType": "market", "baseAmount": str(btc_amount)
    # })
    # if not res or res.get('code') != '00000':
    #     log.error(f"SELL fallito: {res}"); return False

    return True

# ══════════════════════════════════════════════════════════════════════════════
# 6. THREAD — TICKER LIVE (ogni PRICE_INTERVAL secondi)
# ══════════════════════════════════════════════════════════════════════════════
def price_thread():
    while True:
        price = get_ticker_price()
        if price > 0:
            with _lock:
                snapshot['prev_price'] = snapshot['live_price']
                snapshot['live_price'] = price
        time.sleep(PRICE_INTERVAL)

# ══════════════════════════════════════════════════════════════════════════════
# 7. THREAD — LOGICA STRATEGICA (ogni CHECK_INTERVAL secondi)
# ══════════════════════════════════════════════════════════════════════════════
def strategy_thread():
    state     = load_state()
    iteration = 0

    while True:
        iteration += 1
        # Calcola il prossimo 00:05 UTC (chiusura candela daily Bitget + buffer)
        now_utc    = datetime.utcnow()
        next_utc   = now_utc.replace(hour=CHECK_UTC_HOUR, minute=CHECK_UTC_MINUTE,
                                     second=0, microsecond=0)
        if now_utc >= next_utc:
            next_utc += timedelta(days=1)
        # next_check è in ora locale per il countdown della dashboard
        next_check = datetime.now() + (next_utc - now_utc)

        try:
            df = get_daily_candles()
            if df is None or len(df) < SMA_TREND:
                log.warning("Candele insufficienti.")
                with _lock:
                    snapshot['error']      = "⚠ Dati insufficienti — riprovo tra 60s"
                    snapshot['next_check'] = datetime.now() + timedelta(seconds=60)
                time.sleep(60)  # retry rapido in caso di dati insufficienti
                continue

            df         = calculate_indicators(df)
            ieri       = df.iloc[-2]
            altro_ieri = df.iloc[-3]
            bull_ieri  = is_bull(ieri)
            bull_altro = is_bull(altro_ieri)
            price_below = ieri['close'] < ieri['SMA_TREND']

            usdt = get_spot_balance('USDT')
            btc  = get_spot_balance('BTC')

            with _lock:
                live_p = snapshot['live_price'] or float(df.iloc[-1]['close'])

            has_btc  = (btc * live_p) > 20
            oggi_str = datetime.now().strftime('%Y-%m-%d')

            # ── 1. Stop Loss colpito esternamente da Bitget ────────────────────
            if state['is_in_trade'] and not has_btc:
                log.warning(f"STOP LOSS HIT @ ~{live_p:.2f} $")
                entry = state['last_entry_price']
                pnl   = (live_p - entry) / entry * 100 if entry > 0 else 0.0
                state.update({
                    'is_in_trade': False, 'last_exit_price': live_p,
                    'last_entry_price': 0.0, 'last_trade_date': "",
                    'total_trades':  state.get('total_trades', 0) + 1,
                    'total_pnl_pct': state.get('total_pnl_pct', 0.0) + pnl,
                })
                save_state(state)
                with _lock:
                    snapshot['last_log'] = f"⚠ STOP LOSS HIT @ {live_p:,.2f} $  PnL: {pnl:+.2f}%"

            # ── 2. Acquisto ────────────────────────────────────────────────────
            if (bull_ieri and not bull_altro
                    and not state['is_in_trade']
                    and state.get('last_trade_date') != oggi_str):
                to_spend = math.floor(usdt * 0.98 * 100) / 100
                if to_spend > 10:
                    ok = execute_buy(to_spend, live_p)
                    if ok:
                        state.update({
                            'is_in_trade': True, 'last_entry_price': live_p,
                            'last_trade_date': oggi_str,
                            'total_trades': state.get('total_trades', 0) + 1,
                        })
                        save_state(state)
                    else:
                        log.error("execute_buy() fallito — stato invariato.")

            # ── 3. Vendita ─────────────────────────────────────────────────────
            exit_signal = (not bull_ieri) or price_below
            if exit_signal and state['is_in_trade'] and has_btc:
                reason      = "SMA crossover ribassista" if not bull_ieri else "Prezzo < SMA100"
                btc_to_sell = math.floor(btc * 10000) / 10000
                ok          = execute_sell(btc_to_sell, reason)
                if ok:
                    entry = state['last_entry_price']
                    pnl   = (live_p - entry) / entry * 100 if entry > 0 else 0.0
                    state.update({
                        'is_in_trade': False, 'last_exit_price': live_p,
                        'last_entry_price': 0.0, 'last_trade_date': "",
                        'total_pnl_pct': state.get('total_pnl_pct', 0.0) + pnl,
                    })
                    save_state(state)
                else:
                    log.error("execute_sell() fallito — stato invariato.")

            # ── Aggiorna snapshot condiviso ────────────────────────────────────
            with _lock:
                snapshot.update({
                    'ready':             True,
                    'usdt':              usdt,
                    'btc':               btc,
                    'has_btc':           has_btc,
                    'bull_ieri':         bull_ieri,
                    'bull_altro':        bull_altro,
                    'price_below_trend': price_below,
                    'ieri':              ieri.to_dict(),
                    'state':             dict(state),
                    'next_check':        next_check,
                    'iteration':         iteration,
                    'error':             "",
                })
                if snapshot['live_price'] == 0.0:
                    snapshot['live_price'] = float(df.iloc[-1]['close'])

        except Exception as e:
            log.exception(f"Errore strategy loop: {e}")
            with _lock:
                snapshot['error'] = f"Errore: {e}"

        # Dorme fino al prossimo 00:05 UTC
        sleep_secs = max(0, (next_utc - datetime.utcnow()).total_seconds())
        log.info(f"Prossimo check tra {sleep_secs/3600:.2f}h (00:{CHECK_UTC_MINUTE:02d} UTC)")
        time.sleep(sleep_secs)

# ══════════════════════════════════════════════════════════════════════════════
# 8. RENDERING — costruisce il layout ad ogni secondo
# ══════════════════════════════════════════════════════════════════════════════
def _pct_bar(value: float, width: int = 18) -> Text:
    clamped = max(-15.0, min(15.0, value))
    filled  = int(abs(clamped) / 15.0 * width)
    bar     = "█" * filled + "░" * (width - filled)
    color   = "green" if value >= 0 else "red"
    t = Text()
    t.append(f"{value:+6.2f}% ", style=f"bold {color}")
    t.append(bar,               style=color)
    return t

def _signal_panel(bull_ieri: bool, bull_altro: bool, price_below: bool) -> Panel:
    if bull_ieri and not bull_altro:
        txt, style = "▲  CROSSOVER RIALZISTA  —  BUY", "bold black on green"
    elif (not bull_ieri) or price_below:
        reason     = "crossover ribassista" if not bull_ieri else "prezzo < SMA100"
        txt, style = f"▼  SEGNALE USCITA  ({reason})", "bold white on red"
    elif bull_ieri:
        txt, style = "●  TREND UP  —  HOLDING", "bold black on bright_green"
    else:
        txt, style = "○  FUORI MERCATO  —  IN ATTESA", "bold white on grey30"
    return Panel(Align.center(Text(f"  {txt}  ", style=style)),
                 title="[bold cyan]SEGNALE[/]", border_style="cyan", box=box.ROUNDED)

def _countdown(next_check: datetime) -> str:
    delta    = max(timedelta(0), next_check - datetime.now())
    h, rem   = divmod(int(delta.total_seconds()), 3600)
    m, s     = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def build_layout() -> Layout:
    with _lock:
        snap = dict(snapshot)
        # copia profonda di ieri per evitare modifiche concorrenti
        snap['ieri']  = dict(snap.get('ieri', {}))
        snap['state'] = dict(snap.get('state', {}))

    now_str = datetime.now().strftime("%d %b %Y  %H:%M:%S")

    # ── Schermata di avvio ────────────────────────────────────────────────────
    if not snap['ready']:
        layout = Layout()
        layout.split_column(
            Layout(Panel(
                Align.center(
                    Text.assemble(
                        ("\n  ₿  BTC MACRO INVESTOR  \n", Style(bold=True, color="bright_yellow")),
                        (f"  Caricamento dati in corso…  \n  {now_str}\n",
                         Style(dim=True, color="white")),
                    ), vertical="middle",
                ),
                border_style="bright_yellow", box=box.DOUBLE_EDGE,
            ))
        )
        return layout

    live_p  = snap['live_price']
    prev_p  = snap['prev_price']
    ieri    = snap['ieri']
    state   = snap['state']
    usdt    = snap['usdt']
    btc     = snap['btc']
    has_btc = snap['has_btc']

    price_arrow = ("▲" if live_p >= prev_p else "▼") if prev_p > 0 else "●"
    price_color = "green" if live_p >= prev_p else "red"

    sma_t = ieri.get('SMA_TREND', 1)
    sma_f = ieri.get('SMA_FAST', 1)
    sma_s = ieri.get('SMA_SLOW', 1)
    dist_trend = (live_p - sma_t) / sma_t * 100 if sma_t else 0
    dist_fast  = (live_p - sma_f) / sma_f * 100 if sma_f else 0
    dist_slow  = (live_p - sma_s) / sma_s * 100 if sma_s else 0

    # ── Header ────────────────────────────────────────────────────────────────
    header = Panel(
        Align.center(Text.assemble(
            ("  ₿  BTC MACRO INVESTOR  ", Style(bold=True, color="bright_white", bgcolor="grey11")),
            ("SPOT · BITGET  ", Style(color="bright_yellow", bgcolor="grey11")),
            (f"  {now_str}  ", Style(dim=True, color="white", bgcolor="grey11")),
            (f"ciclo #{snap['iteration']}", Style(dim=True, bgcolor="grey11")),
        )),
        style="bold bright_yellow", box=box.DOUBLE_EDGE, padding=(0, 0),
    )

    # ── Prezzo live ───────────────────────────────────────────────────────────
    price_panel = Panel(
        Align.center(Text.assemble(
            (f"  {price_arrow} ", Style(bold=True, color=price_color)),
            (f"{live_p:>12,.2f} $  ", Style(bold=True, color=price_color, bgcolor="grey11")),
            ("BTCUSDT  LIVE", Style(dim=True, color="white")),
        )),
        border_style=price_color, box=box.HEAVY, padding=(0, 1),
    )

    # ── Balance ───────────────────────────────────────────────────────────────
    bal = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 3), expand=True)
    bal.add_column(justify="right", style="dim white")
    bal.add_column(justify="left",  style="bold white")
    bal.add_column(justify="right", style="dim white")
    bal.add_column(justify="left",  style="bold white")
    bal.add_row("💵 USDT",  f"{usdt:,.2f}",
                "₿  BTC",  f"{btc:.6f}")
    bal.add_row("💼 VALUE", f"{btc * live_p:,.2f} $",
                "🎯 STATO", "[bold green]LONG[/]" if has_btc else "[bold dim]WAIT[/]")
    bal_panel = Panel(bal, title="[bold cyan]PORTAFOGLIO[/]",
                      border_style="cyan", box=box.ROUNDED)

    # ── Indicatori ────────────────────────────────────────────────────────────
    ind = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2), expand=True)
    ind.add_column("Indicatore",  style="bold white",  width=20)
    ind.add_column("Valore",      style="bold yellow",  justify="right", width=14)
    ind.add_column("Δ da live",   justify="left")
    ind.add_row(f"SMA {SMA_FAST}   Veloce",
                f"{sma_f:>10,.2f} $", _pct_bar(dist_fast))
    ind.add_row(f"SMA {SMA_SLOW}   Lenta",
                f"{sma_s:>10,.2f} $", _pct_bar(dist_slow))
    ind.add_row(f"SMA {SMA_TREND}  Trend",
                f"{sma_t:>10,.2f} $", _pct_bar(dist_trend))
    ind_panel = Panel(ind, title="[bold cyan]INDICATORI DAILY (candela -1)[/]",
                      border_style="cyan", box=box.ROUNDED)

    # ── Segnale ───────────────────────────────────────────────────────────────
    sig_panel = _signal_panel(snap['bull_ieri'], snap['bull_altro'], snap['price_below_trend'])

    # ── Posizione & Risk ──────────────────────────────────────────────────────
    last_in  = state.get('last_entry_price', 0.0)
    last_out = state.get('last_exit_price',  0.0)
    risk = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 3), expand=True)
    risk.add_column(justify="right", style="dim white",  width=16)
    risk.add_column(justify="left",  style="bold white")
    risk.add_row("Last Entry",  f"{last_in:,.2f} $"  if last_in  > 0 else "[dim]—[/]")
    risk.add_row("Last Exit",   f"{last_out:,.2f} $" if last_out > 0 else "[dim]—[/]")

    if state.get('is_in_trade') and last_in > 0:
        sl_price = last_in * (1 - STOP_LOSS_PCT)
        pnl_pct  = (live_p - last_in) / last_in * 100
        dist_sl  = (live_p - sl_price) / sl_price * 100
        pnl_col  = "green" if pnl_pct >= 0 else "red"
        risk.add_row("Stop Loss", f"[red]{sl_price:,.2f} $[/]  [dim]({dist_sl:+.2f}% away)[/]")
        risk.add_row("P&L live",  f"[{pnl_col}]{pnl_pct:+.2f}%[/]")

    total_pnl = state.get('total_pnl_pct', 0.0)
    risk.add_row("Trade totali",  str(state.get('total_trades', 0)))
    risk.add_row("P&L cumulato",
                 f"[{'green' if total_pnl >= 0 else 'red'}]{total_pnl:+.2f}%[/]")
    risk_panel = Panel(risk, title="[bold cyan]POSIZIONE & RISK[/]",
                       border_style="cyan", box=box.ROUNDED)

    # ── Footer: countdown live + ultimo evento ────────────────────────────────
    cd_str   = _countdown(snap['next_check'])
    last_log = snap.get('last_log', '—')
    footer   = Panel(
        Align.center(Text.assemble(
            ("  ⏳ Prossimo check: ",  Style(dim=True, color="white")),
            (cd_str,                   Style(bold=True, color="bright_yellow")),
            ("   │   ",                Style(color="grey30")),
            ("📋 Ultimo evento: ",     Style(dim=True, color="white")),
            (last_log,                 Style(color="white")),
            (f"   │   log → {os.path.basename(LOG_FILE)}  ", Style(dim=True)),
        )),
        border_style="grey30", box=box.SIMPLE, padding=(0, 0),
    )

    # ── Errore (se presente) ──────────────────────────────────────────────────
    rows = [
        Layout(header,     size=3),
        Layout(price_panel, size=3),
        Layout(bal_panel,  size=5),
        Layout(ind_panel,  size=6),
        Layout(sig_panel,  size=3),
        Layout(risk_panel, size=7),
    ]
    if snap.get('error'):
        rows.append(Layout(
            Panel(Align.center(Text(snap['error'], style="bold red")),
                  border_style="red", box=box.HEAVY), size=3,
        ))
    rows.append(Layout(footer, size=3))

    layout = Layout()
    layout.split_column(*rows)
    return layout

# ══════════════════════════════════════════════════════════════════════════════
# 9. MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("═══ macroBot avviato ═══")

    # Thread 1: ticker live (ogni 5s, non autenticato)
    threading.Thread(target=price_thread, daemon=True).start()

    # Thread 2: logica strategica (una volta al giorno, alle 00:05 UTC)
    threading.Thread(target=strategy_thread, daemon=True).start()

    # Main thread: Rich Live — aggiorna tutto il display ogni secondo, senza scroll
    with Live(
        build_layout(),
        console=console,
        refresh_per_second=1,
        screen=True,            # fullscreen — sovrascrive sempre la stessa area
    ) as live:
        while True:
            live.update(build_layout())
            time.sleep(LIVE_REFRESH_RATE)

if __name__ == "__main__":
    main()