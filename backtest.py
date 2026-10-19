# -*- coding: utf-8 -*-
"""
MSTR 按 mNAV 区间买卖策略回测
在 GitHub Actions（美国环境）运行：Yahoo价格 + SEC股本 + 持仓节点重建 mNAV
"""
import requests, json, csv, datetime, bisect
import numpy as np

# ============================================================
# 历史 BTC 持仓节点（来自 SEC 财报/公司新闻稿，精确；缺口线性插值）
# ============================================================
HOLD_NODES = [
    ("2020-09-30", 38250),
    ("2020-12-31", 70470),
    ("2021-03-31", 91326),
    ("2021-06-30", 105085),
    ("2021-09-30", 114042),
    ("2021-12-31", 124391),
    ("2022-03-31", 129218),
    ("2022-06-30", 129699),
    ("2022-09-30", 130000),
    ("2022-12-31", 132500),
    ("2023-03-31", 140000),
    ("2023-06-30", 152333),
    ("2023-09-30", 158245),
    ("2023-12-31", 189150),
    ("2024-03-31", 214278),
    ("2024-06-30", 226500),
    ("2024-09-30", 252355),
    ("2024-12-31", 447470),
    ("2025-04-28", 553555),
    ("2025-10-13", 640250),
    ("2026-05-03", 818334),
    ("2026-05-25", 843738),
    ("2026-07-26", 843775),
    ("2026-09-27", 847666),
    ("2026-10-04", 848000),
    ("2026-10-11", 848250),
]

# 2024-08-07 完成 10:1 拆股；拆股前股本×10
SPLIT_DATE = "2024-08-07"


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
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    out = {}
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        d = datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
        out[d] = float(q["close"][i])
    return out


def fetch_shares():
    """从 SEC companyfacts 获取股本，拆股统一调整"""
    r = requests.get(
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001050446.json",
        headers={"User-Agent": "research test@example.com"},
        timeout=60,
    )
    facts = r.json()["facts"]
    # 收集所有 us-gaap 下含 shares 的股本概念
    nodes = {}
    for taxonomy in ["us-gaap", "dei"]:
        if taxonomy not in facts:
            continue
        for concept, data in facts[taxonomy].items():
            cl = concept.lower()
            if "shares" not in cl:
                continue
            if not any(k in cl for k in ["commonstock", "entitycommon", "commonstockshares"]):
                continue
            units = data.get("units", {})
            for unit, arr in units.items():
                if "shares" not in unit:
                    continue
                for u in arr:
                    end = u.get("end")
                    val = u.get("val")
                    if end and val and end >= "2020-08-01":
                        # 拆股调整
                        if end < SPLIT_DATE:
                            val = val * 10
                        # 取该日期最大值（多个概念）
                        if end not in nodes or val > nodes[end]:
                            nodes[end] = val
    return nodes


def interp(nodes, date):
    """从 (date,val) 节点插值"""
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


def main():
    print("获取价格...")
    mstr = fetch_yahoo("MSTR")
    btc = fetch_yahoo("BTC-USD")
    print(f"  MSTR {len(mstr)} 天, BTC {len(btc)} 天")

    print("获取股本...")
    share_nodes = fetch_shares()
    print(f"  股本节点 {len(share_nodes)} 个")
    # 转成排序列表
    sdates = sorted(share_nodes)
    share_series = [(d, share_nodes[d]) for d in sdates]

    # 用共同交易日
    dates = sorted(set(mstr) & set(btc))
    print(f"  共同交易日: {len(dates)}")

    # 重建每日 mNAV（gross basic 口径）
    # mNAV = 股价 / (每股BTC × BTC价格) = 市值 / (持仓×BTC价)
    # 用 股本插值（持仓/股本=每股BTC）
    records = []
    for d in dates:
        hold = interp(HOLD_NODES, d)
        shares = interp(share_series, d)
        bps = hold / shares
        nav = bps * btc[d]
        mnav = mstr[d] / nav
        records.append({
            "date": d, "mstr": mstr[d], "btc": btc[d],
            "hold": hold, "shares": shares, "bps": bps, "mnav": mnav,
        })

    print(f"  重建 mNAV {len(records)} 天")
    print(f"  mNAV 区间: {min(r['mnav'] for r in records):.2f} ~ {max(r['mnav'] for r in records):.2f}")

    # ========== 回测 ==========
    # 策略：以 mNAV 为信号，在 MSTR 和 BTC（视为"现金/无溢价"）之间切换
    # 规则（主策略）：mNAV <= BUY_THRESHOLD 持有 MSTR；mNAV >= SELL_THRESHOLD 切换为 BTC
    strategies = [
        ("持有MSTR(基准)", None, None),
        ("持有BTC(基准)", None, None),
        ("mNAV 0.9买/1.3卖", 0.9, 1.3),
        ("mNAV 1.0买/1.5卖", 1.0, 1.5),
        ("mNAV 1.0买/1.22卖", 1.0, 1.22),
    ]

    results = {}
    all_trades = {}
    for name, buy, sell in strategies:
        nav_curve = []
        trades = []
        position = None  # 'MSTR' or 'BTC'
        equity = 1.0
        last_mstr_shares = 0
        last_btc = 0
        prev_hold = None

        for i, r in enumerate(records):
            d = r["date"]
            if buy is None:
                # 基准
                if "MSTR" in name:
                    if i == 0:
                        equity = 1.0
                    else:
                        equity *= r["mstr"] / records[i - 1]["mstr"]
                else:
                    if i == 0:
                        equity = 1.0
                    else:
                        equity *= r["btc"] / records[i - 1]["btc"]
            else:
                # 目标仓位
                target = None
                if r["mnav"] <= buy:
                    target = "MSTR"
                elif r["mnav"] >= sell:
                    target = "BTC"
                # 切换（在当日收盘执行，次日生效近似，这里用收盘）
                if target and target != position:
                    if position == "MSTR":
                        # 卖出MSTR，买入BTC
                        trades.append({
                            "date": d, "action": "卖出MSTR→BTC",
                            "mnav": round(r["mnav"], 3),
                            "mstr": round(r["mstr"], 2),
                        })
                    elif position == "BTC":
                        trades.append({
                            "date": d, "action": "买入MSTR(卖出BTC)",
                            "mnav": round(r["mnav"], 3),
                            "mstr": round(r["mstr"], 2),
                        })
                    else:
                        if target == "MSTR":
                            trades.append({
                                "date": d, "action": "初始买入MSTR",
                                "mnav": round(r["mnav"], 3),
                                "mstr": round(r["mstr"], 2),
                            })
                    position = target

                # 计算净值
                if i == 0:
                    equity = 1.0
                    if position is None:
                        position = "BTC"  # 初始默认持BTC等待
                else:
                    if position == "MSTR":
                        equity *= r["mstr"] / records[i - 1]["mstr"]
                    else:
                        equity *= r["btc"] / records[i - 1]["btc"]

            nav_curve.append(equity)
        results[name] = nav_curve
        all_trades[name] = trades

    # ========== 绩效指标 ==========
    def metrics(curve, name):
        curve = np.array(curve)
        total_ret = curve[-1] - 1
        days = len(curve)
        years = days / 252
        ann = curve[-1] ** (1 / years) - 1
        peak = np.maximum.accumulate(curve)
        dd = (curve - peak) / peak
        max_dd = dd.min()
        # 日收益
        rets = np.diff(curve) / curve[:-1]
        # 胜率（按日）
        win = (rets > 0).sum()
        loss = (rets < 0).sum()
        win_rate = win / (win + loss) if (win + loss) else 0
        # 盈亏比
        gains = rets[rets > 0].sum()
        losses = -rets[rets < 0].sum()
        pf = gains / losses if losses else float("inf")
        return {
            "name": name,
            "total_ret": total_ret,
            "ann": ann,
            "max_dd": max_dd,
            "win_rate": win_rate,
            "pf": pf,
            "trades": len(all_trades[name]),
        }

    report = [metrics(results[n], n) for n, _, _ in strategies]

    print("\n" + "=" * 90)
    print(f"{'策略':<22}{'总收益':>12}{'年化':>10}{'最大回撤':>10}{'日胜率':>9}{'盈亏比':>8}{'交易次数':>8}")
    print("-" * 90)
    for m in report:
        print(f"{m['name']:<22}{m['total_ret']*100:>11.1f}%{m['ann']*100:>9.1f}%{m['max_dd']*100:>9.1f}%{m['win_rate']*100:>8.1f}%{m['pf']:>8.2f}{m['trades']:>8}")
    print("=" * 90)

    # ========== 输出文件 ==========
    # 1. 每日 mNAV 明细
    with open("mnav_daily.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "MSTR", "BTC", "Hold", "Shares", "BPS", "mNAV"])
        for r in records:
            w.writerow([r["date"], round(r["mstr"], 2), round(r["btc"], 1),
                        int(r["hold"]), int(r["shares"]), round(r["bps"], 6),
                        round(r["mnav"], 4)])

    # 2. 各策略净值曲线
    with open("nav_curves.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date"] + [s[0] for s in strategies])
        for i, d in enumerate(dates):
            w.writerow([d] + [round(results[s[0]][i], 5) for s in strategies])

    # 3. 交易明细（主策略）
    main_name = "mNAV 0.9买/1.3卖"
    with open("trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Action", "mNAV", "MSTR"])
        for t in all_trades[main_name]:
            w.writerow([t["date"], t["action"], t["mnav"], t["mstr"]])

    # 4. 绩效 JSON
    json.dump(report, open("performance.json", "w"), ensure_ascii=False, indent=2)

    print("\n已输出: mnav_daily.csv, nav_curves.csv, trades.csv, performance.json")
    return report, dates, results, records


if __name__ == "__main__":
    main()
