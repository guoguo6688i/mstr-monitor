# MSTR 折价率/溢价率监控

监控 **Strategy (MSTR，原 MicroStrategy)** 股价相对于每股 BTC 净资产 (NAV) 的溢价率/折价率（mNAV），每日自动运行并发送邮件。

## 工作原理

```
mNAV = 股价 / (每股 BTC 持仓 × BTC 价格)
```

- mNAV > 1：溢价交易（为公司的"非 BTC 部分"额外付费）
- mNAV < 1：折价交易（股价低于其持有的 BTC 价值）

## 计算口径

脚本提供两种口径：

| 口径 | 每股 BTC | 说明 |
|------|----------|------|
| 完全稀释 (FD) | 848,250 / 422,000,000 = 0.002010 | 计入所有可能增发的股份，更保守 |
| 流通股 (basic) | 848,250 / 384,230,000 = 0.002208 | 按当前实际流通股 |

> 注：本脚本为 gross 口径，未扣除公司债务和优先股（约 205 亿美元）。
> 扣除高级债权后的净资产口径（CEBE）见 cebetracker.io。

## 运行时间

- **北京时间每天 22:30**（美股开盘后约 1 小时），cron `30 14 * * *`（UTC）
- 备用任务 22:50，防止漏跑（状态去重，每天只发一次）
- 支持手动触发

## 邮件内容

- MSTR 股价、BTC 价格
- mNAV 倍数及溢价率/折价率（FD + basic 两种口径）
- 每股 BTC 数量、每股 NAV
- 52 周 mNAV 区间及当前分位
- 状态变化提醒

## 重要：公司数据需要定期更新

MSTR 会持续买入 BTC 并增发股票，脚本顶部的数据需要定期更新：

```python
BTC_HOLD = 848_250          # BTC 总持仓
SHARES_BASIC = 384_230_000  # 流通股
SHARES_FD = 422_000_000     # 完全稀释股
```

数据来源：
- 官方 Investor Briefing：https://www.strategy.com/
- mnav.com：https://www.mnav.com/mnav/strategy
- cebetracker.io

## 数据源（自动回退）

- 股价：Yahoo Finance → 腾讯财经 → 东方财富
- BTC：Yahoo Finance (BTC-USD) → OKX → CoinGecko

## 配置

GitHub Secrets：
- `EMAIL_PASSWORD`：QQ 邮箱 SMTP 授权码

## 免责声明

仅供研究参考，不构成投资建议。
