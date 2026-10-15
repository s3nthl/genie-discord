"""/trade: turn one trade into a full ChaiStreet alert, preview it, then post it.

You give Genie the trade you did (ticker, expiry, strike, type).
Genie pulls live data (price, Bollinger bands, RSI, earnings, option chain)
and builds the alert in your format:
  - the trade itself (CSP / call)
  - a spread version + a smaller-account width
  - a LEAP idea and a shares/DCA idea when the chart says "beaten down"
    (near lower Bollinger band or RSI oversold), or when you force them on
  - an earnings heads-up if earnings land before expiry
You see a private preview first. Nothing posts until you press Post.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime

import discord
from discord import app_commands

log = logging.getLogger("genie.trade")

ALERT_CHANNEL = os.getenv("GENIE_ALERT_CHANNEL", "trade-alerts")


# ---------- data ----------

@dataclass
class Snapshot:
    price: float
    high_52w: float
    bb_lower: float
    bb_mid: float
    rsi: float
    earnings: date | None
    leg_mid: dict = field(default_factory=dict)   # (expiry_str, strike, "P"/"C") -> mid price
    leap_expiry: date | None = None
    leap_strike: float | None = None
    leap_mid: float | None = None


def parse_expiry(text: str) -> date:
    """Accepts 11/13, 11/13/26, 2026-11-13, 13 NOV 26, Nov 13."""
    from dateutil import parser
    today = date.today()
    d = parser.parse(text, default=datetime(today.year, 1, 1), dayfirst=False).date()
    if d < today:  # "1/15" typed in October means next year
        d = d.replace(year=d.year + 1)
    return d


def _rsi(closes, n=14) -> float:
    delta = closes.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down
    return float(100 - 100 / (1 + rs.iloc[-1]))


def _mid(row) -> float | None:
    bid, ask, last = float(row.get("bid", 0) or 0), float(row.get("ask", 0) or 0), float(row.get("lastPrice", 0) or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return round(last, 2) if last > 0 else None


def fetch_snapshot(ticker: str, expiry: date, legs: list[tuple[float, str]]) -> Snapshot:
    """Blocking: call through asyncio.to_thread."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    hist = t.history(period="1y", interval="1d")
    if hist.empty:
        raise ValueError(f"No price data for {ticker}")
    closes = hist["Close"]
    price = float(closes.iloc[-1])
    sma = closes.rolling(20).mean()
    sd = closes.rolling(20).std()

    earnings = None
    try:
        cal = t.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            earnings = ed[0] if isinstance(ed, list) else ed
            earnings = earnings if isinstance(earnings, date) else None
    except Exception:
        log.info("no earnings date for %s", ticker)

    snap = Snapshot(
        price=price,
        high_52w=float(hist["High"].max()),
        bb_lower=float(sma.iloc[-1] - 2 * sd.iloc[-1]),
        bb_mid=float(sma.iloc[-1]),
        rsi=_rsi(closes),
        earnings=earnings,
    )

    exps = list(t.options)
    exp_s = expiry.isoformat()
    if exp_s in exps:
        chain = t.option_chain(exp_s)
        for strike, kind in legs:
            df = chain.puts if kind == "P" else chain.calls
            hit = df[df["strike"] == strike]
            if not hit.empty:
                snap.leg_mid[(exp_s, strike, kind)] = _mid(hit.iloc[0])

    # LEAP: furthest January expiry at least ~12 months out, strike ~15% in the money.
    jans = [e for e in exps if e[5:7] == "01" and (date.fromisoformat(e) - date.today()).days >= 330]
    if jans:
        le = jans[0]
        calls = t.option_chain(le).calls
        target = price * 0.85
        idx = (calls["strike"] - target).abs().idxmin()
        row = calls.loc[idx]
        snap.leap_expiry = date.fromisoformat(le)
        snap.leap_strike = float(row["strike"])
        snap.leap_mid = _mid(row)
    return snap


# ---------- logic (pure, testable) ----------

def spread_width(price: float) -> float:
    if price >= 150:
        return 10
    if price >= 50:
        return 5
    return 2.5


def _fmt(x: float) -> str:
    return f"{x:g}" if x == int(x) else f"{x:.2f}"


def _d(d: date) -> str:
    return f"{d.month}/{d.day}"


def build_alert(ticker: str, expiry: date, strike: float, kind: str, snap: Snapshot,
                premium: float | None = None, leap: bool | None = None,
                shares: bool | None = None, note: str | None = None) -> str:
    """kind: 'put' (cash-secured put) or 'call' (long call)."""
    tk = ticker.upper()
    p = snap.price
    off_high = (1 - p / snap.high_52w) * 100
    near_band = p <= snap.bb_lower * 1.03
    oversold = snap.rsi < 35
    beaten = near_band or oversold
    show_leap = beaten if leap is None else leap
    show_shares = beaten if shares is None else shares
    w = spread_width(p)
    sw = max(w / 2, 1)
    exp_s = expiry.isoformat()

    ctx = [f"Trading at {p:.2f}"]
    if off_high >= 10:
        ctx.append(f"down {off_high:.0f}% from the 52-week high")
    if near_band:
        ctx.append("sitting on the lower Bollinger band")
    if oversold:
        ctx.append(f"RSI {snap.rsi:.0f}, oversold")
    lines = [f"**{tk}**", ", ".join(ctx) + ".", ""]

    if kind == "put":
        prem = premium or snap.leg_mid.get((exp_s, strike, "P"))
        dist = (1 - strike / p) * 100
        at = f" @ {prem:.2f}" if prem else ""
        lines.append(f"CSP idea - {_d(expiry)} {_fmt(strike)}P{at} (~{dist:.0f}% below current)")
        lines.append("")
        long_k = strike - w
        lines.append(f"spread idea - {_d(expiry)} {_fmt(strike)}/{_fmt(long_k)} Bull Put Spread (Max width ${_fmt(w)}, defined risk)")
        lines.append(f"STO {_fmt(strike)} Put")
        lines.append(f"BTO {_fmt(long_k)} Put")
        short_m, long_m = snap.leg_mid.get((exp_s, strike, "P")), snap.leg_mid.get((exp_s, long_k, "P"))
        if short_m and long_m and short_m > long_m:
            lines.append(f"~{short_m - long_m:.2f} credit")
        lines.append("")
        lines.append(f"smaller account - {_fmt(strike)}/{_fmt(strike - sw)} (Max width ${_fmt(sw)})")
    else:
        prem = premium or snap.leg_mid.get((exp_s, strike, "C"))
        at = f" @ {prem:.2f}" if prem else ""
        lines.append(f"call idea - BTO {_d(expiry)} {_fmt(strike)}C{at}")
        lines.append("")
        lines.append(f"spread idea - {_d(expiry)} {_fmt(strike)}/{_fmt(strike + w)} Bull Call Spread (Max width ${_fmt(w)}, defined risk)")
        lines.append(f"BTO {_fmt(strike)} Call")
        lines.append(f"STO {_fmt(strike + w)} Call")

    if show_leap or show_shares:
        lines.append("")
        lines.append("long term -" + (" chart says beaten down, so:" if beaten else ""))
        if show_leap and snap.leap_expiry and snap.leap_strike:
            at = f" @ {snap.leap_mid:.2f}" if snap.leap_mid else ""
            lines.append(f"LEAP: Jan '{snap.leap_expiry.year % 100} {_fmt(snap.leap_strike)}C{at} (in the money, moves close to the stock)")
        if show_shares:
            lo = min(snap.bb_lower, p)
            lines.append(f"shares: start a DCA in the {lo:.0f}-{p:.0f} zone")

    if snap.earnings and date.today() <= snap.earnings <= expiry:
        lines += ["", f"Heads up: earnings {_d(snap.earnings)}, before expiry."]
    if note:
        lines += ["", note]
    lines += ["", "Not financial advice."]
    return "\n".join(lines)


# ---------- Discord wiring ----------

class PreviewView(discord.ui.View):
    def __init__(self, author_id: int, content: str):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.content = content

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Post to #" + ALERT_CHANNEL, style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, _: discord.ui.Button):
        ch = discord.utils.get(interaction.guild.text_channels, name=ALERT_CHANNEL)
        if ch is None:
            await interaction.response.edit_message(content=f"Can't find #{ALERT_CHANNEL}.", view=None)
            return
        await ch.send(self.content)
        await interaction.response.edit_message(content=f"Posted to {ch.mention}.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Nothing posted.", view=None)
        self.stop()


def register(tree: app_commands.CommandTree):
    @tree.command(name="trade", description="Build a trade alert from your trade, preview it, then post")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        ticker="e.g. NBIS",
        expiry="e.g. 11/13 or 2026-11-13",
        strike="e.g. 170",
        kind="put = cash-secured put, call = long call",
        premium="optional: your fill price",
        leap="force LEAP idea on/off (default: auto from chart)",
        shares="force shares/DCA idea on/off (default: auto from chart)",
        note="optional line at the end",
    )
    @app_commands.choices(kind=[app_commands.Choice(name="put", value="put"),
                                app_commands.Choice(name="call", value="call")])
    async def trade(interaction: discord.Interaction, ticker: str, expiry: str, strike: float,
                    kind: app_commands.Choice[str], premium: float | None = None,
                    leap: bool | None = None, shares: bool | None = None, note: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            exp = parse_expiry(expiry)
        except Exception:
            await interaction.followup.send(f"Can't read expiry `{expiry}`. Try 11/13 or 2026-11-13.", ephemeral=True)
            return
        k = kind.value
        tag = "P" if k == "put" else "C"
        # price needed for width, so fetch legs for both common widths
        legs = [(strike, tag)] + [(strike - w, "P") for w in (10, 5, 2.5)] if k == "put" else [(strike, tag)]
        try:
            snap = await asyncio.to_thread(fetch_snapshot, ticker, exp, legs)
        except Exception as e:
            log.exception("snapshot failed")
            await interaction.followup.send(f"Couldn't pull data for {ticker.upper()}: `{e}`", ephemeral=True)
            return
        text = build_alert(ticker, exp, strike, k, snap, premium, leap, shares, note)
        await interaction.followup.send(f"**Preview** (only you see this)\n\n{text}",
                                        view=PreviewView(interaction.user.id, text), ephemeral=True)
