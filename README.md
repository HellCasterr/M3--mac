# Trade M3

Built from Trade M2, with live sector context and a durable alert review log.

Trade M3 is a Windows-friendly, local alert dashboard built exclusively for the
Upstox API. It monitors the official **Nifty 200** constituent list and implements
the exact **Case 2** price-crossing rules discussed for Point A and Point B.

It provides stock alerts and a paper-only option execution terminal. It never places a real broker order.

## Paper options terminal

Open **Paper terminal** from the dashboard. It automatically selects eligible option strikes
and can simulate new alerts using saved capital, target, stop and holding-time defaults.
Manual tickets support per-trade overrides. New eligible alerts record a simulated option
buy at the current chain ask immediately; observed option bids trigger premium-based exits.
The terminal includes open positions, target/stop children, executed fills, paper P&L and CSV export.
Automatic paper entries start off; enable them and save defaults on the terminal page.
The defaults are +10% target and −5% stop, with no simulated brokerage, delay or slippage.
Option quotes, available virtual cash and listed contracts are still required. Connect Upstox
and load the watchlist each trading day. Older alerts are not replayed.
No live-order mode is implemented. LONG buys calls; SHORT buys puts (not short puts).

- [Daily operation and setup](docs/PAPER_TERMINAL_GUIDE.md)
- [Research, selection rules and execution design](docs/PAPER_TRADING_DESIGN.md)

## Local alerts history and historical candle backend

Use **Alerts history** to choose a locally stored IST trading date, sort alerts by time,
and export that date to CSV for review. The export includes saved sector context,
option selection and linked paper results. Local review works without Upstox login.

A cached historical-data backend supports active and expired option candles and exact
contract discovery for future chart/replay development. Expired requests use the Plus
API; credentials/access must be valid. It does not create historical trades or charts.

[Usage, API examples and known challenges](docs/HISTORICAL_DATA_AND_ALERTS.md)

## New in M3: sector context

- Load Nifty 200 after Upstox login each trading day. The official constituent CSV
  provides the Industry classification; official Nifty Bank membership separates
  Banking from broader Financial Services when that source is available.
- The live dashboard shows **equal-weight sector basket returns**, calculated as
  the mean of each fresh constituent's percentage change from its Upstox previous
  official close (`cp`). These are **not official weighted NSE sector index returns**.
  They are also different from M2's last-three-minute-close reference for A/B levels.
- Coverage denominator is all mapped Nifty 200 members in the basket, including
  unloaded or paused members. Only active members with positive valid prices and
  exchange timestamps within the preceding 45 seconds contribute.
- Bullish means mean return >= +0.20% and at least 60% of fresh members rising.
  Bearish means <= -0.20% and at least 60% falling. Otherwise mixed. Labels require
  at least 3 fresh members, 80% coverage, and an open market session. These thresholds
  are descriptive and unvalidated, not a standardized score or probability.
- Dashboard refreshes every 5 seconds. WebSocket quotes feed calculations even with
  the browser closed. Start the app, connect and load before the market opens for
  full-day observation. It does not backfill historical sector readings after downtime.
- Every new alert saves its contemporaneous sector snapshot atomically in SQLite.
  Dashboard, browser notifications and Telegram display it. Recovered or delayed
  alerts show unavailable instead of using future market data. Unknown classification,
  stale quotes and inadequate coverage never imply a bullish/bearish confirmation.
- **Download alert history (CSV)** exports all recorded dates and snapshots. The
  append-only audit history survives changes to daily rule settings; the active
  alerts view still follows M2's reset behavior. The separate paper terminal records
  option executions and estimated results. Keep the local `data` folder backed up.
- If the CSV cannot be retrieved, cached classifications can be used. The packaged
  symbol-only fallback has no classifications: sector readings remain unavailable.
  Mapping source is shown. Financial Services remains broad if the bank source fails.

### Validation before automated execution

M3 now simulates option orders. A real-money order executor is not implemented.
Before considering it, record the **specific option contract**, expiry, actual entry
fill, bid/ask spread, quantity, charges, maximum adverse excursion, holding time,
and which came first: +10% target or -5% stop. An eventual +10% winner that first
fell 5% is a stop-out under that rule. Stock-price stops are not option-premium stops.
Evaluate sector-aligned versus opposed/mixed alerts under the same entry and exit
rules, including missed fills and losing days. One month of green days does not
validate a 20x capital increase or performance under different market conditions.

Future execution needs broker-confirmed protective orders, fill-based target/stop
prices, partial-fill handling, sibling exit cancellation, restart reconciliation,
position/sector exposure limits, daily loss limits and a kill switch. A stop trigger
is not a guaranteed -5% fill; gaps and liquidity can create larger losses. Upstox
API/algo requirements must be rechecked before implementation.

M3 preserves the current M2 crossing rules below, including LONG on a downward A
cross when opening above A. Sector context does not alter or suppress these signals.
The internal Python package remains `trade_m2` for compatibility; the default M3
SQLite file is `data/trade_m3.db`. Do not point M3 at your M2 database.
Both apps default to port 8000: run one at a time, or configure a separate port and
matching registered Upstox callback for M3. Use a separate Telegram bot if you run
both and want to avoid receiving signals from both programs.

## Level calculation

For each stock, the reference is the final completed three-minute candle close of
the immediately preceding trading session. With percentage `P`:

```text
Point A (upper) = reference close × (1 + P / 100)
Point B (lower) = reference close × (1 - P / 100)
```

If the daily range percentage is left blank, the application calculates it from
the preceding completed India VIX session:

```text
P = previous India VIX close / 8.54400
```

All calculations use Python `Decimal`; the dashboard rounds prices to two decimals
only for display.

## Exact Case 2 state machine

The first market price received for the session fixes the stock's opening zone.
Crossings are evaluated on Upstox live ticks, so the alert is emitted at the
intersection and does not wait for a three-minute candle to close.

| Opening zone | Qualifying movement | Alert |
| --- | --- | --- |
| Between B and A | Cross Point A upward | **LONG** |
| Between B and A | Cross Point B downward | **SHORT** |
| Above Point A | Cross Point A downward | **LONG** |
| Below Point B | Cross Point B upward | **SHORT** |

Point A always maps to LONG and Point B always maps to SHORT. An exact open on a
boundary does not create an artificial opening alert; the first movement away from
the level establishes which side the market occupied.

The default is one Point A alert and one Point B alert per stock per trading day.
The opening zone, last price, and sent flags are stored in SQLite so dashboard
refreshes cannot duplicate an alert.
Reloading a stock with a different percentage or corrected reference value resets
that stock's Case 2 state and replaces its earlier same-day alerts, so stale
deduplication records cannot block the recalculated levels.

## Recovery and protective stop

Upstox V3 LTPC is used for live crossing detection. On a fresh login, a new
subscription, or a WebSocket reconnect, Trade M3 first requests today's completed
three-minute candles and queues incoming ticks until recovery finishes. Because
OHLC data cannot reveal intrabar order, an inside-open recovery candle that touched
both A and B is deliberately skipped instead of guessing which alert came first.
The daily-open quote is accepted only when its Upstox timestamp belongs to the
current trading date, preventing a pre-market start from treating yesterday's OHLC
as today's opening price.

Every alert includes an indicative stop on the protective side of the entry. It
uses a 14-candle three-minute ATR and recent 10-candle structure, with a fixed
`1.50 × ATR` fallback and a risk distance bounded from 0.35% to 3.00%. The stop is
not submitted to Upstox and is not a guarantee of execution.

## Nifty 200 universe

“Top 200” is implemented as the official Nifty 200 index constituents, not a
hand-maintained or arbitrarily ranked list. The current CSV is downloaded from NSE
Indices when the universe is loaded and cached locally for 12 hours. The program
requires exactly 200 unique symbols before starting a bulk load; if the source is
temporarily unavailable, it uses the last valid local cache and then a clearly
dated packaged snapshot as the final fallback.

The 200 rules are built in a background job so the dashboard remains responsive.
Each stock is resolved to an exact NSE cash-equity instrument through Upstox and
then subscribed in safe batches.

## Windows setup

Requirements:

- Windows 10 or 11
- Python 3.11 or newer
- An Upstox account with API access
- Chrome, Edge, or another browser supporting desktop notifications

1. Create an Upstox developer app and set its redirect URL to exactly:

   ```text
   http://127.0.0.1:8000/auth/upstox/callback
   ```

2. Download or clone this repository.
3. Double-click `setup_windows.bat`.
4. Copy your existing `.env` into this folder, or edit the generated `.env`:

   ```dotenv
   UPSTOX_API_KEY=your_real_api_key
   UPSTOX_API_SECRET=your_real_api_secret
   UPSTOX_REDIRECT_URL=http://127.0.0.1:8000/auth/upstox/callback
   APP_HOST=127.0.0.1
   APP_PORT=8000
   ```

   Extra variables for other brokers in an older `.env` are harmless and ignored.

5. Double-click `start_windows.bat`.
6. Select **Connect Upstox** and complete the daily OAuth login.
7. Select **Enable browser alerts** and approve the browser permission.
8. Select **Load Nifty 200**. Leave the percentage blank to use the VIX-derived
   value, or enter a positive percentage up to 10.
9. Keep the terminal open during market hours. Keep the browser open for desktop
   notifications; configured Telegram delivery also works with the browser closed.

The access token stays in process memory and is not written to disk. `.env`, cached
constituents, and the SQLite database are excluded from Git.

## Telegram alerts for two people

One bot sends every new Case 2 alert to both configured chats. Each message identifies
**Trade M3**, the stock, LONG/SHORT, Point A/B, crossing direction, reference entry,
indicative **stock-price** stop, original IST timestamp, and live/recovery source.
These values are not option-premium targets or stops. Existing Case 2 rules are unchanged.

1. Create your bot through [@BotFather](https://t.me/BotFather) using `/newbot`.
2. **Both people must open the new bot's chat and tap Start** (or send `/start`).
3. Obtain each person's numeric chat ID. On your own computer, open
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` after they send `/start`.
   Identify each person from `result[].message.from`, then use that message's
   `message.chat.id`. Do not copy `update_id` or `message_id`. Phone numbers and
   personal `@usernames` cannot be substituted for private chat IDs. If the result is
   empty, send another message to the bot and refresh. Keep the token-bearing URL private.
4. Add these lines to your local `.env` (replace all placeholders):

   ```dotenv
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=your_bot_token_from_BotFather
   TELEGRAM_CHAT_IDS=123456789,987654321
   ```

   A single recipient also works. Duplicate IDs are removed. For compatibility,
   `TELEGRAM_CHAT_ID` is accepted when `TELEGRAM_CHAT_IDS` is empty. Negative group
   IDs are supported, provided the bot is a member and can send messages.
5. Restart Trade M3, then click **Send test to both chats** in the Telegram panel.
   It shows a separate result for each recipient. Upstox login and an open market
   are not needed for this test. `sent` means Telegram accepted the message, not
   that the person read it.

The token stays on the server, is excluded from Git via `.env`, and is not returned
to the dashboard. Set `TELEGRAM_ENABLED=false` and restart to turn delivery off.
Leave the computer awake, the program running, and Upstox connected for market alerts.
Updating `.env` always requires a restart.

Delivery uses SQLite queues and a separate background worker per recipient. A blocked
bot or wrong chat ID cannot prevent delivery to the other person. Acknowledged messages
are not resent after a restart; transient errors retry up to five attempts, and Telegram
rate-limit delays are respected. There is a rare possibility of duplicate delivery if
Telegram accepts a message but its response is lost or the process stops before saving
success. Messages carry an alert ID to identify that case.

First enablement skips already-stored alerts. Subsequently, pending deliveries survive
restarts, but signals more than **five minutes past their original event time** expire.
Recovered signals within that window are explicitly marked delayed. Queued alerts whose
rules were recalculated are cancelled when the old event is removed. Counters include
test messages. Separate program installations have separate queues and may both send
alerts if configured with the same bot and recipient IDs.

Telegram's standard bot messaging is free within its rate limits; this implementation
does not enable paid broadcasts. References: [Bot API](https://core.telegram.org/bots/api),
[limits](https://core.telegram.org/bots/faq#how-do-i-avoid-hitting-limits).

## Development

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe run.py
```

API documentation is available at <http://127.0.0.1:8000/api/docs> while running.

## Operational safety

- Alerts depend on broker market data, network connectivity, symbol resolution,
  and browser/Windows notification settings.
- The dashboard exposes feed connection, recovery, queued-tick, stale-feed, and
  subscription counts. A connected feed is marked stale after 45 seconds without a
  tick during market hours when active subscriptions exist.
- Browser alerts are backed by a persistent server-sent event stream. Reconnection
  backfill and database uniqueness prevent lost or duplicated UI notifications.
- Stops and entries are reference values. Gaps, fast markets, tick size, liquidity,
  and slippage can materially change execution.
- Case 2 is a mechanical signal definition, not proof of an edge. Paper trade and
  validate it out of sample before risking capital.

Upstox API documentation: <https://upstox.com/developer/api-documentation/>

NSE Indices Nifty 200: <https://www.niftyindices.com/indices/equity/broad-based-indices/NIFTY-200>
