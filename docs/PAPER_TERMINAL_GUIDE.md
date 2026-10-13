# Paper terminal: daily operation

1. Start Trade M3, connect Upstox from **Stock alerts**, and load Nifty 200. The paper engine needs live option contract and chain data; Upstox Plus does not remove the need for a current login.
   A connected Upstox login is not the same as a connected live market feed. If the
   terminal says **Market feed disconnected**, inspect its reported feed error and
   subscription counts, then use **Retry live market feed**. If retry still fails,
   reconnect Upstox from Stock alerts and check the feed status there. Auto paper
   skips entries while the feed is disconnected because it could not monitor exits.
   Keep the same `data/trade_m3.db` when updating the program. A fresh checkout or changed
   `DATABASE_PATH` starts a new local account (₹1,00,000, alert IDs from 1, auto mode off).
   To restore earlier alerts and settings, stop both instances and copy the original
   `data/trade_m3.db` into the new checkout before starting it. Do not overwrite a database
   that contains trades you need; make a backup first.
2. Open **Paper terminal**. Set virtual starting cash, budget per trade, lots (0 fits as many whole lots as the budget can fund), target, stop and maximum hold. Save defaults and enable **Automatically paper-trade new eligible alerts**. The default premium target is +10% and stop is −5%, a 1:2 stop-to-target ratio. Both are editable. Starting cash locks after the first trade.
3. A new stock LONG signal selects a call to buy (CE); a SHORT signal selects a put to buy (PE). The option candidate shows strike, expiry, lot, bid/ask, delta when available, and selection reason. Sector context remains on the stock alert. An ATM or near ITM option with usable quotes can be selected even when delta or displayed depth is missing; the fallback is flagged. An unavailable option, unaffordable lot, missing quote, disconnected account or closed session yields an explicit skip reason.
4. Auto mode records an immediate **simulated BUY at the option-chain ask**, with an ENTRY fill and target/stop child orders. Manual **Paper trade** offers one-trade budget, lot, target, stop and holding-time overrides. Each alert is submitted at most once. Auto mode only handles alerts generated after it was enabled; old alerts are never replayed as fills.
   The home Signal tape button opens this manual dialog in place. A recovered alert may be
   manually traded later **using a new option quote at the time of the click**, if the feed
   is connected and the session is open. Its earlier candle price is not used as an entry.
5. Open positions mark to observed option bids. The first observed bid at or above the target or at or below the stop immediately records a simulated SELL at that bid. A price gap may lose more than 5%. A time or session exit waits for a usable live quote. No artificial delay, slippage or brokerage is added to new simulated fills. These quote-based prices are assumptions, not guaranteed exchange executions.
6. Review **Open positions**, **All orders**, **Executed fills** and **Closed / cancelled**. Virtual cash pays for entries and receives proceeds from exits. A ₹10,000 per-trade cap cannot fund even one option lot whose ask times lot size exceeds ₹10,000. Available virtual cash also limits concurrent trades. Open positions with stale quotes show unavailable P&L instead of a fabricated mark.

Entry cutoff is 15:15 IST and session exit cutoff is 15:20 IST; late entries may have less than five minutes to exit. The default expiry filter for a new database is zero days, so today's expiry can be considered. Existing saved settings persist after an update: set Minimum expiry days to zero and save if desired. The program reads actual contract expiries rather than guessing dates. When ATM and nearby ITM contracts exceed the budget, it can select one of the three nearest quoted OTM strikes if a whole lot fits. Some Nifty 200 stocks have no listed option or no affordable whole lot. Local alert history and paper trade CSVs are separate exports. Keep the `data` folder backed up and credentials out of GitHub.

On the stock dashboard, the signal tape sorts newest first by the time Trade M3 generated the alert. Recovered alerts display their original market candle time separately. Old records can be inspected and exported from **Alerts history**; they receive no reconstructed paper fill.
The tape also refreshes from the durable local audit log, so a stalled browser event stream
or a rule reset does not hide alerts that Alerts history still retains. Older audit rows
without a recorded creation timestamp show “time unavailable” rather than mislabeling the
market candle time as the time the alert appeared.

This is a deliberately simple forward paper model. Entry uses the first option trade reported at or after the next one-minute boundary as a live approximation of the next candle open; exits use observed option bids. It cannot establish that those prices would have filled in the exchange order book. A positive paper result does not establish a real trading edge. See [research and design](PAPER_TRADING_DESIGN.md).
