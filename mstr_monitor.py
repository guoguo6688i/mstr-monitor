# -*- coding: utf-8 -*-
"""
MSTR (Strategy / MicroStrategy) 折价率/溢价率监控
每日运行，自动从 mnav.com 公开 API 获取最新 BTC 持仓和股本数据
（自动反映 MSTR 加仓/减仓/增发，无需手动更新）
"""
import os
import json
import smtplib
from datetime import datetime, timezone as _tz
from email.mime.text import MIMEText
from email.header import Header

import requests

# ============================================================
# 备用硬编码数据（仅当实时 API 全部失败时使用）
# ============================================================
FALLBACK = {
    "btcHeld": 848_250,
    "sharesFD": 422_000_000,
    "updated": "2026-10-11",
}

# 52周 mNAV 历史区间（gross 口径，作为高低参考）
MNAV_52W_LOW = 0.95
MNAV_52W_HIGH = 1.43

MNAV_API = "https://www.mnav.com/api/companies/summary"

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


def get_mnav_data():
    """
    从 mnav.com 公开 API 获取 MSTR 实时数据
    返回字典，包含持仓、股本、价格、mNAV等
    """
    try:
        r = requests.get(
            MNAV_API,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        companies = r.json().get("companies", [])
        for c in companies:
            if c.get("id") == "strategy":
                return c, True
    except Exception as e:
        print(f"  [mnav API] 获取失败: {e}")
    return None, False


def get_btc_price_yahoo():
    """回退：从 Yahoo 获取 BTC 价格"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
        )
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


# 阈值区间（基于 gross mNAV，直观口径）
ZONES = [
    ("extreme_premium", "极度高溢价（风险高）", 1.40),
    ("high_premium", "高溢价（偏贵）", 1.20),
    ("premium", "溢价", 1.10),
    ("neutral", "接近净资产（中性）", 0.90),
    ("discount", "折价（相对便宜）", 0.80),
    ("deep_discount", "深度折价（非常便宜）", 0.0),
]
ALERT_ZONES = {"deep_discount", "discount", "high_premium", "extreme_premium"}


def get_zone(mnav):
    for zone_id, name, threshold in ZONES:
        if mnav >= threshold:
            return zone_id, name
    return "deep_discount", "深度折价（非常便宜）"


def get_zone_advice(zone_id):
    advice = {
        "extreme_premium": "股价大幅高于BTC净资产，追高风险极大，持有者可减仓，空仓者勿追。",
        "high_premium": "溢价较高，性价比偏低，持有者可逢高减仓，空仓者等待溢价回落。",
        "premium": "存在一定溢价，可继续持有，不建议追高。",
        "neutral": "股价接近BTC净资产，估值中性，可持有或观望。",
        "discount": "股价低于BTC净资产，出现折价，可考虑分批建仓。",
        "deep_discount": "深度折价，股价显著低于所持BTC价值，历史少见，可重点关注/加仓（需排除基本面风险）。",
    }
    return advice.get(zone_id, "")


def main():
    print("=" * 60)
    print("  MSTR 折价率/溢价率监控（自动同步持仓）")
    print("=" * 60)
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  邮件推送: {'已启用' if EMAIL['enabled'] else '未启用'} → {EMAIL['to']}")

    # 获取实时数据
    print("\n  正在获取实时数据...")
    data, live = get_mnav_data()

    if live and data:
        mstr_price = float(data["sharePrice"])
        btc_held = float(data["btcHeld"])
        btc_nav = float(data["bitcoinNav"])
        market_cap = float(data["marketCap"])
        market_cap_fd = float(data["marketCapFullyDiluted"])
        btc_per_share = float(data["btcPerShare"])
        mnav_theirs = float(data["mnav"])
        btc_price = btc_nav / btc_held
        total_cash = float(data.get("totalCash", 0))
        total_debt = float(data.get("totalDebt", 0))
        btc_change_30d = float(data.get("btcChange30D", 0))
        bps_change_30d = float(data.get("btcPerShareChange30D", 0))
        src = "mnav.com 实时 API"
        # 推算完全稀释股本
        shares_fd = btc_held / btc_per_share if btc_per_share else FALLBACK["sharesFD"]
        data_date = "实时"
        print("  ✅ 已获取实时持仓数据（自动反映加仓/减仓）")
    else:
        # 回退
        btc_price = get_btc_price_yahoo()
        if not btc_price:
            print("  所有数据源失败")
            if EMAIL["enabled"]:
                send_email("⚠️ MSTR监控：数据获取失败",
                           f"运行时间: {datetime.now()}\n")
            return
        btc_held = FALLBACK["btcHeld"]
        shares_fd = FALLBACK["sharesFD"]
        btc_per_share = btc_held / shares_fd
        btc_nav = btc_held * btc_price
        # 股价无法获取，无法计算 mNAV
        print("  ⚠️ 无法获取 MSTR 股价和持仓，仅获取到 BTC 价格")
        return

    # ===== 计算两种口径 =====
    # 1. Gross mNAV（市值 / 全部BTC净资产，最直观，未扣债务）
    gross_mnav = market_cap / btc_nav
    gross_mnav_fd = market_cap_fd / btc_nav
    gross_premium = (gross_mnav - 1) * 100

    # 2. mnav.com 口径（资本结构调整后，反映其 mNAV 指标）
    # 区间
    zone_id, zone_name = get_zone(gross_mnav)
    advice = get_zone_advice(zone_id)

    # 52周分位
    pct_52w = (gross_mnav - MNAV_52W_LOW) / (MNAV_52W_HIGH - MNAV_52W_LOW) * 100
    pct_52w = max(0, min(100, pct_52w))

    # 每股 NAV
    nav_per_share = btc_per_share * btc_price

    print(f"  数据源: {src}")
    print(f"  MSTR 股价: ${mstr_price:,.2f}")
    print(f"  BTC 价格: ${btc_price:,.0f}")
    print(f"  BTC 持仓: {btc_held:,.0f} 枚")
    print("\n" + "-" * 60)
    print(f"  【当前状态】{zone_name}")
    print(f"  Gross mNAV: {gross_mnav:.3f}× ({gross_premium:+.1f}%)")
    print(f"  mnav.com口径: {mnav_theirs:.3f}×")
    print(f"  52周分位: {pct_52w:.0f}%")
    print("-" * 60)

    # 读取历史状态
    prev = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                prev = json.load(f)
        except Exception:
            pass

    # ===== 构建邮件 =====
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_utc = datetime.now(_tz.utc).strftime("%Y-%m-%d")

    body = f"""MSTR (Strategy) 折价率报告
{'=' * 50}
运行时间: {now_str}（北京时间）
数据源: {src}（持仓自动同步）

【当前状态】{zone_name}
操作参考: {advice}

价格数据:
  MSTR 股价:   ${mstr_price:,.2f}
  BTC 价格:    ${btc_price:,.0f}

mNAV 两种口径:
  Gross（市值/全部BTC价值）: {gross_mnav:.3f}×  ({gross_premium:+.1f}%)
  mnav.com（资本结构调整）:  {mnav_theirs:.3f}×
  完全稀释 Gross:            {gross_mnav_fd:.3f}×

每股数据:
  每股 BTC:     {btc_per_share:.6f}（{btc_per_share*1e8:.0f} sats）
  每股 NAV:     ${nav_per_share:,.2f}

公司持仓（自动同步，无需手动更新）:
  BTC 持仓:     {btc_held:,.0f} 枚
  BTC 净资产:   ${btc_nav/1e9:.1f}B
  总市值:       ${market_cap/1e9:.1f}B
  现金:         ${total_cash/1e9:.2f}B
  债务:         ${total_debt/1e9:.2f}B

最近30天变化:
  持仓变化:     {btc_change_30d:+,.0f} BTC
  每股BTC变化:  {bps_change_30d:+.2f}%

参考区间:
  52周 Gross:   {MNAV_52W_LOW}× ~ {MNAV_52W_HIGH}×
  当前分位:     {pct_52w:.0f}%

预警阈值（Gross口径）:
  深度折价: mNAV ≤ 0.80（折价≥20%）
  折价:     mNAV ≤ 0.90
  高溢价:   mNAV ≥ 1.20
  极度高溢价: mNAV ≥ 1.40
"""

    if prev and prev.get("gross_mnav"):
        old = prev["gross_mnav"]
        old_zone = prev.get("zone_name", "")
        body += f"\n状态变化:\n  上次 Gross: {old:.3f}× ({old_zone})\n  变化: {gross_mnav-old:+.3f}×\n"

    body += "\n" + "=" * 50
    body += "\n口径说明：\n"
    body += "Gross mNAV = 总市值 / 全部BTC净资产（未扣债务，最直观）\n"
    body += "mnav.com口径扣除了部分资本结构影响，数值通常更高\n"
    body += "持仓和股本由 mnav.com API 自动同步，加仓/减仓自动反映\n"
    body += "仅供研究参考，不构成投资建议\n"

    # ===== 发送逻辑 =====
    now_utc = datetime.now(_tz.utc)
    is_daily_slot = now_utc.hour == 14
    is_intraday = now_utc.hour in (16, 17, 18, 19, 20, 21)

    sent_date = prev.get("last_daily_date") if prev else None
    should_send = False
    is_alert = False
    subject = ""

    if is_daily_slot:
        if sent_date != today_utc:
            should_send = True
            subject = f"📊 MSTR {zone_name} | Gross {gross_mnav:.2f}× ({gross_premium:+.1f}%) | ${mstr_price:,.0f}"
        else:
            print("\n  今日例行报告已发送，跳过")
    elif is_intraday:
        old_zone = prev.get("zone_id") if prev else None
        if zone_id != old_zone and zone_id in ALERT_ZONES:
            should_send = True
            is_alert = True
            old_name = prev.get("zone_name", "无记录") if prev else "无记录"
            subject = f"🔔 MSTR预警: {old_name} → {zone_name} | Gross {gross_mnav:.2f}× ({gross_premium:+.1f}%)"
        else:
            print("\n  盘中检测：未触发关键阈值，不发邮件")
    else:
        should_send = True
        subject = f"📊 MSTR {zone_name} | Gross {gross_mnav:.2f}× ({gross_premium:+.1f}%) | ${mstr_price:,.0f}"

    if should_send and EMAIL["enabled"]:
        print("\n  正在发送邮件...")
        ok = send_email(subject, body)
        if is_daily_slot and ok:
            sent_date = today_utc

    # 保存状态
    state = {
        "gross_mnav": gross_mnav,
        "theirs_mnav": mnav_theirs,
        "mstr_price": mstr_price,
        "btc_price": btc_price,
        "btc_held": btc_held,
        "zone_id": zone_id,
        "zone_name": zone_name,
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
