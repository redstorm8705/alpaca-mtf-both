"""
log_fill.py — Log a manually executed Robinhood options fill.

Usage:
    python3.10 log_fill.py SYMBOL DIRECTION STRIKE EXPIRY PREMIUM CONTRACTS [--exit PRICE] [--notes "..."]

Examples:
    python3.10 log_fill.py AAPL call 230 2026-04-10 2.50 1          # opening fill
    python3.10 log_fill.py AAPL call 230 2026-04-10 2.50 1 --exit 5.00  # close with exit price
    python3.10 log_fill.py AAPL call 230 2026-04-10 2.50 1 --notes "CPI beat, momentum strong"
"""

import json
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT            = ZoneInfo("America/Los_Angeles")
BASE_DIR      = Path(__file__).resolve().parent
TRADES_FILE   = BASE_DIR / "logs" / "robinhood_trades.json"


def load_trades() -> list:
    if not TRADES_FILE.exists():
        return []
    try:
        return json.loads(TRADES_FILE.read_text())
    except Exception:
        return []


def save_trades(trades: list) -> None:
    tmp = TRADES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(trades, indent=2))
    tmp.replace(TRADES_FILE)


def main():
    parser = argparse.ArgumentParser(description="Log a Robinhood options fill")
    parser.add_argument("symbol",     help="Ticker e.g. AAPL")
    parser.add_argument("direction",  choices=["call", "put"], help="call or put")
    parser.add_argument("strike",     type=float, help="Strike price e.g. 230")
    parser.add_argument("expiry",     help="Expiry date e.g. 2026-04-10 or 4/10")
    parser.add_argument("premium",    type=float, help="Premium paid per share e.g. 2.50")
    parser.add_argument("contracts",  type=int,   help="Number of contracts")
    parser.add_argument("--exit",          type=float, default=None, help="Exit premium (closes position)")
    parser.add_argument("--entry-premium", type=float, default=None,
                        help="Actual entry premium for external closes without a matching open position. "
                             "If omitted, 'premium' arg is used and a warning is printed.")
    parser.add_argument("--notes",    default="", help="Optional notes")
    args = parser.parse_args()

    # Normalise expiry to YYYY-MM-DD
    expiry = args.expiry
    if "/" in expiry:
        parts = expiry.split("/")
        if len(parts) == 2:
            year = datetime.now(PT).year
            expiry = f"{year}-{int(parts[0]):02d}-{int(parts[1]):02d}"

    symbol    = args.symbol.upper()
    total_cost = round(args.premium * 100 * args.contracts, 2)
    now_str    = datetime.now(PT).isoformat()

    trades = load_trades()

    # Check if this is a close on an existing open position
    existing_idx = None
    for i, t in enumerate(trades):
        if (t["symbol"] == symbol and
            t["direction"] == args.direction and
            t["strike"] == args.strike and
            t["expiry"] == expiry and
            t["status"] == "open"):
            existing_idx = i
            break

    if args.exit is not None and existing_idx is not None:
        # Close existing position
        t = trades[existing_idx]
        entry   = float(t["entry_premium"])
        pl      = round((args.exit - entry) * 100 * t["contracts"], 2)
        pl_pct  = round((args.exit - entry) / entry * 100, 1)
        t.update({
            "exit_premium": args.exit,
            "exit_time":    now_str,
            "pnl":          pl,
            "pnl_pct":      pl_pct,
            "status":       "closed",
            "notes":        args.notes or t.get("notes", ""),
        })
        print(f"✅ Closed {symbol} {args.direction.upper()} ${args.strike:.0f} {expiry}")
        print(f"   P&L: ${pl:+.2f} ({pl_pct:+.1f}%)")
    elif args.exit is not None:
        # Exit flag but no matching open position — log as closed.
        # Use --entry-premium if provided; fall back to 'premium' arg with a loud
        # warning so the operator knows the P&L calculation may be incorrect.
        _entry_px = args.entry_premium if args.entry_premium is not None else args.premium
        if args.entry_premium is None:
            print(
                f"⚠️  WARNING: No matching open position found and --entry-premium not provided.\n"
                f"   Using 'premium' arg (${args.premium:.2f}) as entry premium for P&L.\n"
                f"   If incorrect, re-run with: --entry-premium <actual_entry_price>"
            )
        pl     = round((args.exit - _entry_px) * 100 * args.contracts, 2)
        pl_pct = round((args.exit - _entry_px) / _entry_px * 100, 1) if _entry_px else 0.0
        trades.append({
            "symbol":          symbol,
            "direction":       args.direction,
            "strike":          args.strike,
            "expiry":          expiry,
            "entry_premium":   _entry_px,
            "exit_premium":    args.exit,
            "contracts":       args.contracts,
            "total_cost":      total_cost,
            "pnl":             pl,
            "pnl_pct":         pl_pct,
            "entry_time":      now_str,
            "exit_time":       now_str,
            "status":          "closed",
            "notes":           args.notes,
        })
        print(f"✅ Logged closed position: {symbol} {args.direction.upper()} ${args.strike:.0f} | P&L ${pl:+.2f}")
    else:
        # New opening fill
        new_trade = {
            "symbol":          symbol,
            "direction":       args.direction,
            "strike":          args.strike,
            "expiry":          expiry,
            "entry_premium":   args.premium,
            "exit_premium":    None,
            "contracts":       args.contracts,
            "total_cost":      total_cost,
            "pnl":             None,
            "pnl_pct":         None,
            "entry_time":      now_str,
            "exit_time":       None,
            "status":          "open",
            "notes":           args.notes,
        }
        trades.append(new_trade)
        print(f"✅ Logged: {symbol} {args.direction.upper()} ${args.strike:.0f} {expiry} — {args.contracts} contract(s) @ ${args.premium:.2f}")
        print(f"   Cost: ${total_cost:.2f} | Target: ${args.premium * 2:.2f} limit sell | Stop: ${args.premium * 0.50:.2f} (mental)")

    save_trades(trades)
    print(f"   Saved → {TRADES_FILE}")


if __name__ == "__main__":
    main()
