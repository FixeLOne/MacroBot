# 🌍 Bitget Macro Investor: Trend Following su BTC (Spot)

Questo bot è un sistema di trading algoritmico a lungo termine (Macro) progettato per intercettare e cavalcare le grandi *Bull Run* di Bitcoin, proteggendo il capitale durante i *Bear Market*. Opera esclusivamente sul mercato **SPOT** di Bitget per eliminare i costi latenti dei *Funding Rates* tipici dei contratti Futures.

---

## 📊 La Strategia (7 / 40 / 100)
La logica si basa su un approccio quantitativo testato su uno storico pluriennale di Bitcoin, che ha sovraperformato il Buy & Hold abbattendo drasticamente il Drawdown.

### 1. Il Motore Logico
Il bot analizza il mercato una volta al giorno (Timeframe: `1day`) utilizzando tre Medie Mobili Semplici (SMA):
* **SMA Veloce (7 giorni):** Segue il momentum di breve termine.
* **SMA Lenta (40 giorni):** Definisce il trend di medio termine.
* **SMA Trend (100 giorni):** Il filtro macroeconomico definitivo.

### 2. Le Regole di Ingresso e Uscita
* **Acquisto (Long):** Il bot investe il 98% della liquidità disponibile in BTC quando la SMA 7 incrocia al rialzo la SMA 40 **E** il prezzo di chiusura si trova sopra la SMA 100.
* **Uscita (Take Profit Dinamico / Stop Fisiologico):** Il bot chiude la posizione e torna in USDT quando il trend si esaurisce (la SMA 7 incrocia al ribasso la SMA 40) oppure se il prezzo chiude sotto la SMA 100.
* **Hard Stop Loss (-5%):** Indipendentemente dalle chiusure giornaliere, il bot controlla il prezzo ogni ora. Se rileva un crollo pari o superiore al 5% rispetto al prezzo di carico medio, liquida la posizione per prevenire "Flash Crash" irreversibili (Cigni Neri).

---

## 🏗️ Architettura del Software
A differenza dei bot da scalping, questo script utilizza un'architettura **"Smart Loop"**:
1. **Efficienza Risorse:** Il bot si attiva solo una volta all'ora, scarica le candele, calcola le medie, verifica la tenuta dello Stop Loss, aggiorna la Dashboard a schermo e poi entra in modalità *Sleep* per 60 minuti. Impatto su CPU e RAM quasi nullo.
2. **State Persistence:** Lo stato del bot (prezzo di carico, ultima operazione) viene salvato localmente su un file `macro_state.json`. In caso di riavvio del server o crash di sistema, il bot riprende esattamente da dove si era interrotto, senza disallineamenti.
3. **Compound Reale:** Ad ogni nuovo ingresso, il bot calcola dinamicamente il 98% del saldo USDT. Questo permette una capitalizzazione composta (Compounding) automatica dei profitti.

---

## ⚙️ Installazione e Requisiti

**1. Librerie Python richieste:**
```bash
pip install pandas requests python-dotenv numpy
```
2. Variabili d'Ambiente (.env):
Crea un file .env nella directory principale inserendo le credenziali API di Bitget.

Attenzione: Le API devono avere i permessi abilitati per il trading SPOT. Non abilitare mai i permessi di prelievo (Withdrawal).
```
API_KEY=la_tua_api_key_qui
SECRET_KEY=il_tuo_secret_key_qui
PASSPHRASE=la_tua_passphrase_qui
```
🚀 Messa in Produzione (Deploy su VPS Linux)
Poiché il bot include una dashboard testuale interattiva (TUI) e deve girare 24/7, si consiglia l'esecuzione all'interno di un multiplexer di terminale come screen o tmux.

1. Avvia una sessione isolata:

```
screen -S btc_macro
```
2. Esegui lo script:
(Assicurati di aver de-commentato le funzioni API di acquisto/vendita nel codice se hai completato i test in paper-trading)

```
python3 btc_macro.py
```
3. Sganciati dalla sessione (Detach):
Premi in sequenza: CTRL + A e poi D.

5. Per monitorare la Dashboard:
Ripristina lo schermo in qualsiasi momento digitando:

```
screen -r btc_macro
```
⚠️ Risk Disclaimer
La strategia applica una tolleranza al rischio (Stop Loss) stretta per proteggere il capitale, ma i rendimenti passati non sono garanzia di rendimenti futuri.

Il bot opera sul mercato Spot, annullando il rischio di liquidazione da leva finanziaria e i costi di Funding Rate, rendendolo adatto all'investimento di capitali destinati al medio-lungo periodo.
