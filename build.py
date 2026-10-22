#!/usr/bin/env python3
"""Build the market monitor dashboard.

Fetches market data (Yahoo Finance via yfinance) and economic data (FRED),
optionally values a portfolio from a holdings CSV, and writes a single
self-contained HTML page to the output folder.

    python build.py --out site            # live data (needs FRED_API_KEY)
    python build.py --demo --out site     # synthetic data, no network

Holdings are read from the HOLDINGS_CSV environment variable (a GitHub secret)
or, locally, from holdings.csv. This script never prints holdings, quantities
or values, because Actions logs are public in public repositories.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent
MARKET_YEARS = 5
MACRO_YEARS = 7          # extra history so year-over-year transforms cover 5 years
MIN_MARKET_COVERAGE = 0.5  # fail the build (keep the last good page) below this


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _clean(ser: pd.Series) -> pd.Series:
    ser = pd.to_numeric(ser, errors="coerce").dropna()
    idx = pd.to_datetime(ser.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    ser.index = idx.normalize()
    ser = ser[~ser.index.duplicated(keep="last")].sort_index()
    return ser[np.isfinite(ser.values)].astype(float)


def fetch_yahoo(symbols: list[str], start: str) -> dict[str, pd.Series]:
    import yfinance as yf

    out: dict[str, pd.Series] = {}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), 25):
        chunk = symbols[i:i + 25]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=start, auto_adjust=True, progress=False,
                                 threads=True, group_by="column")
                if df is not None and not df.empty:
                    break
            except Exception:
                pass
            time.sleep(5 * (attempt + 1))
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"]
        else:
            close = df[["Close"]].rename(columns={"Close": chunk[0]})
        if isinstance(close, pd.Series):
            close = close.to_frame(chunk[0])
        for s in chunk:
            if s in close.columns:
                ser = _clean(close[s])
                if len(ser) > 5:
                    out[s] = ser

    for s in [s for s in symbols if s not in out]:
        for attempt in range(2):
            try:
                hist = yf.Ticker(s).history(start=start, auto_adjust=True)
                ser = _clean(hist["Close"])
                if len(ser) > 5:
                    out[s] = ser
                    break
            except Exception:
                pass
            time.sleep(3)
    return out


def fetch_fred(series_id: str, start: str, api_key: str) -> pd.Series | None:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException:
            r = None  # never log the exception: its text contains the API key
        if r is not None and r.status_code == 200:
            obs = r.json().get("observations", [])
            vals = {o["date"]: o["value"] for o in obs if o.get("value") not in (".", "", None)}
            if not vals:
                return None
            return _clean(pd.Series(vals))
        if r is not None and r.status_code in (400, 404):
            log(f"  FRED {series_id}: HTTP {r.status_code}")
            return None
        time.sleep(2 * (attempt + 1))
    log(f"  FRED {series_id}: no response")
    return None


# ---------------------------------------------------------------------------
# Demo data (no network): plausible random walks so the page can be previewed
# ---------------------------------------------------------------------------

def demo_series(symbol: str, source: str, kind: str, start: str) -> pd.Series:
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    end = pd.Timestamp.today().normalize()
    if source == "fred" and kind == "macro":
        idx = pd.date_range(start, end, freq="MS")
        base = {"CPIAUCSL": 280, "CPILFESL": 290, "PCEPILFE": 120, "PAYEMS": 150000,
                "INDPRO": 102, "RSAFS": 650000}.get(symbol)
        if base:  # index levels -> growth path
            steps = rng.normal(0.0025, 0.002, len(idx))
            return pd.Series(base * np.exp(np.cumsum(steps)), idx)
        level = {"DFF": 4.3, "UNRATE": 4.2, "A191RL1Q225SBEA": 2.2, "ICSA": 225000,
                 "MORTGAGE30US": 6.4, "UMCSENT": 62}.get(symbol, 3.0)
        vol = level * 0.03
        return pd.Series(level + np.cumsum(rng.normal(0, vol, len(idx))) * 0.3, idx)
    idx = pd.bdate_range(start, end)
    if kind in ("yield", "spread") or source == "fred":
        level = {"DGS1MO": 4.2, "DGS3MO": 4.1, "DGS6MO": 4.0, "DGS1": 3.8, "DGS2": 3.6,
                 "DGS3": 3.6, "DGS5": 3.7, "DGS7": 3.9, "DGS10": 4.2, "DGS20": 4.7,
                 "DGS30": 4.8, "T10Y2Y": 0.55, "T10Y3M": 0.1, "DFII10": 1.8, "T10YIE": 2.35,
                 "BAMLC0A0CM": 0.85, "BAMLC0A4CBBB": 1.05, "BAMLH0A0HYM2": 3.0,
                 "BAMLH0A3HYC": 8.5, "BAMLEMCBPIOAS": 1.9, "NFCI": -0.5, "STLFSI4": -0.6
                 }.get(symbol, 2.0)
        walk = np.cumsum(rng.normal(0, 0.03, len(idx)))
        walk -= walk[-1]
        return pd.Series(level + walk * max(abs(level), 0.5) * 0.15, idx)
    if kind == "level":
        level = {"^VIX": 16, "^MOVE": 95, "^VVIX": 90, "^SKEW": 145}.get(symbol, 20)
        x = np.zeros(len(idx))
        for i in range(1, len(idx)):
            x[i] = x[i - 1] * 0.97 + rng.normal(0, 0.08)
        return pd.Series(level * np.exp(x), idx)
    vol = 0.6 if symbol.endswith("-USD") else (0.35 if symbol.endswith("=F") else 0.16)
    if symbol.endswith("=X") or symbol == "DX-Y.NYB":
        vol = 0.08
    if symbol in ("AGG", "SHY", "MUB", "LQD", "TIP", "IEF", "EMB", "BKLN"):
        vol = 0.05
    drift = rng.normal(0.06, 0.05)
    rets = rng.normal(drift / 252, vol / math.sqrt(252), len(idx))
    return pd.Series(100 * np.exp(np.cumsum(rets)), idx)


# ---------------------------------------------------------------------------
# Transforms and serialization
# ---------------------------------------------------------------------------

def apply_transform(ser: pd.Series, item: dict) -> pd.Series:
    t = item.get("transform")
    if t == "yoy":
        ser = (ser / ser.shift(12) - 1) * 100
    elif t == "diff":
        ser = ser.diff()
    if item.get("scale"):
        ser = ser * float(item["scale"])
    return ser.dropna()


def sig(x: float) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return float(f"{x:.6g}")


def days(idx: pd.DatetimeIndex) -> list[int]:
    return [int(d) for d in (idx.values.astype("datetime64[D]").astype(np.int64))]


def pack(ser: pd.Series) -> dict:
    return {"t": days(ser.index), "v": [sig(v) for v in ser.values]}


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def load_holdings(path: Path) -> pd.DataFrame | None:
    text = os.environ.get("HOLDINGS_CSV", "").strip()
    if text:
        df = pd.read_csv(io.StringIO(text))
    elif path.exists():
        df = pd.read_csv(path)
    else:
        return None
    df.columns = [c.strip().lower() for c in df.columns]
    for col in ("ticker", "quantity", "value", "asset_class", "name"):
        if col not in df.columns:
            df[col] = np.nan
    df["ticker"] = df["ticker"].fillna("").astype(str).str.strip().str.upper().replace({"NAN": ""})
    df["asset_class"] = df["asset_class"].fillna("Other").astype(str).str.strip()
    df["name"] = df["name"].astype(object).where(df["name"].notna(), None)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    bad = df[(df["quantity"].isna()) & (df["value"].isna())]
    if len(bad):
        log(f"  Holdings: {len(bad)} row(s) have neither quantity nor value; skipped "
            f"(rows {', '.join(str(i + 2) for i in bad.index)})")
    return df.drop(bad.index)


def build_portfolio(h: pd.DataFrame, prices: dict[str, pd.Series], bench_w: dict[str, float],
                    show_dollars: bool) -> dict | None:
    manual = h[h["quantity"].isna() | (h["ticker"].isin(["", "CASH"]))].copy()
    priced = h.drop(manual.index)
    priced = priced.groupby(["ticker", "asset_class"], as_index=False).agg(
        quantity=("quantity", "sum"), name=("name", "first"))

    missing = sorted(t for t in priced["ticker"] if t not in prices)
    priced = priced[priced["ticker"].isin(prices.keys())]
    if priced.empty and manual.empty:
        return None

    frame = pd.DataFrame({t: prices[t] for t in priced["ticker"]})
    if frame.empty:
        return None
    frame = frame[frame.index.dayofweek < 5].ffill()
    first_full = frame.dropna().index.min()
    frame = frame.loc[first_full:]
    cutoff = frame.index.max() - pd.DateOffset(years=MARKET_YEARS, days=15)
    frame = frame.loc[frame.index >= cutoff]

    values = frame * priced.set_index("ticker")["quantity"].reindex(frame.columns).values
    manual_value = float(manual["value"].fillna(manual["quantity"]).sum()) if len(manual) else 0.0
    total = values.sum(axis=1) + manual_value
    scale = float(total.iloc[-1])
    if not math.isfinite(scale) or scale <= 0:
        return None

    bench = None
    bw = {k: v for k, v in bench_w.items() if k in prices}
    if bw:
        bf = pd.DataFrame({k: prices[k] for k in bw}).reindex(frame.index).ffill().bfill()
        rets = bf.pct_change().fillna(0)
        wsum = sum(bw.values())
        brets = sum(rets[k] * (w / wsum) for k, w in bw.items())
        bench = (1 + brets).cumprod()

    holdings = []
    for _, row in priced.iterrows():
        v = values[row["ticker"]] / scale
        holdings.append({"id": row["ticker"],
                         "name": row["name"] if isinstance(row["name"], str) else row["ticker"],
                         "cls": row["asset_class"], "w": sig(v.iloc[-1]),
                         "v": [sig(x) for x in v.values]})
    for _, row in manual.iterrows():
        val = row["value"] if pd.notna(row["value"]) else row["quantity"]
        label = row["name"] if isinstance(row["name"], str) else (row["ticker"] or "Unpriced holding")
        holdings.append({"id": row["ticker"] or label, "name": label, "cls": row["asset_class"],
                         "w": sig(val / scale), "v": None})

    out = {"t": days(frame.index), "total": [sig(x) for x in (total / scale).values],
           "bench": [sig(x) for x in bench.values] if bench is not None else None,
           "holdings": holdings, "missing": missing,
           "history_start": days(pd.DatetimeIndex([frame.index.min()]))[0]}
    if show_dollars:
        out["scale"] = scale
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--holdings", default=str(ROOT / "holdings.csv"))
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    today = pd.Timestamp.today().normalize()
    mkt_start = (today - pd.DateOffset(years=MARKET_YEARS, days=30)).strftime("%Y-%m-%d")
    macro_start = (today - pd.DateOffset(years=MACRO_YEARS)).strftime("%Y-%m-%d")

    groups = cfg["groups"]
    items = [it for g in groups for it in g.get("items", [])]
    curve = [c for g in groups for c in g.get("curve", [])]

    holdings = None
    if not args.demo:
        holdings = load_holdings(Path(args.holdings))
    elif (ROOT / "holdings.example.csv").exists():
        holdings = load_holdings(ROOT / "holdings.example.csv")
    bench_w = (cfg.get("benchmark") or {}).get("weights", {})

    yahoo_syms = {it["symbol"] for it in items if it["source"] == "yahoo"} | set(bench_w)
    if holdings is not None:
        yahoo_syms |= {t for t in holdings["ticker"] if t and t != "CASH"}
    fred_syms = {it["symbol"]: it for it in items if it["source"] == "fred"}
    for c in curve:
        fred_syms.setdefault(c["symbol"], {"kind": "yield"})

    log(f"Fetching {len(yahoo_syms)} Yahoo symbols and {len(fred_syms)} FRED series")
    if args.demo:
        kinds = {it["symbol"]: it["kind"] for it in items}
        yahoo = {s: demo_series(s, "yahoo", kinds.get(s, "price"), mkt_start) for s in yahoo_syms}
        fred = {s: demo_series(s, "fred", meta.get("kind", "yield"),
                               macro_start if meta.get("kind") == "macro" else mkt_start)
                for s, meta in fred_syms.items()}
    else:
        yahoo = fetch_yahoo(sorted(yahoo_syms), mkt_start)
        key = os.environ.get("FRED_API_KEY", "").strip()
        fred = {}
        if not key:
            log("  FRED_API_KEY not set: rates, credit and economy panels will be left out")
        else:
            for s, meta in fred_syms.items():
                start = macro_start if meta.get("kind") == "macro" else mkt_start
                ser = fetch_fred(s, start, key)
                if ser is not None:
                    fred[s] = ser

    # As-of date: latest close across exchange-traded items (crypto trades weekends)
    trad = [yahoo[it["symbol"]].index.max() for it in items
            if it["source"] == "yahoo" and it["symbol"] in yahoo
            and not it["symbol"].endswith("-USD")]
    if not trad:
        log("No market data returned; keeping the previous page.")
        return 1
    as_of = max(trad)
    mkt_cut = as_of - pd.DateOffset(years=MARKET_YEARS, days=20)
    macro_cut = as_of - pd.DateOffset(years=MARKET_YEARS + 1)

    series, dropped = {}, []
    for it in items:
        raw = (yahoo if it["source"] == "yahoo" else fred).get(it["symbol"])
        if raw is None or raw.empty:
            dropped.append(it["name"])
            continue
        ser = apply_transform(raw, it)
        ser = ser[ser.index >= (macro_cut if it["kind"] == "macro" else mkt_cut)]
        if len(ser) < 2:
            dropped.append(it["name"])
            continue
        series[it["id"]] = pack(ser)
    for c in curve:
        raw = fred.get(c["symbol"])
        if raw is not None and len(raw) > 1:
            series[c["id"]] = pack(raw[raw.index >= mkt_cut])

    mkt_items = [it for it in items if it["source"] == "yahoo"]
    ok = sum(1 for it in mkt_items if it["id"] in series)
    log(f"Market items: {ok}/{len(mkt_items)}  |  all items: {len(items) - len(dropped)}/{len(items)}")
    if mkt_items and ok / len(mkt_items) < MIN_MARKET_COVERAGE:
        log("Too little market data (Yahoo may be rate-limiting); keeping the previous page.")
        return 1
    if dropped:
        log("Left out (no data): " + ", ".join(dropped))

    portfolio = None
    if holdings is not None and len(holdings):
        show_dollars = bool((cfg.get("portfolio") or {}).get("show_dollar_values", False))
        portfolio = build_portfolio(holdings, yahoo, bench_w, show_dollars)
        log("Portfolio: built" if portfolio else "Portfolio: no priced holdings")
        if portfolio and portfolio["missing"]:
            log(f"Portfolio: {len(portfolio['missing'])} ticker(s) had no price data")

    page_groups = []
    for g in groups:
        g2 = {k: v for k, v in g.items() if k not in ("items", "curve")}
        g2["items"] = [{k: v for k, v in it.items() if k not in ("source", "scale", "transform")}
                       | {"source": "FRED" if it["source"] == "fred" else "Yahoo Finance"}
                       for it in g.get("items", []) if it["id"] in series]
        if g.get("curve"):
            g2["curve"] = [c for c in g["curve"] if c["id"] in series]
        if g2["items"] or g2.get("curve"):
            page_groups.append(g2)

    payload = {
        "title": cfg.get("title", "Market monitor"),
        "demo": bool(args.demo),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "as_of": days(pd.DatetimeIndex([as_of]))[0],
        "groups": page_groups,
        "series": series,
        "dropped": dropped,
        "benchmark_name": (cfg.get("benchmark") or {}).get("name", "Benchmark"),
        "portfolio": portfolio,
    }
    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    template = (ROOT / "template.html").read_text()
    html = template.replace("__TITLE__", payload["title"]).replace("__DATA__", blob, 1)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html)
    (out / ".nojekyll").write_text("")
    log(f"Wrote {out / 'index.html'} ({len(html) / 1e6:.1f} MB), as of {as_of.date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
