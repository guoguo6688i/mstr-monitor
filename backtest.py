# -*- coding: utf-8 -*-
"""
MSTR 按 mNAV 区间买卖策略回测（FD口径，与 mnav.com gross 一致）
- 在 MSTR 与 BTC（现金/无溢价替代）之间轮动
- 计入交易成本；按完整交易统计胜率/PF；阈值网格稳健性检验
"""
import requests, json, csv, datetime, bisect
import numpy as np

# ---------- 历史 BTC 持仓节点（SEC财报/新闻稿） ----------
HOLD_NODES = [
    ("2020-09-30", 38250), ("2020-12-31", 70470),
    ("2021-03-31", 91326), ("2021-06-30", 105085),
    ("2021-09-30", 114042), ("2021-12-31", 124391),
    ("2022-03-31", 129218), ("2022-06-30", 129699),
    ("2022-09-30", 130000), ("2022-12-31", 132500),
    ("2023-03-31", 140000), ("2023-06-30", 152333),
    ("2023-09-30", 158245), ("2023-12-31", 189150),
    ("2024-03-31", 214278), ("2024-06-30", 226500),
    ("2024-09-30", 252355), ("2024-12-31", 447470),
    ("2025-04-28", 553555), ("2025-10-13", 640250),
    ("2025-12-31", 713502), ("2026-03-31", 803101),
    ("2026-05-03", 818334), ("2026-05-25", 843738),
    ("2026-07-26", 843775), ("2026-09-27", 847666),
    ("2026-10-04", 848000), ("2026-10-11", 848250),
]
SPLIT_DATE = "2024-08-07"
COST = 0.0005  # 每边 0.05%


def fetch_yahoo(sym):
    p1 = int(datetime.datetime(2020, 8, 1).timestamp())
    p2 = int(datetime.datetime(2026, 10, 20).timestamp())
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=30,
    )
    res = r.json()["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    out = {}
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
        out[d] = float(q["close"][i])
    return out


def fetch_shares():
    r = requests.get(
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001050446.json",
        headers={"User-Agent": "research test@example.com"}, timeout=60,
    )
    cs = r.json()["facts"]["us-gaap"]
    nodes = {}
    for cname in ["WeightedAverageNumberOfDilutedSharesOutstanding",
                  "WeightedAverageNumberOfSharesOutstandingBasic"]:
        for u in cs[cname]["units"]["shares"]:
            end, val = u.get("end"), u.get("val")
            if end and val and end >= "2020-08-01":
                if end not in nodes or val > nodes[end]:
                    nodes[end] = val
    for end in list(nodes):
        if nodes[end] < 50_000_000:
            nodes[end] *= 10
    nodes["2026-10-16"] = 422_000_000
    return nodes


def interp(nodes, date):
    dates = [n[0] for n in nodes]
    vals = [n[1] for n in nodes]
    i = bisect.bisect_right(dates, date) - 1
    if i < 0:
        return vals[0]
    if i >= len(dates) - 1:
        return vals[-1]
    d0, d1 = dates[i], dates[i + 1]
    span = (datetime.date.fromisoformat(d1) - datetime.date.fromisoformat(d0)).days
    frac = (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(d0)).days / span
    return vals[i] + (vals[i + 1] - vals[i]) * frac


def run_strategy(records, buy, sell):
    """在 MSTR/BTC 间轮动；返回净值曲线、交易列表、配对完整交易"""
    pos = "BTC"
    equity = 1.0
    curve = []
    open_buy = None
    trades = []
    closed = []
    for i, r in enumerate(records):
        d = r["date"]
        if i > 0:
            if pos == "MSTR":
                equity *= r["mstr"] / records[i - 1]["mstr"]
            else:
                equity *= r["btc"] / records[i - 1]["btc"]
        target = None
        if r["mnav"] <= buy:
            target = "MSTR"
        elif r["mnav"] >= sell:
            target = "BTC"
        if target and target != pos:
            if pos == "MSTR" and target == "BTC":
                equity *= (1 - COST)
                if open_buy:
                    mret = (r["mstr"] / open_buy["mstr"]) * (1 - COST) * (1 - COST)
                    bret = r["btc"] / open_buy["btc"]
                    closed.append({
                        "buy_date": open_buy["date"], "sell_date": d,
                        "mstr_buy": open_buy["mstr"], "mstr_sell": r["mstr"],
                        "btc_buy": open_buy["btc"], "btc_sell": r["btc"],
                        "mstr_ret": mret - 1, "btc_ret": bret - 1,
                        "alpha": mret - bret,
                        "days": (datetime.date.fromisoformat(d) -
                                 datetime.date.fromisoformat(open_buy["date"])).days,
                    })
                trades.append({"date": d, "action": "卖出MSTR→BTC",
                               "mnav": round(r["mnav"], 3)})
                open_buy = None
            elif pos == "BTC" and target == "MSTR":
                equity *= (1 - COST)
                open_buy = {"date": d, "mstr": r["mstr"], "btc": r["btc"]}
                trades.append({"date": d, "action": "买入MSTR",
                               "mnav": round(r["mnav"], 3),
                               "mstr": round(r["mstr"], 2)})
            pos = target
        curve.append(equity)
    return curve, trades, closed, open_buy


def perf(curve, records, closed, open_buy):
    curve = np.array(curve)
    years = len(curve) / 252
    total = curve[-1] - 1
    ann = curve[-1] ** (1 / years) - 1
    peak = np.maximum.accumulate(curve)
    maxdd = ((curve - peak) / peak).min()
    rets = np.diff(curve) / curve[:-1]
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252) if rets.std() else 0
    # 按完整交易
    n = len(closed)
    wins = [c for c in closed if c["alpha"] > 0]
    losses = [c for c in closed if c["alpha"] <= 0]
    wr = len(wins) / n if n else 0
    gw = np.mean([c["alpha"] for c in wins]) if wins else 0
    ll = -np.mean([c["alpha"] for c in losses]) if losses else 0
    pf = gw / ll if ll else float("inf")
    avg_days = np.mean([c["days"] for c in closed]) if closed else 0
    return {
        "total": total, "ann": ann, "maxdd": maxdd, "sharpe": sharpe,
        "n_round": n, "open": bool(open_buy), "win_rate": wr, "pf": pf,
        "avg_days": avg_days,
    }


def main():
    print("获取价格...")
    mstr, btc = fetch_yahoo("MSTR"), fetch_yahoo("BTC-USD")
    print("获取股本...")
    sn = fetch_shares()
    share_series = [(d, sn[d]) for d in sorted(sn)]
    dates = sorted(d for d in (set(mstr) & set(btc)) if d >= "2020-10-01")
    records = []
    for d in dates:
        hold = interp(HOLD_NODES, d)
        shares = interp(share_series, d)
        bps = hold / shares
        mnav = mstr[d] / (bps * btc[d])
        records.append({"date": d, "mstr": mstr[d], "btc": btc[d],
                        "mnav": mnav, "hold": hold, "shares": shares})
    print(f"交易日 {len(records)}，mNAV {min(r['mnav'] for r in records):.2f}"
          f"~{max(r['mnav'] for r in records):.2f}")

    # 基准
    mstr_curve = [1.0]
    for i in range(1, len(records)):
        mstr_curve.append(mstr_curve[-1] * records[i]["mstr"] / records[i - 1]["mstr"])
    btc_curve = [1.0]
    for i in range(1, len(records)):
        btc_curve.append(btc_curve[-1] * records[i]["btc"] / records[i - 1]["btc"])

    # 阈值网格
    buys = [0.8, 0.9, 1.0, 1.1]
    sells = [1.3, 1.5, 2.0]
    grid = {}
    curves_out = {"持有MSTR": mstr_curve, "持有BTC": btc_curve}
    detail = {}
    for b in buys:
        for s in sells:
            if s <= b:
                continue
            curve, trades, closed, ob = run_strategy(records, b, s)
            m = perf(curve, records, closed, ob)
            grid[f"{b}/{s}"] = m
            curves_out[f"{b}/{s}"] = curve
            detail[f"{b}/{s}"] = (trades, closed, ob)

    # 打印网格（年化）
    print("\n=== 阈值网格：年化收益（%） ===")
    head = "BUY\\SELL " + "".join(f"{s:>10}" for s in sells)
    print(head)
    for b in buys:
        line = f"{b:>7}  "
        for s in sells:
            k = f"{b}/{s}"
            line += f"{grid[k]['ann']*100:>9.1f} " if k in grid else f"{'-':>10}"
        print(line)
    print("\n=== 阈值网格：最大回撤（%） ===")
    print(head)
    for b in buys:
        line = f"{b:>7}  "
        for s in sells:
            k = f"{b}/{s}"
            line += f"{grid[k]['maxdd']*100:>9.1f} " if k in grid else f"{'-':>10}"
        print(line)

    # 主策略（选网格中 1.0/1.5）
    keys = [f"{b}/{s}" for b in buys for s in sells if s > b]
    rows = []
    rows.append(("持有MSTR", perf(mstr_curve, records, [], None)))
    rows.append(("持有BTC", perf(btc_curve, records, [], None)))
    for k in keys:
        rows.append((f"mNAV {k}", grid[k]))

    print("\n" + "=" * 104)
    print(f"{'策略':<14}{'总收益':>10}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'轮动次数':>8}{'交易胜率':>9}{'盈亏比':>7}{'平均持有天':>9}")
    print("-" * 104)
    for name, m in rows:
        print(f"{name:<14}{m['total']*100:>9.0f}%{m['ann']*100:>7.1f}%{m['maxdd']*100:>8.1f}%"
              f"{m['sharpe']:>7.2f}{m['n_round']:>8}{m['win_rate']*100:>8.0f}%{m['pf']:>7.2f}{m['avg_days']:>9.0f}")
    print("=" * 104)

    # ---- 输出 ----
    with open("mnav_daily.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "MSTR", "BTC", "Hold", "Shares", "mNAV"])
        for r in records:
            w.writerow([r["date"], round(r["mstr"], 2), round(r["btc"], 1),
                        int(r["hold"]), int(r["shares"]), round(r["mnav"], 4)])
    with open("nav_curves.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date"] + list(curves_out))
        for i, d in enumerate(dates):
            w.writerow([d] + [round(curves_out[c][i], 5) for c in curves_out])
    # 主策略 1.0/1.5 交易 + 配对明细
    mk = "1.0/1.5"
    trades, closed, ob = detail[mk]
    with open("trades_1.0_1.5.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Action", "mNAV", "MSTR"])
        for t in trades:
            w.writerow([t["date"], t["action"], t["mnav"], t.get("mstr", "")])
    with open("roundtrips_1.0_1.5.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["BuyDate", "SellDate", "Days", "MSTR_Ret", "BTC_Ret",
                    "Alpha", "MSTR_Buy", "MSTR_Sell"])
        for c in closed:
            w.writerow([c["buy_date"], c["sell_date"], c["days"],
                        round(c["mstr_ret"], 4), round(c["btc_ret"], 4),
                        round(c["alpha"], 4), round(c["mstr_buy"], 2),
                        round(c["mstr_sell"], 2)])
    # 全部 closed roundtrips（所有策略）
    with open("roundtrips_all.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Strategy", "BuyDate", "SellDate", "Days", "MSTR_Ret",
                    "BTC_Ret", "Alpha"])
        for k in keys:
            for c in detail[k][1]:
                w.writerow([k, c["buy_date"], c["sell_date"], c["days"],
                            round(c["mstr_ret"], 4), round(c["btc_ret"], 4),
                            round(c["alpha"], 4)])
    json.dump({k: {kk: (vv if not isinstance(vv, float) or vv != float("inf") else 99)
                   for kk, vv in v.items()} for k, v in grid.items()},
              open("performance.json", "w"), ensure_ascii=False, indent=2)
    print("\n已输出: mnav_daily.csv, nav_curves.csv, trades_1.0_1.5.csv, "
          "roundtrips_1.0_1.5.csv, roundtrips_all.csv, performance.json")
    return records, curves_out, detail


if __name__ == "__main__":
    main()
