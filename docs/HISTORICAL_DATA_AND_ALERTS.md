# Historical market-data backend and local alerts history

Added 28 September 2026. These features are independent: local alerts are stored by
Trade M3; market candles are retrieved from Upstox on demand.

## Alerts history: user workflow

1. Open **Alerts history** from Live alerts or Paper terminal.
2. Choose a stored **Trading date (IST)**. Dates are listed newest first with counts.
3. Choose newest-first or oldest-first alert time order.
4. Click **Export selected date CSV**. The downloaded filename includes the date,
   for example `trade_m3_alerts_2026-09-25.csv`.
5. Upload that file to ChatGPT for review. It contains all stored alerts for the selected
   day, not only the visible/current trading day. No Upstox login is required for this.

The CSV includes original signal prices, stock stop (where recorded), sector snapshot,
option strike/type/expiry/key/lot/quotes/delta/reason, and linked paper state, quantity,
entry, realised P&L, fees, exit reason, uncertainty and settings. Option quotes are the
selection snapshot, not the paper fill. Paper entry and P&L have separate columns.
Unrealised P&L is not fabricated for historical exports. OPEN means still unresolved.
Sector data is from the alert time; linked paper results are their latest saved state.

Only this installation's SQLite history is shown. Rule resets retain its append-only
alert audit. Removing the database, switching DATABASE_PATH, or using a different PC
changes which dates are available. Alerts never observed or saved cannot be recreated by
this page. Older snapshots may lack fields such as stock stop; available original event
rows supplement those fields, otherwise they stay blank. No historic sector or option
candidate is reconstructed using present-day data.

Endpoints:

- `GET /api/review/dates` -> dates/counts, newest first.
- `GET /api/review/alerts?trading_date=2026-09-25&order=asc` -> full records for one day.
- `GET /api/review/export?trading_date=2026-09-25&order=desc` -> UTF-8 CSV for one day.
- Omitting trading_date on the export retains the all-dates export for compatibility.

CSV uses UTF-8 BOM for Excel and neutralises formula-like textual cells. It contains
no access tokens or broker credentials. Preserve the local database for long-term review.

## Historical data: backend API for future charts/replay

Interactive endpoint documentation is available at `/api/docs`. No chart UI, historical
trading simulation or automatic daily candle download is added by this feature.

### 1. Discover an exact contract

`GET /api/history/contracts?underlying_key=NSE_EQ%7CYOUR_ISIN&expiry=2026-09-29`

Use a real underlying instrument key from Upstox. The endpoint chooses current versus
expired option-contract discovery based on the requested expiry and returns provider
contracts. Use the exact returned instrument_key, strike, CE/PE and expiry. Expired
keys include an expiry suffix. Do not assume the current instrument key always
identifies an expired contract; exchange identifiers can be reused.

### 2. Request candles

`POST /api/history/candles`, JSON example (replace the placeholder key):

```json
{
  "instrument_key": "NSE_FO|YOUR_CONTRACT_ID",
  "from_date": "2026-09-21",
  "to_date": "2026-09-25",
  "interval": "1minute",
  "expired": false,
  "refresh": false
}
```

For an expired option, set expired=true and use the full key from discovery, for example
the shape `NSE_FO|CONTRACT_ID|25-09-2026`. The expiry suffix must be DD-MM-YYYY and the
request cannot extend past that expiry. These examples specify formats, not actual instruments.

Supported intervals: `1minute`, `3minute`, `5minute`, `15minute`, `30minute`, `day`.
The request is limited to 90 inclusive calendar days. Requests are split into 28-day
chunks to stay within the tighter documented short-interval limit. Future dates are rejected.
For active instruments, requests including today fetch today's minute candles through
the intraday endpoint separately. In-progress candles and today's daily candle are excluded.

Response contains instrument identity, interval, range, source, IST timestamps, fetched_at,
cache_hit, request_chunks, count, status (AVAILABLE/EMPTY), discarded_rows, warnings and
chronologically sorted candles with open/high/low/close/volume/open_interest. Decimal
prices are strings to preserve precision. Rows are validated, deduplicated by timestamp,
filtered to the requested range and restricted to completed periods. Invalid rows are
counted, not passed to replay. Missing open interest remains null.

Cache is persisted in SQLite by the full request, including contract key/expiry mode,
interval and date range. Completed historical requests cache for 24 hours; requests
including today cache for 30 seconds. refresh=true bypasses the cache. Valid cached
responses can be read without a current login. Cache misses/expired cache need login.
A failed chunk prevents caching a misleading partial result. Empty successful responses
are labelled EMPTY; no zero-priced or interpolated candles are generated.

## Challenges and boundaries

1. **Expired API entitlement:** Upstox documents UDAPI1149 for accounts without Plus.
   The user has confirmed Plus; actual token entitlement still needs a live API check.
   PLUS_REQUIRED, ACCESS_DENIED, LOGIN_REQUIRED and RATE_LIMITED are explicit errors.
   Provider tokens and raw exception URLs are never returned in these errors.
2. **Coverage:** history exists only during a contract's life and only where the provider
   has data. A 90-day request is not a guarantee of 90 days of option candles. Holiday,
   non-trading and illiquid intervals can be absent; coverage is not assumed complete.
3. **Candle ambiguity:** OHLC is not a tick path. If target and stop lie within the same
   candle, a future replay must mark ordering ambiguous. It must not invent a winner.
4. **Execution information:** these candles do not supply the historic bid/ask book,
   queue position or a guaranteed fill. They cannot retrospectively reproduce the
   live paper engine's depth and latency model.
5. **Provider interval differences:** the expired API parameter documentation lists
   1/3/5/15/30-minute and daily intervals, while its generic error table is inconsistent.
   Unsupported provider requests surface as PROVIDER_ERROR instead of silently changing
   interval. Access and live availability have not been verified with the user's token.
6. **Operational isolation:** historical requests have their own lock and bounded HTTP
   timeouts; no history request invokes the option order simulator or a broker order API.
   There is no background bulk download competing with live option monitoring.

## Sources

Official Upstox documentation reviewed 28 September 2026:

- [Historical candles V3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)
- [Intraday candles V3](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/)
- [Expired option contracts](https://upstox.com/developer/api-documentation/get-expired-option-contracts/)
- [Expired historical candles](https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/)

Verification covers request routing, expiry validation, chunking, cache reuse, invalid/incomplete
candles, entitlement errors, failed-chunk handling, local dates, sorting, joins and CSV isolation.
Real authenticated Upstox responses remain to be checked during user operation.
