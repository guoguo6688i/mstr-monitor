# -*- coding: utf-8 -*-
"""
MSTR (Strategy / MicroStrategy) 折价率/溢价率监控
工作日盘中每2小时运行，自动从 mnav.com 公开 API 获取最新 BTC 持仓和股本数据
（自动反映 MSTR 加仓/减仓/增发，无需手动更新）

邮件按 Gross mNAV（与回测口径一致）直接标注当前处于：
  加仓区 / 持有区 / 减仓区
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

# 52周 mNAV 历史区间（Gross 口径，作为高低参考）
MNAV_52W_LOW = 0.572
MNAV_52W_HIGH = 1.318

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


# ============================================================
# 操作区间（Gross mNAV，与历史回测口径一致）
# 三大类：加仓区 / 持有区 / 减仓区
# ============================================================
# 每条: (该档阈值下限, 大类id, 大类标签, 细分标签, 操作建议)
ZONE_BANDS = [
    (4.0, "reduce", "减仓区", "清仓区",
     "极度高估，Gross mNAV 超 4.0x，建议清仓或不持有。"),
    (2.5, "reduce", "减仓区", "减仓区",
     "严重溢价（2.5~4.0x），建议大幅减仓。"),
    (1.5, "reduce", "减仓区", "逢高减仓区",
     "高溢价（1.5~2.5x），高于隐性成本线，持有 BTC 性价比更高，建议逢高减仓/换 BTC。"),
    (1.0, "hold", "持有区", "持有区",
     "Gross mNAV 1.0~1.5x，可继续持有；回落至 1.0 以下可加仓。"),
    (0.8, "accumulate", "加仓区", "分批加仓区",
     "折价（0.8~1.0x），可分批买入/持有。"),
    (0.0, "accumulate", "加仓区", "重点加仓区",
     "深度折价（<0.8x），历史少见，可重点加仓（需排除基本面风险）。"),
]


def get_action_zone(mnav):
    """返回 (大类id, 大类标签, 细分标签, 操作建议)"""
    for low, major_id, major_label, sub_label, advice in ZONE_BANDS:
        if mnav >= low:
            return major_id, major_label, sub_label, advice
    last = ZONE_BANDS[-1]
    return last[1], last[2], last[3], last[4]


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


def main():
    print("=" * 60)
    print("  MSTR 折价率/溢价率监控（自动同步持仓）")
    print("=" * 60)
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  邮件推送: {'已启用' if EMAIL['enabled'] else '未启用'} -> {EMAIL['to']}")

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
        print("  ⚠️ 无法获取 MSTR 股价和持仓，仅获取到 BTC 价格")
        return

    # ===== 计算两种口径 =====
    # 1. Gross mNAV（市值 / 全部BTC净资产，最直观，未扣债务）
    #    marketCap 对应 422M 完全稀释股本，与回测口径一致
    gross_mnav = market_cap / btc_nav
    gross_mnav_fd = market_cap_fd / btc_nav
    gross_premium = (gross_mnav - 1) * 100

    # 2. 区域判断（用 Gross mNAV，与回测一致）
    major_zone, major_label, sub_label, advice = get_action_zone(gross_mnav)
    zone_name = major_label + "·" + sub_label

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
    print(f"  【当前处于：{major_label}（{sub_label}）】")
    print(f"  Gross mNAV: {gross_mnav:.3f}x ({gross_premium:+.1f}%)")
    print(f"  mnav.com口径: {mnav_theirs:.3f}x")
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

    body = f"""MSTR (Strategy) 操作区间报告
{'=' * 50}
运行时间: {now_str}（北京时间）
数据源: {src}（持仓自动同步）

★ 当前处于：{major_label}（{sub_label}）
操作参考: {advice}

价格数据:
  MSTR 股价:   ${mstr_price:,.2f}
  BTC 价格:    ${btc_price:,.0f}

mNAV 三种口径（区域判断以 Gross 为准）:
  Gross（市值/全部BTC价值，回测口径）: {gross_mnav:.3f}x  ({gross_premium:+.1f}%)
  完全稀释 Gross:                     {gross_mnav_fd:.3f}x
  mnav.com（资本结构调整）:            {mnav_theirs:.3f}x

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
  52周 Gross:   {MNAV_52W_LOW}x ~ {MNAV_52W_HIGH}x
  当前分位:     {pct_52w:.0f}%

操作阈值（Gross mNAV，与回测一致）:
  加仓区:
    mNAV < 0.80    重点加仓（深度折价）
    0.80 ~ 1.00    分批加仓
  持有区:
    1.00 ~ 1.50    持有（回落至 1.0 以下加仓）
  减仓区:
    1.50 ~ 2.50    逢高减仓 / 换 BTC
    2.50 ~ 4.00    减仓
    mNAV > 4.00    清仓
"""

    if prev and prev.get("gross_mnav"):
        old = prev["gross_mnav"]
        old_zone = prev.get("zone_name", "")
        body += f"\n状态变化:\n  上次 Gross: {old:.3f}x ({old_zone})\n  变化: {gross_mnav-old:+.3f}x\n"

    body += "\n" + "=" * 50
    body += "\n口径说明：\n"
    body += "Gross mNAV = 总市值 / 全部BTC净资产（未扣债务，最直观，与回测一致）\n"
    body += "mnav.com口径扣除了部分资本结构影响，数值通常更高，仅作参考\n"
    body += "持仓和股本由 mnav.com API 自动同步，加仓/减仓自动反映\n"
    body += "仅供研究参考，不构成投资建议\n"

    # ===== 发送逻辑（对 GitHub schedule 延迟免疫）=====
    # 关键：用“事件类型（GITHUB_EVENT_NAME）”而非运行时钟点判断本次是什么任务，
    # 这样定时任务即便排队延迟数小时运行，也不会落入兜底分支误发。
    now_utc = datetime.now(_tz.utc)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    is_workday = now_utc.isoweekday() <= 5

    sent_date = prev.get("last_daily_date") if prev else None
    old_major = prev.get("major_zone") if prev else None

    # 例行报告窗口：工作日 UTC 14:30（北京 22:30）之后
    daily_window_open = is_workday and (
        now_utc.hour > 14 or (now_utc.hour == 14 and now_utc.minute >= 30)
    )
    need_daily = daily_window_open and sent_date != today_utc
    zone_changed = (old_major is not None) and (major_zone != old_major)

    should_send = False
    is_alert = False
    is_daily = False
    subject = ""
    mark_only = os.environ.get("MARK_ONLY", "false").lower() == "true"

    if mark_only:
        # 仅标记今日例行已发，不发邮件（用于已手动补发后，防止延迟的 schedule 重复）
        if daily_window_open:
            sent_date = today_utc
            print("\n  标记：今日例行报告已标记为已发送（不发邮件）")
        else:
            print("\n  标记：当前不在每日窗口内，不修改")
    elif event_name == "schedule":
        if need_daily:
            # 每日例行报告（窗口内任何一次运行发现今天没发，都会补发）
            should_send = True
            is_daily = True
            subject = f"📊 MSTR【{major_label}·{sub_label}】Gross {gross_mnav:.2f}x ({gross_premium:+.1f}%) | ${mstr_price:,.0f}"
            print("\n  定时：发送每日例行报告（含延迟补发）")
        elif zone_changed:
            # 区域切换即时预警
            should_send = True
            is_alert = True
            old_major_label = prev.get("major_label", "无记录") if prev else "无记录"
            subject = f"🔔 MSTR区域切换: {old_major_label} → {major_label} | Gross {gross_mnav:.2f}x ({gross_premium:+.1f}%)"
            print(f"\n  定时：检测到区域切换（{old_major_label} → {major_label}）")
        else:
            print("\n  定时：今日例行报告已发且区域未变化，不发邮件")
    else:
        # 手动触发（workflow_dispatch）或本地运行：发一封当前状态
        should_send = True
        subject = f"📊 MSTR【{major_label}·{sub_label}】Gross {gross_mnav:.2f}x ({gross_premium:+.1f}%) | ${mstr_price:,.0f}"
        print(f"\n  手动触发/本地（{event_name or 'local'}）：发送当前状态")

    if should_send and EMAIL["enabled"]:
        print("  正在发送邮件...")
        ok = send_email(subject, body)
        if is_daily and ok:
            sent_date = today_utc
        # 窗口内手动补发也视为今日例行已发，避免延迟的 schedule 再发一封
        if event_name == "workflow_dispatch" and ok and daily_window_open:
            sent_date = today_utc

    # 保存状态
    state = {
        "gross_mnav": gross_mnav,
        "theirs_mnav": mnav_theirs,
        "mstr_price": mstr_price,
        "btc_price": btc_price,
        "btc_held": btc_held,
        "major_zone": major_zone,
        "major_label": major_label,
        "sub_label": sub_label,
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
