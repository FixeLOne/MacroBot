import pandas as pd
import numpy as np
import time
import os
import hmac
import hashlib
import base64
import requests
import json
import math
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ── Rich (pip install rich) ───────────────────────────────────────────────────
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich.align import Align
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.rule import Rule
from rich.padding import Padding
from rich.style import Style

load_dotenv()

# ==========================================
# 1. CONFIGURAZIONE
# ==========================================
API_KEY    = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')
PASSPHRASE = os.getenv('PASSPHRASE')

SYMBOL       = 'BTCUSDT'
PRODUCT_TYPE = 'SPOT'
TIMEFRAME    = '1day'
SMA_FAST     = 7
SMA_SLOW     = 40
SMA_TREND    = 100
STOP_LOSS_PCT = 0.05          # 5% Hard Stop Loss
CHECK_INTERVAL = 3600         # Controlla ogni ora
MAX_BALANCE_RETRY = 8         # Retry per verifica balance post-buy
BALANCE_RETRY_SLEEP = 4       # Secondi tra retry

# FIX JSON: Forziamo il salvataggio nella stessa directory dello script!
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "macro_state.json")
LOG_FILE   = os.path.join(BASE_DIR, "macrobot.log")

console = Console()

# ── Logging su file ───────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("macroBot")

# ==========================================
# 2. STATO E API
# ==========================================
def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    log.info(f"Stato salvato: {state}")

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        "is_in_trade": False,
        "last_entry_price": 0.0,
        "last_exit_price": 0.0,
        "last_trade_date": "",
        "total_trades": 0,
        "total_pnl_pct": 0.0,
    }

def bitget_request(method: str, endpoint: str, params=None, body=None):
    base_url    = "https://api.bitget.com"
    timestamp   = str(int(time.time() * 1000))
    path        = endpoint
    if params:
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        path = f"{endpoint}?{query_string}"
    body_str = json.dumps(body) if body else ""
    message  = timestamp + method + path + body_str

    # FIX #1: digestmod= obbligatorio in Python 3.8+
    mac  = hmac.new(bytes(SECRET_KEY, 'utf-8'), bytes(message, 'utf-8'), digestmod=hashlib.sha256)
    sign = base64.b64encode(mac.digest()).decode('utf-8')

    headers = {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       sign,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'ACCESS-TIMESTAMP':  timestamp,
        'Content-Type':      'application/json',
    }
    try:
        url  = base_url + path
        resp = (requests.get(url, headers=headers)
                if method == 'GET'
                else requests.post(url, headers=headers, data=body_str))
        return resp.json()
    except Exception as e:
        log.error(f"Errore API {method} {endpoint}: {e}")
        return None

def get_daily_candles():
    res = bitget_request('GET', '/api/v2/spot/market/candles',
                         params={'symbol': SYMBOL, 'granularity': TIMEFRAME, 'limit': 150})
    if res and res.get('code') == '00000':
        df = pd.DataFrame(res.get('data', []),
                          columns=['timestamp','open','high','low','close','base_v','quote_v','usdt_v'])
        df = df.astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df.sort_values('timestamp').reset_index(drop=True)
    log.warning("Impossibile ottenere candele daily.")
    return None

def get_spot_balance(coin: str) -> float:
    res = bitget_request('GET', '/api/v2/spot/account/assets', params={'coin': coin})
    if res and res.get('code') == '00000':
        for asset in res.get('data', []):
            if asset.get('coin') == coin:
                return float(asset.get('available', 0))
    return 0.0

# ==========================================
# 3. INDICATORI E SEGNALI
# ==========================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df['SMA_FAST']  = df['close'].rolling(window=SMA_FAST).mean()
    df['SMA_SLOW']  = df['close'].rolling(window=SMA_SLOW).mean()
    df['SMA_TREND'] = df['close'].rolling(window=SMA_TREND).mean()
    return df

def evaluate_signals(df: pd.DataFrame):
    ieri       = df.iloc[-2]
    altro_ieri = df.iloc[-3]
    current_p  = df.iloc[-1]['close']

    # FIX #2: condizione long = SMA7 > SMA40 AND prezzo > SMA100
    def is_bull(row):
        return (row['SMA_FAST'] > row['SMA_SLOW']) and (row['close'] > row['SMA_TREND'])

    bull_ieri  = is_bull(ieri)
    bull_altro = is_bull(altro_ieri)

    # FIX #3: segnale di uscita aggiuntivo — prezzo chiude sotto SMA100
    price_below_trend = ieri['close'] < ieri['SMA_TREND']

    return bull_ieri, bull_altro, current_p, ieri, price_below_trend

# ==========================================
# 4. ORDINI
# ==========================================
def _poll_btc_balance(min_value_usd: float, current_price: float) -> float:
    """FIX #4: polling con retry per attendere conferma ordine su exchange."""
    for attempt in range(MAX_BALANCE_RETRY):
        btc = get_spot_balance('BTC')
        if btc * current_price >= min_value_usd:
            log.info(f"Balance BTC confermato: {btc:.6f} BTC (tentativo {attempt+1})")
            return btc
        log.info(f"Attendo conferma ordine... tentativo {attempt+1}/{MAX_BALANCE_RETRY}")
        time.sleep(BALANCE_RETRY_SLEEP)
    log.warning("Balance BTC non confermato dopo tutti i retry.")
    return 0.0

def execute_buy(usdt_amount: float, current_price: float) -> bool:
    log.info(f"BUY MARKET: {usdt_amount} USDT @ ~{current_price:.2f}")
    console.print(f"  [bold green]▶  BUY MARKET[/] — investo [yellow]{usdt_amount:.2f} USDT[/] a mercato…")

    # [DE-COMMENTARE PER API REALE]
    # res = bitget_request('POST', '/api/v2/spot/trade/place-order', body={
    #     "symbol": SYMBOL, "side": "buy", "orderType": "market", "quoteAmount": str(usdt_amount)
    # })
    # if not res or res.get('code') != '00000':
    #     log.error(f"BUY fallito: {res}")
    #     return False

    sl_price = current_price * (1 - STOP_LOSS_PCT)

    # FIX #4: aspetta conferma balance BTC prima di piazzare lo SL
    btc_balance    = _poll_btc_balance(min_value_usd=10, current_price=current_price)
    size_to_protect = math.floor(btc_balance * 10000) / 10000

    min_qty = 0.0001  # Bitget BTCUSDT spot minimum
    if size_to_protect < min_qty:
        log.error(f"BTC ricevuto ({size_to_protect}) sotto minQty ({min_qty}). Stop Loss NON piazzato.")
        console.print(f"  [bold red]✗  ATTENZIONE:[/] BTC insufficiente per Stop Loss — verifica manuale!")
        return False

    log.info(f"STOP LOSS PLAN ORDER: trigger={sl_price:.2f} size={size_to_protect}")
    console.print(f"  [bold yellow]🛡  STOP LOSS[/] — trigger a [red]{sl_price:.2f} $[/] per [white]{size_to_protect} BTC[/]")

    # [DE-COMMENTARE PER API REALE]
    # res_sl = bitget_request('POST', '/api/v2/spot/trade/place-plan-order', body={
    #     "symbol": SYMBOL, "side": "sell", "orderType": "market",
    #     "triggerPrice": str(round(sl_price, 2)), "baseAmount": str(size_to_protect)
    # })
    # if not res_sl or res_sl.get('code') != '00000':
    #     log.error(f"Stop Loss NON piazzato: {res_sl}")
    #     console.print("  [bold red]✗  Stop Loss FALLITO — posizione non protetta![/]")
    #     return False

    return True

def execute_sell(btc_amount: float) -> bool:
    log.info(f"SELL MARKET: {btc_amount} BTC")
    console.print(f"  [bold red]▶  CANCELLO ordini piano pendenti…[/]")

    # [DE-COMMENTARE PER API REALE]
    # res_cancel = bitget_request('POST', '/api/v2/spot/trade/cancel-plan-order', body={"symbol": SYMBOL})
    # if not res_cancel or res_cancel.get('code') != '00000':
    #     log.warning(f"Cancel plan order — risposta anomala: {res_cancel}")

    console.print(f"  [bold red]▶  SELL MARKET[/] — vendo [yellow]{btc_amount} BTC[/] a mercato…")

    # [DE-COMMENTARE PER API REALE]
    # res_sell = bitget_request('POST', '/api/v2/spot/trade/place-order', body={
    #     "symbol": SYMBOL, "side": "sell", "orderType": "market", "baseAmount": str(btc_amount)
    # })
    # if not res_sell or res_sell.get('code') != '00000':
    #     log.error(f"SELL fallito: {res_sell}")
    #     return False   # FIX #5: non aggiornare stato se il sell è fallito

    return True

# ==========================================
# 5. UI — DASHBOARD RICH
# ==========================================
def _trend_bar(value: float, width: int = 20) -> Text:
    """Barra grafica proporzionale al % distanza dal trend."""
    clamped = max(-15.0, min(15.0, value))
    filled  = int(abs(clamped) / 15.0 * width)
    bar     = "█" * filled + "░" * (width - filled)
    color   = "green" if value >= 0 else "red"
    t = Text()
    t.append(f"[{value:+.2f}%] ", style=f"bold {color}")
    t.append(bar, style=color)
    return t

def _signal_badge(bull_ieri: bool, bull_altro: bool) -> Text:
    if bull_ieri and not bull_altro:
        return Text("  ▲ CROSSOVER RIALZISTA  ", style="bold black on green")
    elif not bull_ieri and bull_altro:
        return Text("  ▼ CROSSOVER RIBASSISTA  ", style="bold white on red")
    elif bull_ieri:
        return Text("  ● TREND UP (Hold)  ", style="bold black on bright_green")
    else:
        return Text("  ○ FUORI MERCATO  ", style="bold white on grey30")

def _next_check_str(next_check: datetime) -> str:
    delta = next_check - datetime.now()
    mins  = int(delta.total_seconds() // 60)
    secs  = int(delta.total_seconds() % 60)
    return f"{mins:02d}m {secs:02d}s"

def render_dashboard(df, usdt, btc, current_p, state, has_btc, bull_ieri, bull_altro, next_check, iteration):
    ieri      = df.iloc[-2]
    dist_trend = ((current_p - ieri['SMA_TREND']) / ieri['SMA_TREND']) * 100
    dist_fast  = ((current_p - ieri['SMA_FAST'])  / ieri['SMA_FAST'])  * 100
    dist_slow  = ((current_p - ieri['SMA_SLOW'])  / ieri['SMA_SLOW'])  * 100

    os.system('cls' if os.name == 'nt' else 'clear')

    # ── Header ────────────────────────────────────────────────────────────────
    now_str  = datetime.now().strftime("%A %d %b %Y  %H:%M:%S")
    header   = Align.center(
        Text.assemble(
            ("  ₿  BTC MACRO INVESTOR  ", Style(bold=True, color="bright_white", bgcolor="grey15")),
            ("SPOT · BITGET  ", Style(color="bright_yellow", bgcolor="grey15")),
        )
    )
    console.print(Panel(header, style="bold bright_yellow", box=box.DOUBLE_EDGE))
    console.print(Align.center(Text(f"⏱  {now_str}  │  ciclo #{iteration}", style="dim white")))
    console.print()

    # ── Balance & Prezzo ──────────────────────────────────────────────────────
    bal_table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 2))
    bal_table.add_column(justify="right",  style="dim white", width=16)
    bal_table.add_column(justify="left",   style="bold white", width=22)
    bal_table.add_column(justify="right",  style="dim white", width=16)
    bal_table.add_column(justify="left",   style="bold white", width=22)

    bal_table.add_row(
        "💵 USDT",   f"{usdt:>12.2f}",
        "₿  BTC",    f"{btc:>12.6f}",
    )
    bal_table.add_row(
        "📈 PREZZO", f"{current_p:>12,.2f} $",
        "💼 VALUE",  f"{btc * current_p:>12.2f} $",
    )
    console.print(Panel(bal_table, title="[bold cyan]PORTAFOGLIO[/]", border_style="cyan", box=box.ROUNDED))

    # ── Indicatori ───────────────────────────────────────────────────────────
    ind_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
    ind_table.add_column("Indicatore",    style="bold white",   width=22)
    ind_table.add_column("Valore",        style="bold yellow",  justify="right", width=16)
    ind_table.add_column("Δ Prezzo",      justify="left",       width=32)

    ind_table.add_row(
        f"SMA {SMA_FAST}  (Veloce)",
        f"{ieri['SMA_FAST']:>10,.2f} $",
        _trend_bar(dist_fast),
    )
    ind_table.add_row(
        f"SMA {SMA_SLOW}  (Lenta)",
        f"{ieri['SMA_SLOW']:>10,.2f} $",
        _trend_bar(dist_slow),
    )
    ind_table.add_row(
        f"SMA {SMA_TREND} (Trend)",
        f"{ieri['SMA_TREND']:>10,.2f} $",
        _trend_bar(dist_trend),
    )
    console.print(Panel(ind_table, title="[bold cyan]INDICATORI DAILY (candela -1)[/]", border_style="cyan", box=box.ROUNDED))

    # ── Segnale ───────────────────────────────────────────────────────────────
    console.print(Panel(
        Align.center(_signal_badge(bull_ieri, bull_altro)),
        title="[bold cyan]SEGNALE[/]", border_style="cyan", box=box.ROUNDED,
    ))

    # ── Posizione & Risk ──────────────────────────────────────────────────────
    risk_table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 2))
    risk_table.add_column(justify="right", style="dim white",  width=18)
    risk_table.add_column(justify="left",  style="bold white", width=28)

    last_in  = state['last_entry_price']
    last_out = state['last_exit_price']

    risk_table.add_row("Last Entry",  f"{last_in:.2f} $"  if last_in  > 0 else "—")
    risk_table.add_row("Last Exit",   f"{last_out:.2f} $" if last_out > 0 else "—")

    if state['is_in_trade'] and last_in > 0:
        sl_price = last_in * (1 - STOP_LOSS_PCT)
        pnl_pct  = (current_p - last_in) / last_in * 100
        dist_sl  = (current_p - sl_price) / sl_price * 100
        risk_table.add_row("Stop Loss",   f"[red]{sl_price:.2f} $[/]  ([dim]{dist_sl:+.2f}% away[/])")
        risk_table.add_row("P&L attuale", f"[{'green' if pnl_pct >= 0 else 'red'}]{pnl_pct:+.2f}%[/]")

    risk_table.add_row("Trade totali", str(state.get('total_trades', 0)))

    status_text = (
        Text("  🟢  IN HOLDING — Long attivo  ", style="bold black on green")
        if has_btc
        else Text("  ⏳  IN ATTESA — Liquidità USDT  ", style="bold white on grey30")
    )
    console.print(Panel(
        Columns([risk_table, Align.center(status_text, vertical="middle")]),
        title="[bold cyan]POSIZIONE & RISK[/]", border_style="cyan", box=box.ROUNDED,
    ))

    # ── Countdown ─────────────────────────────────────────────────────────────
    console.print(Panel(
        Align.center(Text(
            f"⏳  Prossimo controllo tra  {_next_check_str(next_check)}",
            style="bold dim white",
        )),
        border_style="grey30", box=box.SIMPLE,
    ))

    console.print(Align.center(Text(f"📋  Log → {LOG_FILE}", style="dim")))

# ==========================================
# 6. LOOP PRINCIPALE
# ==========================================
def main():
    state     = load_state()
    iteration = 0
    log.info("═══ macroBot avviato ═══")

    while True:
        iteration += 1
        next_check = datetime.now() + timedelta(seconds=CHECK_INTERVAL)

        try:
            df = get_daily_candles()

            if df is None or len(df) < SMA_TREND:
                console.print("[red]Dati insufficienti — attendo il prossimo ciclo.[/]")
                log.warning("Candele insufficienti o API error.")
                time.sleep(CHECK_INTERVAL)
                continue

            df = calculate_indicators(df)
            bull_ieri, bull_altro, current_p, ieri, price_below_trend = evaluate_signals(df)

            usdt         = get_spot_balance('USDT')
            btc          = get_spot_balance('BTC')
            has_btc      = (btc * current_p) > 20
            oggi_str     = datetime.now().strftime('%Y-%m-%d')

            # ── 1. CHECK STOP LOSS ESTERNO (Bitget ha già liquidato) ──────────
            if state['is_in_trade'] and not has_btc:
                log.warning(f"STOP LOSS HIT da Bitget a ~{current_p:.2f} $")
                console.print("[bold red]⚠  STOP LOSS COLPITO DA BITGET — resetto stato.[/]")
                entry = state['last_entry_price']
                pnl   = (current_p - entry) / entry * 100 if entry > 0 else 0.0
                state['is_in_trade']     = False
                state['last_exit_price'] = current_p
                state['last_entry_price'] = 0.0
                state['total_trades']    = state.get('total_trades', 0) + 1
                state['total_pnl_pct']   = state.get('total_pnl_pct', 0.0) + pnl
                save_state(state)

            # ── 2. LOGICA ACQUISTO ────────────────────────────────────────────
            if (bull_ieri and not bull_altro
                    and not state['is_in_trade']
                    and state.get('last_trade_date') != oggi_str):

                to_spend = math.floor(usdt * 0.98 * 100) / 100
                if to_spend > 10:
                    log.info(f"SEGNALE BUY — spendo {to_spend} USDT")
                    ok = execute_buy(to_spend, current_p)
                    if ok:
                        state['is_in_trade']      = True
                        state['last_entry_price']  = current_p
                        state['last_trade_date']   = oggi_str
                        state['total_trades']      = state.get('total_trades', 0) + 1
                        save_state(state)
                    else:
                        log.error("execute_buy() ha restituito False — stato NON aggiornato.")

            # ── 3. LOGICA VENDITA ─────────────────────────────────────────────
            # FIX #2: uscita anche se prezzo < SMA100 (anche con SMA7 ancora > SMA40)
            exit_signal = (not bull_ieri) or price_below_trend
            if exit_signal and state['is_in_trade'] and has_btc:
                reason = "SMA crossover ribassista" if not bull_ieri else "Prezzo < SMA100"
                log.info(f"SEGNALE SELL ({reason}) — vendo {btc:.6f} BTC")
                btc_to_sell = math.floor(btc * 10000) / 10000
                ok = execute_sell(btc_to_sell)
                if ok:                          # FIX #5: aggiorna stato SOLO se sell ok
                    entry = state['last_entry_price']
                    pnl   = (current_p - entry) / entry * 100 if entry > 0 else 0.0
                    state['is_in_trade']      = False
                    state['last_exit_price']  = current_p
                    state['last_entry_price'] = 0.0
                    state['total_pnl_pct']    = state.get('total_pnl_pct', 0.0) + pnl
                    save_state(state)
                else:
                    log.error("execute_sell() ha restituito False — stato NON aggiornato.")

            # ── Render UI ─────────────────────────────────────────────────────
            render_dashboard(
                df, usdt, btc, current_p, state,
                has_btc, bull_ieri, bull_altro,
                next_check, iteration,
            )

        except Exception as e:
            log.exception(f"Errore nel loop principale: {e}")
            console.print(f"[bold red]Errore: {e}[/]")

        # ── Countdown sleep (aggiorna display ogni 30s) ───────────────────────
        while datetime.now() < next_check:
            remaining = (next_check - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            sleep_chunk = min(30, remaining)
            time.sleep(sleep_chunk)
            # Re-stampa solo il countdown senza rinfrescare tutto il display
            console.print(
                f"\r  [dim]⏳ Prossimo controllo tra {_next_check_str(next_check)}[/]",
                end="",
            )

if __name__ == "__main__":
    main()