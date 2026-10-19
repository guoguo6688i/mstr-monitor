# -*- coding: utf-8 -*-
"""
MSTR (Strategy / MicroStrategy) 折价率/溢价率监控
每日运行，计算 MSTR 股价相对于每股 BTC 净资产 (NAV) 的溢价/折价
"""
import os
import json
import smtplib
from datetime import datetime, timezone as _tz
from email.mime.text import MIMEText
from email.header import Header

import requests

# ============================================================
# 公司数据（需要定期更新！每次 MSTR 发布新持仓/增发后更新）
# ============================================================
# 数据来源：MSTR Investor Briefing / mnav.com
# 最近更新：2026-10-19
# 官方最新简报：https://www.strategy.com/ 或 https://assets.contentstack.io/
BTC_HOLD = 848_250            # BTC 总持仓（截至 2026-10-11）
SHARES_BASIC = 384_230_000    # 流通股（basic，最近）
SHARES_FD = 422_000_000       # 完全稀释股（fully diluted，约4.22亿）
AVG_COST = 75_800             # 平均持仓成本（美元/BTC，约）

# 52周 mNAV 历史区间（来自 mnav.com，作为溢价高低参考）
MNAV_52W_LOW = 0.95
MNAV_52W_HIGH = 1.43

# ============================================================
# 邮件配置
# ============================================================
EMAIL = {
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "user": "5660597@qq.com",
    "password": os.environ.get("EMAIL_PASSWORD", ""),
    "to": "5660597@qq.com",
    "enabled": os.environ.get("EMAIL_ENABLED", "true").lower() == "true",
}

STATE_FILE = ".mstr_last_state.json"
OKX_API = "https://www.oyigwcn.biz"


def send_email(subject, body):
    """发送邮件"""
    if not EMAIL["enabled"] or not EMAIL["password"]:
        print("  [邮件] 未启用或缺少密码")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = EMAIL["user"]
        msg["To"] = EMAIL["to"]
        msg["Subject"] = Header(subject, "utf-8")

        server = smtplib.SMTP_SSL(EMAIL["smtp_host"], EMAIL["smtp_port"], timeout=30)
        server.login(EMAIL["user"], EMAIL["password"])
        server.sendmail(EMAIL["user"], [EMAIL["to"]], msg.as_string())
        server.quit()
        print(f"  [邮件] 已发送至 {EMAIL['to']}")
        return True
    except Exception as e:
        print(f"  [邮件] 发送失败: {e}")
        return False


def get_btc_price():
    """获取 BTC 价格，多数据源回退"""
    # 1. Yahoo Finance BTC-USD（GitHub Actions 美国环境）
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
        )
        d = r.json()
        meta = d["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except Exception:
        pass

    # 2. OKX 镜像/主站
    bases = [OKX_API, "https://www.okx.com", "https://aws.okx.com"]
    for base in bases:
        try:
            r = requests.get(
                f"{base}/api/v5/market/ticker?instId=BTC-USDT",
                timeout=15,
            )
            d = r.json()
            if d.get("code") == "0":
                return float(d["data"][0]["last"])
        except Exception:
            continue

    # 3. CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            timeout=15,
        )
        d = r.json()
        return float(d["bitcoin"]["usd"])
    except Exception:
        pass

    return None


def get_mstr_price():
    """
    获取 MSTR 股价，多数据源回退
    返回 (price, source, timestamp_str)
    """
    # 1. Yahoo Finance（GitHub Actions 美国环境优先）
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/MSTR",
            params={"interval": "1d", "range": "2d"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
        )
        d = r.json()
        meta = d["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        ts = datetime.fromtimestamp(meta["regularMarketTime"]).strftime("%Y-%m-%d %H:%M")
        return price, "Yahoo Finance", ts
    except Exception:
        pass

    # 2. 腾讯财经（国内/沙箱可用）
    try:
        r = requests.get(
            "https://qt.gtimg.cn/q=usMSTR",
            timeout=15,
        )
        text = r.text
        fields = text.split("~")
        price = float(fields[3])
        ts = fields[30] if len(fields) > 30 else ""
        return price, "腾讯财经", ts
    except Exception:
        pass

    # 3. 东方财富
    try:
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": "105.MSTR", "fields": "f43,f86"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        d = r.json()
        price = d["data"]["f43"] / 1000.0
        ts = datetime.fromtimestamp(d["data"]["f86"]).strftime("%Y-%m-%d %H:%M")
        return price, "东方财富", ts
    except Exception:
        pass

    return None, None, None


def calc_metrics(mstr_price, btc_price):
    """计算折价率/溢价率指标"""
    # 每股 BTC 数量
    btc_per_share_basic = BTC_HOLD / SHARES_BASIC
    btc_per_share_fd = BTC_HOLD / SHARES_FD

    # 每股 NAV（美元）
    nav_basic = btc_per_share_basic * btc_price
    nav_fd = btc_per_share_fd * btc_price

    # mNAV 倍数（市值 / BTC净资产）
    mnav_basic = mstr_price / nav_basic
    mnav_fd = mstr_price / nav_fd

    # 溢价率（正=溢价，负=折价）
    premium_basic = (mnav_basic - 1) * 100
    premium_fd = (mnav_fd - 1) * 100

    # 溢价率在52周区间的分位
    pct_52w = (mnav_fd - MNAV_52W_LOW) / (MNAV_52W_HIGH - MNAV_52W_LOW) * 100
    pct_52w = max(0, min(100, pct_52w))

    return {
        "btc_per_share_basic": btc_per_share_basic,
        "btc_per_share_fd": btc_per_share_fd,
        "nav_basic": nav_basic,
        "nav_fd": nav_fd,
        "mnav_basic": mnav_basic,
        "mnav_fd": mnav_fd,
        "premium_basic": premium_basic,
        "premium_fd": premium_fd,
        "pct_52w": pct_52w,
    }


def get_premium_label(mnav):
    """根据 mNAV 给出状态标签（兼容旧函数）"""
    zone_id, name = get_zone(mnav)
    return name


# 阈值区间定义（基于 mNAV 倍数）
# 顺序：从高到低
ZONES = [
    ("extreme_premium", "极度高溢价（风险高）", 1.40),
    ("high_premium", "高溢价（偏贵）", 1.20),
    ("premium", "溢价", 1.10),
    ("neutral", "接近净资产（中性）", 0.90),
    ("discount", "折价（相对便宜）", 0.80),
    ("deep_discount", "深度折价（非常便宜）", 0.0),
]

# 需要预警的关键区间（进入这些区间时立即发邮件）
ALERT_ZONES = {"deep_discount", "discount", "high_premium", "extreme_premium"}


def get_zone(mnav):
    """获取 mNAV 所在区间，返回 (zone_id, 名称)"""
    for zone_id, name, threshold in ZONES:
        if mnav >= threshold:
            return zone_id, name
    return "deep_discount", "深度折价（非常便宜）"


def get_zone_advice(zone_id):
    """根据区间给出操作建议"""
    advice = {
        "extreme_premium": "股价大幅高于 BTC 净资产，追高风险极大，持有者可考虑减仓，空仓者勿追。",
        "high_premium": "溢价较高，性价比偏低，持有者可逢高减仓，空仓者等待溢价回落。",
        "premium": "存在一定溢价，可继续持有，不建议追高买入。",
        "neutral": "股价接近 BTC 净资产，估值中性，可持有或观望。",
        "discount": "股价低于 BTC 净资产，出现折价，可考虑分批建仓。",
        "deep_discount": "深度折价，股价显著低于所持 BTC 价值，历史上少见，可重点关注/加仓（需排除基本面风险）。",
    }
    return advice.get(zone_id, "")


def main():
    print("=" * 60)
    print("  MSTR 折价率/溢价率监控")
    print("=" * 60)
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  邮件推送: {'已启用' if EMAIL['enabled'] else '未启用'} → {EMAIL['to']}")

    # 获取数据
    print("\n  正在获取价格...")
    btc_price = get_btc_price()
    mstr_price, src, ts = get_mstr_price()

    if not btc_price or not mstr_price:
        print(f"  数据获取失败: BTC={btc_price}, MSTR={mstr_price}")
        # 数据失败也发邮件提醒
        if EMAIL["enabled"]:
            send_email("⚠️ MSTR监控：数据获取失败",
                       f"运行时间: {datetime.now()}\nBTC: {btc_price}\nMSTR: {mstr_price}\n")
        return

    print(f"  数据源: {src}")
    print(f"  MSTR 股价: ${mstr_price:,.2f}  ({ts})")
    print(f"  BTC 价格: ${btc_price:,.0f}")

    # 计算指标
    m = calc_metrics(mstr_price, btc_price)

    label = get_premium_label(m["mnav_fd"])

    # 显示
    print("\n" + "-" * 60)
    print(f"  【当前状态】{label}")
    print(f"  mNAV (完全稀释): {m['mnav_fd']:.3f}×  溢价率 {m['premium_fd']:+.1f}%")
    print(f"  mNAV (流通股):   {m['mnav_basic']:.3f}×  溢价率 {m['premium_basic']:+.1f}%")
    print(f"  52周区间分位: {m['pct_52w']:.0f}%（{MNAV_52W_LOW}× ~ {MNAV_52W_HIGH}×）")
    print("-" * 60)
    print(f"  每股 BTC: {m['btc_per_share_fd']:.6f} (FD) / {m['btc_per_share_basic']:.6f} (basic)")
    print(f"  每股 NAV: ${m['nav_fd']:,.2f} (FD) / ${m['nav_basic']:,.2f} (basic)")
    print(f"  BTC持仓: {BTC_HOLD:,} 枚 | 完全稀释股本: {SHARES_FD/1e6:.0f}M")

    # 读取历史状态
    prev = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                prev = json.load(f)
        except Exception:
            pass

    # 当前区间
    zone_id, zone_name = get_zone(m["mnav_fd"])
    advice = get_zone_advice(zone_id)

    # 构建邮件
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_utc = datetime.now(_tz.utc).strftime("%Y-%m-%d")

    body = f"""MSTR (Strategy) 折价率报告
{'=' * 50}
运行时间: {now_str}（北京时间）
价格时间: {ts}
数据源: {src}

【当前状态】{zone_name}
操作参考: {advice}

价格数据:
  MSTR 股价:   ${mstr_price:,.2f}
  BTC 价格:    ${btc_price:,.0f}

溢价/折价（mNAV = 市值 / BTC净资产）:
  完全稀释口径: {m['mnav_fd']:.3f}×  ({m['premium_fd']:+.1f}%)
  流通股口径:   {m['mnav_basic']:.3f}×  ({m['premium_basic']:+.1f}%)

每股净资产:
  每股 BTC:     {m['btc_per_share_fd']:.6f} (FD) / {m['btc_per_share_basic']:.6f} (basic)
  每股 NAV:     ${m['nav_fd']:,.2f} (FD) / ${m['nav_basic']:,.2f} (basic)

参考区间:
  52周 mNAV:    {MNAV_52W_LOW}× ~ {MNAV_52W_HIGH}×
  当前分位:     {m['pct_52w']:.0f}%

预警阈值（完全稀释口径）:
  深度折价: mNAV ≤ 0.80（折价≥20%）
  折价:     mNAV ≤ 0.90（折价≥10%）
  高溢价:   mNAV ≥ 1.20（溢价≥20%）
  极度高溢价: mNAV ≥ 1.40（溢价≥40%）

公司数据（截至2026-10-11）:
  BTC 持仓:     {BTC_HOLD:,} 枚
  流通股:       {SHARES_BASIC/1e6:.1f}M
  完全稀释股:   {SHARES_FD/1e6:.0f}M
  平均成本:     ${AVG_COST:,.0f}/BTC
"""

    # 状态变化
    if prev and prev.get("mnav_fd"):
        old_mnav = prev["mnav_fd"]
        old_zone = prev.get("zone_id", "")
        old_zone_name = prev.get("label", "")
        change = m["mnav_fd"] - old_mnav
        body += f"\n状态变化:\n  上次 mNAV: {old_mnav:.3f}× ({old_zone_name})\n  变化: {change:+.3f}×\n"

    body += "\n" + "=" * 50
    body += "\n口径说明：mNAV = 股价 / (每股BTC持仓 × BTC价格)\n"
    body += "完全稀释口径更保守（计入可能增发的股份）\n"
    body += "未扣除公司债务/优先股，实际净资产口径见 cebetracker.io\n"
    body += "仅供研究参考，不构成投资建议\n"

    # ========== 发送逻辑 ==========
    now_utc = datetime.now(_tz.utc)
    is_daily_slot = now_utc.hour == 14          # 北京22:30例行报告
    is_intraday = now_utc.hour in (16, 17, 18, 19, 20, 21)

    sent_date = prev.get("last_daily_date") if prev else None
    should_send = False
    subject = ""
    is_alert = False

    if is_daily_slot:
        # 例行报告：每天必发（去重）
        if sent_date != today_utc:
            should_send = True
            subject = f"📊 MSTR {zone_name} | mNAV {m['mnav_fd']:.2f}× ({m['premium_fd']:+.1f}%) | 股价${mstr_price:,.0f}"
        else:
            print("\n  今天例行报告已发送，跳过（备用任务去重）")
    elif is_intraday:
        # 盘中检测：区间跨越关键阈值才预警
        old_zone = prev.get("zone_id") if prev else None
        if zone_id != old_zone and zone_id in ALERT_ZONES:
            should_send = True
            is_alert = True
            old_name = prev.get("label", "无记录") if prev else "无记录"
            subject = f"🔔 MSTR预警: {old_name} → {zone_name} | mNAV {m['mnav_fd']:.2f}× ({m['premium_fd']:+.1f}%)"
        else:
            print("\n  盘中检测：未触发关键阈值，不发邮件")
    else:
        # 手动触发（非定时时段）：总是发送
        should_send = True
        subject = f"📊 MSTR {zone_name} | mNAV {m['mnav_fd']:.2f}× ({m['premium_fd']:+.1f}%) | 股价${mstr_price:,.0f}"

    if should_send and EMAIL["enabled"]:
        print("\n  正在发送邮件...")
        ok = send_email(subject, body)
        if is_daily_slot and ok:
            sent_date = today_utc

    # 保存状态
    state = {
        "mnav_fd": m["mnav_fd"],
        "mnav_basic": m["mnav_basic"],
        "mstr_price": mstr_price,
        "btc_price": btc_price,
        "label": zone_name,
        "zone_id": zone_id,
        "time": datetime.now().isoformat(),
        "last_daily_date": sent_date,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    print("\n" + "=" * 60)
    print("  完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
