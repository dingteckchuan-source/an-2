# -*- coding: utf-8 -*-
"""
ASX 纯动量「领涨抗跌」强势股扫描器 (本地版)
===================================================
核心信条:
  "When the market goes up, there's certain stocks and sectors go up a lot more,
   and when the market goes down those things don't really fall."

逻辑:
  1. 行业板块筛选 —— 11 个 ASX 行业指数的「领涨抗跌」相对强度（呼应「sectors」）
  2. 个股纯技术筛选 —— ASX200 成分股，同时满足以下三个条件即为「纯动量强势股」:
       · 条件 A（领涨）: 个股在 XJO 上涨日的平均涨幅 > XJO 平均涨幅
       · 条件 B（抗跌）: 个股在 XJO 下跌日的平均跌幅 < XJO 平均跌幅的 50%
                         （要求极其抗跌，甚至逆势上涨）
       · 条件 C（趋势护栏）: 当前价格站上 50 日均线
  3. 生成精简 Markdown 报告并保存到 Windows 桌面 (不发送邮件)

数据源: Yahoo Finance (yfinance)

运行: python asx_scanner.py
依赖: pip install yfinance pandas numpy

───────────────────────────────────────────────────────────────────────────────
变更历史 / 关键设计决策
───────────────────────────────────────────────────────────────────────────────
[2026-06-29] 彻底删除巴菲特价值基本面维度，重构为纯技术面动量筛选
  • 起因：回归核心信条——只关心「涨得更多、跌得更少」的相对强度，不再混入估值。
  • 删除：safe_get_info / compute_dividend_yield / 基本面交叉验证整块逻辑，
          以及 PE / 股息率 / ROE / 营运现金流 / PEG 所有获取与阈值。
  • 抗跌门槛收紧：RESIST_FACTOR 0.7 → 0.5（个股下跌日跌幅须 < 大盘跌幅的一半）。
  • 报告精简：剔除所有基本面表格，专注「纯动量强势股」（同时满足 A/B/C 三条件）。

[2026-06-25] 股票池扩展至完整 ASX200 (~194 只)
  • 起因：原 68 只大盘股池天然偏高 Beta，「领涨抗跌」组合极稀有，常出现 0 候选。
  • 已剔除退市/被收购：NCM(Newcrest), AWC(Alumina), ALU(Altium), AKE(Allkem), LNK(Link)。

[2026-06-24] 修正失效行业指数 + 下载重试机制
  • 房地产指数 ^AXRJ(失效) → ^AXPJ(A-REIT)；通信指数 ^AXKJ 无可用源，已移除
    （其成分股 TLS/TPG/REA 仍纳入个股池）。
  • safe_download 加入 3 次退避重试，应对偶发连接重置。

[2026-06-24] 本地化（原为云端定时任务，因云端 yfinance 遭 403 代理封锁而改本地）
  • 取消邮件发送；报告存至 Windows 桌面（兼容 OneDrive 重定向），跑完自动打开。
  • 由 Windows 计划任务「ASX Weekly Scanner」每周一 08:00 触发。
───────────────────────────────────────────────────────────────────────────────
"""

import os
import sys
import time
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# 下载重试配置
MAX_RETRIES = 3      # 每个标的最多尝试 3 次
RETRY_BACKOFF = 1.5  # 重试间隔基数（秒），按尝试次数递增

# Windows 终端默认 cp1252，无法打印中文；统一切到 UTF-8 (Python 3.7+)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"[错误] 缺少依赖: {e}")
    print("请先运行: pip install yfinance pandas numpy")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=100)   # ~3 个月交易日
MA_START   = END_DATE - timedelta(days=130)   # 50 日均线需额外历史
BENCHMARK  = "^AXJO"                           # S&P/ASX 200

# 技术面「抗跌」阈值：个股/板块在大盘下跌日的平均跌幅须 < 大盘平均跌幅 × 此系数 即算抗跌。
# 跌幅为负数：系数 0.5 表示个股跌幅须不及大盘的一半（极其抗跌，甚至逆势上涨）。
RESIST_FACTOR = 0.5

SECTORS = {
    "能源 Energy":        "^AXEJ",
    "材料 Materials":     "^AXMJ",
    "金融 Financials":    "^AXFJ",
    "工业 Industrials":   "^AXIJ",
    "可选消费 Cons.Disc": "^AXDJ",
    "必选消费 Cons.Stap": "^AXSJ",
    "信息技术 IT":        "^AXTJ",
    "医疗健康 Health":    "^AXHJ",
    # 通信 Comm.Svc：Yahoo 不提供可用的 ASX 通信板块指数（^AXKJ 已失效），
    # 故此处省略；其成分股 TLS/TPG/REA 仍纳入下方个股筛选池。
    "房地产 Real Estate": "^AXPJ",   # A-REIT 指数（原 ^AXRJ 已失效）
    "公用事业 Utilities": "^AXUJ",
}

# 完整 ASX200 成分股池 (~190 只，覆盖全部板块；含中盘防御性/低波动标的)。
# 已剔除退市/被收购：NCM(Newcrest), AWC(Alumina), ALU(Altium), AKE(Allkem合并), LNK(Link)。
# 个别代码若已变动，脚本会自动重试并跳过，不影响整体运行。
ASX200_STOCKS = {
    # ── Materials ──
    "BHP.AX": "BHP Group", "RIO.AX": "Rio Tinto", "FMG.AX": "Fortescue",
    "S32.AX": "South32", "MIN.AX": "Mineral Resources", "IGO.AX": "IGO",
    "LYC.AX": "Lynas RE", "NST.AX": "Northern Star", "EVN.AX": "Evolution Mining",
    "NEM.AX": "Newmont", "PLS.AX": "Pilbara Minerals", "JHX.AX": "James Hardie",
    "AMC.AX": "Amcor", "ORI.AX": "Orica", "BSL.AX": "BlueScope", "SGM.AX": "Sims",
    "ILU.AX": "Iluka", "SFR.AX": "Sandfire", "DRR.AX": "Deterra Royalties",
    "LTR.AX": "Liontown", "PDN.AX": "Paladin Energy", "BKW.AX": "Brickworks",
    "CMM.AX": "Capricorn Metals", "RRL.AX": "Regis Resources", "RMS.AX": "Ramelius",
    "GOR.AX": "Gold Road", "PRU.AX": "Perseus Mining", "NIC.AX": "Nickel Industries",
    "CIA.AX": "Champion Iron", "IPL.AX": "Incitec Pivot", "WGX.AX": "Westgold",
    "MAD.AX": "Mader Group", "VAU.AX": "Vault Minerals", "EMR.AX": "Emerald",
    # ── Energy ──
    "WDS.AX": "Woodside", "STO.AX": "Santos", "WHC.AX": "Whitehaven Coal",
    "NHC.AX": "New Hope", "VEA.AX": "Viva Energy", "BPT.AX": "Beach Energy",
    "KAR.AX": "Karoon Energy", "YAL.AX": "Yancoal", "ALD.AX": "Ampol",
    "COE.AX": "Cooper Energy",
    # ── Financials ──
    "CBA.AX": "CBA", "WBC.AX": "Westpac", "ANZ.AX": "ANZ", "NAB.AX": "NAB",
    "MQG.AX": "Macquarie", "SUN.AX": "Suncorp", "QBE.AX": "QBE",
    "IAG.AX": "IAG", "ASX.AX": "ASX Ltd", "CPU.AX": "Computershare",
    "BOQ.AX": "Bank of Queensland", "BEN.AX": "Bendigo Bank", "MFG.AX": "Magellan",
    "PPT.AX": "Perpetual", "PNI.AX": "Pinnacle", "AMP.AX": "AMP",
    "CGF.AX": "Challenger", "MPL.AX": "Medibank", "NHF.AX": "NIB Holdings",
    "HUB.AX": "Hub24", "NWL.AX": "Netwealth", "GQG.AX": "GQG Partners",
    "PXA.AX": "Pexa", "SDF.AX": "Steadfast", "AUB.AX": "AUB Group",
    "IFL.AX": "Insignia Financial", "QAL.AX": "Qualitas", "HLI.AX": "Helia",
    # ── Consumer Discretionary ──
    "WES.AX": "Wesfarmers", "JBH.AX": "JB Hi-Fi", "HVN.AX": "Harvey Norman",
    "ARB.AX": "ARB Corp", "SUL.AX": "Super Retail", "WEB.AX": "Webjet",
    "FLT.AX": "Flight Centre", "CTD.AX": "Corporate Travel", "ALL.AX": "Aristocrat",
    "LOV.AX": "Lovisa", "PMV.AX": "Premier Investments", "TPW.AX": "Temple & Webster",
    "BRG.AX": "Breville", "DMP.AX": "Domino's Pizza", "TAH.AX": "Tabcorp",
    "SGR.AX": "Star Entertainment", "IEL.AX": "IDP Education", "EVT.AX": "EVT Ltd",
    "BAP.AX": "Bapcor", "AX1.AX": "Accent Group", "NCK.AX": "Nick Scali",
    "ADH.AX": "Adairs", "KGN.AX": "Kogan", "LNW.AX": "Light & Wonder",
    "GUD.AX": "G.U.D. Holdings",
    # ── Consumer Staples ──
    "WOW.AX": "Woolworths", "COL.AX": "Coles", "TWE.AX": "Treasury Wine",
    "GNC.AX": "GrainCorp", "EDV.AX": "Endeavour Group", "MTS.AX": "Metcash",
    "A2M.AX": "A2 Milk", "BGA.AX": "Bega Cheese", "ELD.AX": "Elders",
    # ── Health Care ──
    "CSL.AX": "CSL", "RMD.AX": "ResMed", "COH.AX": "Cochlear",
    "SHL.AX": "Sonic Healthcare", "RHC.AX": "Ramsay Health",
    "PME.AX": "Pro Medicus", "FPH.AX": "Fisher & Paykel", "TLX.AX": "Telix Pharma",
    "NEU.AX": "Neuren Pharma", "CUV.AX": "Clinuvel", "NAN.AX": "Nanosonics",
    "PNV.AX": "PolyNovo", "IDX.AX": "Integral Diagnostics", "HLS.AX": "Healius",
    "EBO.AX": "Ebos Group", "SIG.AX": "Sigma Healthcare", "MVF.AX": "Monash IVF",
    "CU6.AX": "Clarity Pharma",
    # ── Information Technology ──
    "WTC.AX": "WiseTech Global", "XRO.AX": "Xero", "TNE.AX": "TechnologyOne",
    "NXT.AX": "NEXTDC", "DTL.AX": "Data#3", "MP1.AX": "Megaport",
    "CDA.AX": "Codan", "IRE.AX": "Iress", "ALQ.AX": "ALS Ltd",
    "APX.AX": "Appen", "EML.AX": "EML Payments", "LFS.AX": "Latitude",
    # ── Communication Services ──
    "TLS.AX": "Telstra", "TPG.AX": "TPG Telecom", "REA.AX": "REA Group",
    "CAR.AX": "CAR Group", "SEK.AX": "Seek", "NEC.AX": "Nine Entertainment",
    "NWS.AX": "News Corp", "SWM.AX": "Seven West", "SXL.AX": "SCA Media",
    # ── Real Estate ──
    "GMG.AX": "Goodman Group", "SCG.AX": "Scentre Group", "DXS.AX": "Dexus",
    "MGR.AX": "Mirvac", "LLC.AX": "Lendlease", "VCX.AX": "Vicinity Centres",
    "SGP.AX": "Stockland", "CHC.AX": "Charter Hall", "GPT.AX": "GPT Group",
    "CLW.AX": "Charter Hall LWR", "HMC.AX": "HMC Capital", "BWP.AX": "BWP Trust",
    "NSR.AX": "National Storage", "INA.AX": "Ingenia", "CQR.AX": "Charter Hall Retail",
    "ABP.AX": "Abacus", "HDN.AX": "HomeCo Daily Needs", "ARF.AX": "Arena REIT",
    "GOZ.AX": "Growthpoint", "CIP.AX": "Centuria Industrial",
    # ── Utilities ──
    "AGL.AX": "AGL Energy", "ORG.AX": "Origin Energy", "APA.AX": "APA Group",
    "MCY.AX": "Mercury NZ", "GNE.AX": "Genesis Energy", "ATL.AX": "Apex",
    # ── Industrials ──
    "TCL.AX": "Transurban", "AZJ.AX": "Aurizon", "QAN.AX": "Qantas",
    "WOR.AX": "Worley", "ALX.AX": "Atlas Arteria", "BXB.AX": "Brambles",
    "CWY.AX": "Cleanaway", "DOW.AX": "Downer EDI", "SVW.AX": "Seven Group",
    "REH.AX": "Reece", "QUB.AX": "Qube Holdings", "MND.AX": "Monadelphous",
    "NWH.AX": "NRW Holdings", "AIA.AX": "Auckland Airport", "VNT.AX": "Ventia",
    "CKF.AX": "Collins Foods", "EHL.AX": "Emeco", "SNL.AX": "Supply Network",
    "MAH.AX": "Macmahon", "IPH.AX": "IPH Ltd", "ORA.AX": "Orora",
    "PWH.AX": "PWR Holdings", "SPZ.AX": "Smart Parking",
}


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────
def safe_download(ticker, start, end):
    """下载价格数据，统一处理 MultiIndex 列。
    失败时最多重试 MAX_RETRIES 次（应对偶发连接重置），仍失败返回空 DataFrame。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.download(ticker, start=start, end=end,
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty:
                return df
            # 空数据：可能是真退市，也可能是偶发返回空——重试一次再判定
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return df  # 最终仍为空
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  [重试 {attempt}/{MAX_RETRIES}] {ticker}: {e}")
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                print(f"  [下载失败] {ticker}: {e}")
    return pd.DataFrame()


def get_returns(df, min_len=20):
    if df.empty or "Close" not in df.columns:
        return None
    r = df["Close"].squeeze().pct_change().dropna()
    return r if len(r) >= min_len else None


# ─────────────────────────────────────────────────────────────────────────────
# Block A —— 基准
# ─────────────────────────────────────────────────────────────────────────────
def analyse_benchmark():
    print("正在获取基准 ^AXJO ...")
    bm_df = safe_download(BENCHMARK, START_DATE, END_DATE)
    bm_ret = get_returns(bm_df)
    if bm_ret is None:
        print("[致命错误] 无法获取 ^AXJO 基准数据，终止。")
        sys.exit(1)

    up = bm_ret[bm_ret > 0]
    dn = bm_ret[bm_ret < 0]
    stats = {
        "ret": bm_ret,
        "avg_up": up.mean(),
        "avg_down": dn.mean(),
        "up_count": len(up),
        "dn_count": len(dn),
        "cumret": (1 + bm_ret).prod() - 1,
    }
    print(f"  XJO: {len(bm_ret)} 交易日 | 上涨 {len(up)} | 下跌 {len(dn)}")
    print(f"       均涨 {stats['avg_up']:.4f} | 均跌 {stats['avg_down']:.4f} "
          f"| 累计 {stats['cumret']:.2%}")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Block B —— 行业板块（领涨抗跌相对强度）
# ─────────────────────────────────────────────────────────────────────────────
def screen_sectors(bm):
    print("\n正在分析行业板块 ...")
    rows = []
    for name, ticker in SECTORS.items():
        ret = get_returns(safe_download(ticker, START_DATE, END_DATE))
        if ret is None:
            print(f"  [跳过] {name}: 无数据")
            continue
        aligned = ret.reindex(bm["ret"].index).dropna()
        bm_a = bm["ret"].reindex(aligned.index)
        avg_up = aligned[bm_a > 0].mean()
        avg_down = aligned[bm_a < 0].mean()
        beat_up = avg_up > bm["avg_up"]
        resist_down = avg_down > bm["avg_down"] * RESIST_FACTOR
        passed = bool(beat_up and resist_down)
        flag = "✅" if passed else ("⚠️" if (beat_up or resist_down) else "❌")
        print(f"  {flag} {name}: 涨 {avg_up:.4f} 跌 {avg_down:.4f}")
        rows.append(dict(name=name, ticker=ticker, avg_up=avg_up, avg_down=avg_down,
                         beat_up=beat_up, resist_down=resist_down, passed=passed))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Block C —— 个股纯技术筛选 (领涨 A + 抗跌 B + 趋势护栏 C)
# ─────────────────────────────────────────────────────────────────────────────
def screen_stocks_technical(bm):
    print("\n正在筛选个股技术面 (可能需 1-2 分钟) ...")
    rows = []
    for ticker, cname in ASX200_STOCKS.items():
        df = safe_download(ticker, MA_START, END_DATE)
        if df.empty or "Close" not in df.columns or len(df) < 55:
            continue
        try:
            close = df["Close"].squeeze()
            ma50 = close.rolling(50).mean()
            price = float(close.iloc[-1])
            ma50v = float(ma50.iloc[-1])
            above = price > ma50v   # 条件 C：站上 50 日均线

            ret = close.pct_change().dropna()
            aligned = ret.reindex(bm["ret"].index).dropna()
            if len(aligned) < 20:
                continue
            bm_a = bm["ret"].reindex(aligned.index)
            avg_up = float(aligned[bm_a > 0].mean())
            avg_down = float(aligned[bm_a < 0].mean())
            beat_up = avg_up > bm["avg_up"]                       # 条件 A：领涨
            resist_down = avg_down > bm["avg_down"] * RESIST_FACTOR  # 条件 B：抗跌

            rows.append(dict(
                ticker=ticker.replace(".AX", ""), full_ticker=ticker, name=cname,
                price=round(price, 2), ma50=round(ma50v, 2), above_ma50=above,
                avg_up=avg_up, avg_down=avg_down,
                beat_up=beat_up, resist_down=resist_down,
                rs_pass=bool(above and beat_up and resist_down),
            ))
        except Exception as e:
            print(f"  [错误] {ticker}: {e}")
    passing = [r for r in rows if r["rs_pass"]]
    print(f"  共扫描 {len(rows)} 只 | 三条件全通过 {len(passing)} 只")
    return rows, passing


# ─────────────────────────────────────────────────────────────────────────────
# Block D —— 生成精简 Markdown 报告（纯动量强势股）
# ─────────────────────────────────────────────────────────────────────────────
def build_report(bm, sectors, all_stocks, passing_stocks):
    today = datetime.today().strftime("%Y-%m-%d")
    period = f"{START_DATE.strftime('%Y-%m-%d')} ~ {today}"
    L = []
    A = L.append

    A("# ASX 纯动量「领涨抗跌」强势股报告")
    A(f"**报告日期**: {today}  |  **分析区间**: {period}  |  **数据源**: Yahoo Finance")
    A("")
    A("> *\"When the market goes up, there's certain stocks and sectors go up a lot more,")
    A("> and when the market goes down those things don't really fall.\"*")
    A("")
    A("---\n")

    # 市场总览
    A("## 一、市场总览（基准：S&P/ASX 200 — ^AXJO）\n")
    A("| 指标 | 数值 |")
    A("|------|------|")
    A(f"| 区间累计涨跌 | {bm['cumret']:+.2%} |")
    A(f"| 总交易日数 | {len(bm['ret'])} 天 |")
    A(f"| 上涨日 | {bm['up_count']} 天（均涨 {bm['avg_up']:.3%}）|")
    A(f"| 下跌日 | {bm['dn_count']} 天（均跌 {bm['avg_down']:.3%}）|")
    A("\n---\n")

    # 筛选框架
    A("## 二、筛选框架（纯技术面，三条件须同时满足）\n")
    A("> - **条件 A（领涨）**：个股在 XJO 上涨日的平均涨幅 **>** XJO 平均涨幅")
    A(f"> - **条件 B（抗跌）**：个股在 XJO 下跌日的平均跌幅 **<** XJO 平均跌幅的 "
      f"**{RESIST_FACTOR:.0%}**（极其抗跌，甚至逆势上涨）")
    A("> - **条件 C（趋势护栏）**：当前价格站上 **50 日均线**\n")
    A("---\n")

    # 行业板块
    A("## 三、行业板块「领涨抗跌」相对强度\n")
    A(f"基准上涨日均涨幅 **{bm['avg_up']:.3%}** | 下跌日均跌幅 **{bm['avg_down']:.3%}** "
      f"| 抗跌阈值 **{bm['avg_down']*RESIST_FACTOR:.3%}**（基准跌幅×{RESIST_FACTOR:.0%}）\n")
    A("| 板块 | 上涨日均涨幅 | 下跌日均跌幅 | 条件A | 条件B | 结论 |")
    A("|------|:-----------:|:-----------:|:-----:|:-----:|:----:|")
    for r in sorted(sectors, key=lambda x: (-int(x["passed"]), -x["avg_up"])):
        verdict = "✅ **通过**" if r["passed"] else "❌ 未通过"
        A(f"| {r['name']} | {r['avg_up']:+.3%} | {r['avg_down']:+.3%} | "
          f"{'✓' if r['beat_up'] else '✗'} | {'✓' if r['resist_down'] else '✗'} | {verdict} |")
    A("")
    passed_sectors = [r["name"] for r in sectors if r["passed"]]
    if passed_sectors:
        A(f"**通过板块（{len(passed_sectors)} 个）**: {', '.join(passed_sectors)}")
    else:
        A("**本周无板块同时满足两项条件。** 建议关注仅满足单项条件（⚠️）的板块。")
    A("\n---\n")

    # 纯动量强势股
    A("## ⭐ 四、纯动量强势股（同时满足 A / B / C 三条件）\n")
    A("> 大盘涨时涨得更多、大盘跌时几乎不跌，且站稳 50 日均线之上的稀有标的。\n")
    if passing_stocks:
        A("| 代码 | 名称 | 现价(A$) | 50日均线 | 高于均线 | 上涨日均涨幅 | 超额涨幅 | 下跌日均跌幅 | 抗跌比率 |")
        A("|------|------|--------:|--------:|:-------:|:-----------:|:-------:|:-----------:|:-------:|")
        for r in sorted(passing_stocks, key=lambda x: -x["avg_up"]):
            pct = r["price"] / r["ma50"] - 1
            excess = r["avg_up"] - bm["avg_up"]
            resist_ratio = r["avg_down"] / bm["avg_down"] if bm["avg_down"] else 0
            A(f"| **{r['ticker']}** | {r['name']} | {r['price']} | {r['ma50']} | "
              f"+{pct:.1%} | {r['avg_up']:+.3%} | {excess:+.3%} | {r['avg_down']:+.3%} | "
              f"{resist_ratio:.0%} |")
        A(f"\n**共 {len(passing_stocks)} 只标的同时满足三个条件。**\n")

        # 逐只点评（前 8 只）
        for r in sorted(passing_stocks, key=lambda x: -x["avg_up"])[:8]:
            pct = r["price"] / r["ma50"] - 1
            excess = r["avg_up"] - bm["avg_up"]
            resist_ratio = r["avg_down"] / bm["avg_down"] if bm["avg_down"] else 0
            A(f"### {r['ticker']} — {r['name']}")
            A(f"- **领涨 (A)**: 上涨日均涨 {r['avg_up']:+.3%}，超越 XJO **{excess:+.3%}**")
            A(f"- **抗跌 (B)**: 下跌日均跌 {r['avg_down']:+.3%}，仅为 XJO 跌幅的 "
              f"**{resist_ratio:.0%}**")
            A(f"- **趋势 (C)**: 现价 A${r['price']}，高于 50 日均线 **{pct:+.1%}**")
            A("")
    else:
        A("**本周无个股同时满足全部三个条件。**\n")
        A("观察名单（满足条件 A 领涨且站上均线，但抗跌性 B 不足）：\n")
        watch = [r for r in all_stocks
                 if r["above_ma50"] and r["beat_up"] and not r["resist_down"]]
        if watch:
            A("| 代码 | 名称 | 现价(A$) | 上涨日均涨幅 | 下跌日均跌幅 |")
            A("|------|------|-------:|:-----------:|:-----------:|")
            for r in sorted(watch, key=lambda x: -x["avg_up"])[:10]:
                A(f"| {r['ticker']} | {r['name']} | {r['price']} | "
                  f"{r['avg_up']:+.3%} | {r['avg_down']:+.3%} |")
        else:
            A("（暂无接近标的。）")
    A("\n---\n")

    # 免责声明
    A("## 免责声明\n")
    A("本报告由本地 Python 脚本基于 Yahoo Finance 历史数据自动生成，分析区间约 3 个月。")
    A("**仅供参考，不构成投资建议。** 过去的相对强度不代表未来收益，"
      "投资有风险，请独立判断。")
    A(f"\n*自动生成于 {today} | asx_scanner.py 本地运行*")

    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# 桌面路径解析 (兼容 OneDrive 重定向)
# ─────────────────────────────────────────────────────────────────────────────
def resolve_desktop():
    candidates = []
    up = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if one:
        candidates.append(os.path.join(one, "Desktop"))
    candidates.append(os.path.join(up, "Desktop"))
    candidates.append(os.path.join(up, "OneDrive", "Desktop"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    # 兜底：默认 Desktop 路径（即便不存在也返回，由调用方创建）
    return os.path.join(up, "Desktop")


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("ASX 纯动量「领涨抗跌」强势股扫描器")
    print("=" * 60)

    bm = analyse_benchmark()
    sectors = screen_sectors(bm)
    all_stocks, passing_stocks = screen_stocks_technical(bm)

    report = build_report(bm, sectors, all_stocks, passing_stocks)

    # 保存到桌面
    desktop = resolve_desktop()
    os.makedirs(desktop, exist_ok=True)
    fname = f"ASX_报告_{datetime.today().strftime('%Y%m%d')}.md"
    out_path = os.path.join(desktop, fname)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + "=" * 60)
    print(f"✅ 报告已保存到桌面:\n   {out_path}")
    print("=" * 60)

    # 跑完自动用默认程序打开报告
    try:
        os.startfile(out_path)   # type: ignore[attr-defined]  # Windows 专有
        print("📖 已自动打开报告。")
    except Exception as e:
        print(f"（自动打开报告失败，可手动打开：{e}）")


if __name__ == "__main__":
    main()
