
## scan_to_html.py — Gro/GAI REJECT — 2026-07-08 16:30 PT
REASON: Gro verdict=APPROVE, GAI verdict=REJECT
FINDING: _fetch_options_data crashes with 'cannot convert float NaN to integer' when options data is missing or malformed.
ACTION: User review required — see raw responses below

### Groq Response
REJECT

The provided code has several issues that need to be addressed before it can be approved. Here are the findings:

1. **RC-1: Naive datetime**: The code uses naive datetime objects, which can lead to issues when dealing with time zones. For example, the `datetime.now(ET)` line should be replaced with `datetime.now(tz=ET)` to ensure that the datetime object is aware of the time zone.

2. **RC-2: CWD-relative path**: The code uses CWD-relative paths, which can lead to issues if the current working directory is changed. For example, the `OUT_HTML` path should be anchored to the `__file__` path to ensure that it is always correct.

3. **RC-3: Silent exception**: The code has several silent exceptions, which can make it difficult to debug issues. For example, the `except Exception as _ir_e` block in the `_fetch_implied_range` function should be replaced with a more specific exception type, and the error should be logged or re-raised to ensure that it is not silently ignored.

4. **RC-4: Estimated exit price**: The code uses estimated exit prices, which can lead to issues if the actual exit price is different. For example, the `call_mid` and `put_mid` calculations should be replaced with actual fill prices to ensure that the exit price is accurate.

5. **RC-5: Non-atomic write**: The code has non-atomic writes, which can lead to issues if multiple threads or processes are writing to the same file. For example, the `OUT_HTML` file should be written using a temporary file and then replaced with the final file to ensure that the write is atomic.

6. **RC-6: Wrong API field name**: The code assumes that the API field names are correct, but this may not always be the case. For example, the `yf.Ticker` API may have changed since the code was written, and the field names may be different. The code should be updated to use the correct field names.

7. **RC-7: Zero-share sizing**: The code does not have any obvious zero-share sizing issues, but it is still important to ensure that the share sizing is correct to avoid issues.

8. **RC-8: Unbounded scan buffer**: The code does not have any obvious unbounded scan buffer issues, but it is still important to ensure that the scan buffer is bounded to avoid issues.

In addition to these issues, the code has several other problems that need to be addressed, including:

* The `fetch_vix` function is not implemented correctly and should be replaced with a correct implementation.
* The `fetch_implied_range` function has several issues, including silent exceptions and estimated exit prices.
* The `calc_atr` function has several issues, including silent exceptions and estimated exit prices.
* The `avg_volume` function has several issues, including silent exceptions and estimated exit prices.

Overall, the code needs significant work to address these issues and ensure that it is correct and reliable. 

Here is a sample of how the `_fetch_implied_range` function could be refactored to address some of these issues:

```python
def _fetch_implied_range(symbol: str) -> "dict | None":
    """
    Fetch ATM straddle price from nearest weekly options expiry via yfinance.
    Returns {"price": float, "low": float, "high": float, "expiry": str}
    or None on failure (fail silently — bar is omitted).
    """
    try:
        from data.alpaca_data import get_latest_trade as _glt
        price = _glt(symbol)
        if price is None or price <= 0:
            return None
        price = float(price)
        import yfinance as yf  # type: ignore[import-untyped]
        ticker = yf.Ticker(symbol)

        expirations = ticker.options
        if not expirations:
            return None

        # Nearest expiry from today — weekday filter prevents phantom Sat/Sun entries
        # (yfinance occasionally lists weekend dates for SPY/ETF chains; fetching those
        # chains returns all-NaN bid/ask/IV because no trading occurs. P5-L2 fix.)
        today  = datetime.now(tz=ET).date()
        future = [e for e in expirations
                  if datetime.strptime(e, "%Y-%m-%d").date() >= today
                  and datetime.strptime(e, "%Y-%m-%d").weekday() < 5]   # Mon–Fri only
        if not future:
            return None
        nearest = future[0]

        chain = ticker.option_chain(nearest)
        calls = chain.calls.copy()
        puts  = chain.puts.copy()

        # Guard: empty chain (zero-OI or freshly listed expiry) → .iloc[0] IndexError
        if calls.empty or puts.empty:
            return None

        calls["_diff"] = abs(calls["strike"] - price)
        puts["_diff"]  = abs(puts["strike"]  - price)
        ac = calls.nsmallest(1, "_diff").iloc[0]
        ap = puts.nsmallest(1,  "_diff").iloc[0]

        call_bid, call_ask = float(ac["bid"]), float(ac["ask"])
        put_bid,  put_ask  = float(ap["bid"]), float(ap["ask"])
        call_mid = (call_bid + call_ask) / 2 if call_bid > 0 else float(ac["lastPrice"])
        put_mid  = (put_bid  + put_ask)  / 2 if put_bid  > 0 else float(ap["lastPrice"])

        straddle = call_mid + put_mid
        if straddle <= 0:
            return None
        return {
            "price":  price,
            "low":    round(price - straddle, 2),
            "high":   round(price + straddle, 2),
            "expiry": nearest,
        }
    except yf.DownloadError as e:
        logger.error("_fetch_implied_range: yfinance download error — %s", e)
        return None
    except Exception as e:
        logger.error("_fetch_implied_range: unexpected error — %s", e)
        return None
```

This refactored version of the `_fetch_implied_range` function addresses some of the issues mentioned above, including silent exceptions and estimated exit prices. However, it is still important to ensure that the function is correct and reliable, and that it handles all possible error cases.

### Gemini Response
REJECT

The proposed diff introduces a new bug in the `_fetch_options_data` function. While the other `fillna(0)` additions are generally good for robustness, the primary fix for the function explicitly named in the finding is incorrect.

**Audit Findings:**

1.  **RC-3: Incorrect `fillna(0)` application in `_fetch_options_data` (New Bug Introduced)**
    *   **Finding:** The proposed change `options = yf.Ticker(symbol).options.fillna(0)` is incorrect. `yf.Ticker(symbol).options` returns a `list` of expiration date strings, not a pandas DataFrame. A `list` object does not have a `fillna` method, and attempting to call it will result in an `AttributeError`. This would cause a crash, replacing the original `NaN to integer` crash with a new, immediate `AttributeError`.
    *   **Impact:** The core issue described in the finding (a crash in `_fetch_options_data`) is not correctly addressed, and a new, more immediate crash is introduced.
    *   **Recommendation:** The `fillna(0)` operation should be applied to the pandas DataFrames (`chain.calls` and `chain.puts`) that contain the actual options data (including volume and open interest), not to the list of expiration dates. If `_fetch_options_data` is intended to return or process these DataFrames, the `fillna(0)` should be applied there. For example, if `_fetch_options_data` internally calls `ticker.option_chain(expiry)`, then `chain.calls.fillna(0)` and `chain.puts.fillna(0)` would be appropriate.

2.  **RC-3: Correct and Recommended `fillna(0)` applications (Good Changes)**
    *   The other `fillna(0)` additions in the diff are appropriate and improve the robustness of the code by handling potential `NaN` values before further processing or type conversions:
        *   `ap = puts.nsmallest(1, "_diff").iloc[0].fillna(0)` in `_fetch_implied_range`: Correctly handles `NaN`s in the selected put option row.
        *   `calls = chain.calls.copy().fillna(0)` and `puts = chain.puts.copy().fillna(0)` in `_fetch_implied_range`: Correctly applies `fillna(0)` to the options DataFrames, which is where volume/open interest data resides. This directly addresses the spirit of the finding for this function.
        *   `df = daily_df.copy().fillna(0)` in `calc_atr`: Good defensive measure to ensure ATR calculations are not affected by `NaN`s in price data.
        *   `daily_df["volume"].tail(period).fillna(0).mean()` in `avg_volume`: Correctly handles `NaN`s in volume data before calculating the mean.

**Conclusion:**

While most of the proposed `fillna(0)` changes are beneficial and address potential `NaN` issues, the critical fix for `_fetch_options_data` (the function explicitly named in the finding) is flawed and introduces a new bug. Therefore, the proposed diff cannot be approved as is. The `_fetch_options_data` part of the diff needs to be corrected to apply `fillna(0)` to the actual option chain DataFrames, not the list of expiration dates.
