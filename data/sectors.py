# Shared sector classification maps — single source of truth for both main.py
# and strategy/signal_generator.py.
#
# SECTOR_MAP    — correlation gate (main.py); None = exempt from sector gate
# SECTOR_MAP_SG — signal grouping (signal_generator.py); string labels for
#                 sector-based signal weighting

SECTOR_MAP: dict = {
    # Energy
    "XOM":  "energy", "CVX":  "energy", "COP":  "energy",
    "SLB":  "energy", "EOG":  "energy", "MPC":  "energy",
    "VLO":  "energy", "PSX":  "energy", "OXY":  "energy",
    # Semiconductors
    "NVDA": "semis",  "AMD":  "semis",  "INTC": "semis",
    "QCOM": "semis",  "MU":   "semis",  "AVGO": "semis",
    "SMCI": "semis",  "MRVL": "semis",  "ARM":  "semis",
    "TXN":  "semis",  "KLAC": "semis",  "LRCX": "semis",
    "AMAT": "semis",  "ON":   "semis",  "WOLF": "semis",
    # Memory-cycle add (2026-09-05, Rafael + board): SNDK (NAND), DRAM (Roundhill
    # Memory ETF), EWY (iShares S. Korea — Samsung/SK Hynix memory weight). Tagged
    # "semis" so the concentration gate treats them as one memory/semis cluster with
    # MU (already "semis"). EWY is a broad Korea ETF but memory-heavy — "semis" is a
    # deliberate CONSERVATIVE choice: the gate only ever REDUCES entries (blocks a
    # correlated candidate), never adds risk, so over-tagging fails safe. Closes the
    # prior ETF/unmapped bypass (unmapped names + ETFs were "always allowed through").
    "SNDK": "semis",  "DRAM": "semis",  "EWY":  "semis",
    # Big Tech / Mega-cap
    "AAPL": "bigtech", "AMZN": "bigtech", "META": "bigtech",
    "GOOGL":"bigtech", "MSFT": "bigtech", "NFLX": "bigtech",
    # Software / SaaS
    "CRM":  "software", "PLTR": "software", "SNOW": "software",
    "DDOG": "software", "MDB":  "software",  "NET":  "software",
    "SHOP": "software", "TTD":  "software",  "OKTA": "software",
    "GTLB": "software", "DOCN": "software",
    # Cybersecurity
    "CRWD": "cybersec", "PANW": "cybersec", "ZS":   "cybersec",
    # Fintech / Consumer
    "SOFI": "fintech",  "TOST": "fintech",  "UBER": "fintech",
    "COIN": "crypto",   "MSTR": "crypto",
    # EV / Auto
    "TSLA": "ev",
    # Financials
    "GS":   "financials", "MS":  "financials",
    "JPM":  "financials", "BAC": "financials",
    # Consumer tech / marketplace
    "MELI": "consumer_tech", "DASH": "consumer_tech",
    "ABNB": "consumer_tech",
    # Broad market ETFs — exempt (no sector gate)
    "SPY":  None, "QQQ":  None,
    # Leveraged ETFs — exempt (intentional hedges)
    "TQQQ": None, "SQQQ": None, "TSLL": None, "NVDL": None,
}

SECTOR_MAP_SG: dict = {
    "SPY":  "broad",   "QQQ":  "broad",   "IWM":  "broad",
    "TQQQ": "letf",    "SQQQ": "letf",    "NVDL": "letf",    "TSLL": "letf",
    "NVDA": "semis",   "AMD":  "semis",   "SMCI": "semis",
    "AAPL": "bigtech", "MSFT": "bigtech", "AMZN": "bigtech",
    "META": "bigtech", "GOOGL":"bigtech", "NFLX": "bigtech",
    "TSLA": "ev",      "RIVN": "ev",
    "XOM":  "energy",  "CVX":  "energy",
    "UBER": "consumer","ABNB": "consumer",
    "JPM":  "finance", "GS":   "finance",
    "XLK":  "tech_sector", "XLF": "finance_sector",
}
