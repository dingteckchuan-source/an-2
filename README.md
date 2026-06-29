# ASX 纯动量「领涨抗跌」强势股扫描器

基于一条核心信条的 ASX 选股工具：

> *"When the market goes up, there's certain stocks and sectors go up a lot more,
> and when the market goes down those things don't really fall."*

脚本扫描完整 **S&P/ASX 200** 成分股，仅用纯技术面相对强度筛选出「大盘涨时涨得更多、大盘跌时几乎不跌」的强势股，并生成精简 Markdown 报告保存到 Windows 桌面、自动打开。

---

## 筛选逻辑

以 **^AXJO（S&P/ASX 200）** 为基准，对每只个股要求**同时满足**以下三个条件：

| 条件 | 名称 | 判定 |
|------|------|------|
| **A** | 领涨 | 个股在 XJO **上涨日**的平均涨幅 **>** XJO 平均涨幅 |
| **B** | 抗跌 | 个股在 XJO **下跌日**的平均跌幅 **<** XJO 平均跌幅的 **50%**（极其抗跌，甚至逆势上涨）|
| **C** | 趋势护栏 | 当前价格站上 **50 日均线** |

分析区间约 3 个月（~67 个交易日）。行业板块（11 个 ASX 行业指数）也用同一套「领涨抗跌」标准做相对强度参考。

> 抗跌阈值由常量 `RESIST_FACTOR = 0.5` 控制（跌幅为负数，系数越小越严格）。

---

## 报告内容

生成的 `ASX_报告_YYYYMMDD.md` 包含：

1. **市场总览** — 基准 XJO 区间累计涨跌、上涨/下跌日均幅
2. **筛选框架** — 三条件说明
3. **行业板块相对强度** — 各板块领涨/抗跌表现
4. **⭐ 纯动量强势股** — 同时满足 A/B/C 三条件的标的（含超额涨幅、抗跌比率，并逐只点评）

---

## 运行

```bash
pip install yfinance pandas numpy
python asx_scanner.py
```

数据源为 [Yahoo Finance](https://finance.yahoo.com/)（`yfinance`）。跑完报告会保存到桌面（兼容 OneDrive 重定向）并自动打开。

### 定时运行（Windows）

`run_asx_scanner.bat` 供 Windows 计划任务调用，每次运行追加日志到 `asx_scanner_run.log`。当前由计划任务「ASX Weekly Scanner」每周一 08:00 触发。

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `asx_scanner.py` | 主扫描脚本 |
| `run_asx_scanner.bat` | 计划任务启动器（带日志） |

---

## 免责声明

本工具基于历史数据自动生成，**仅供参考，不构成投资建议**。过去的相对强度不代表未来收益，投资有风险，请独立判断。
