# -*- coding: utf-8 -*-
"""
ASX 相对强度 + 巴菲特价值 双重选股扫描器 (本地版)
===================================================
逻辑:
  1. 行业板块筛选  —— 11 个 ASX 行业指数的「领涨抗跌」相对强度
  2. 个股技术筛选  —— ASX200 成分股，站上 50 日均线 + 领涨抗跌
  3. 基本面交叉验证 —— 对技术面通过者做巴菲特价值四项检验
  4. 生成 Markdown 报告并保存到 Windows 桌面 (不发送邮件)

数据源: Yahoo Finance (yfinance)

运行: python asx_scanner.py
依赖: pip install yfinance pandas numpy

───────────────────────────────────────────────────────────────────────────────
变更历史 / 关键设计决策
───────────────────────────────────────────────────────────────────────────────
[2026-06-25] 股票池扩展至完整 ASX200 (~194 只)
  • 起因：原 68 只大盘股池天然偏高 Beta，「领涨抗跌」组合极稀有，常出现 0 候选。
  • 诊断证实瓶颈在股票池广度（非筛选阈值）：扩池后技术面通过 1→8 只、双击 0→2 只。
  • 涌现出的双击标的（如 DRR 特许权、PMV 零售）正是原窄池缺失的低波动/防御型标的。
  • 已剔除退市/被收购：NCM(Newcrest), AWC(Alumina), ALU(Altium), AKE(Allkem), LNK(Link)。

[2026-06-25] 修复股息率单位 bug（重要 — 否则污染结果）
  • 现象：CDA(Codan) 真实股息率 0.89% 被误算成 89%，导致其假冒「双击」。
  • 根因：当前 yfinance 的 dividendYield 字段为【百分数单位】(0.89 表示 0.89%)，
          旧逻辑误把 <1 的值当作小数（0.89 → 89%）。
  • 修复：compute_dividend_yield() 优先用【每股股息 dividendRate ÷ 现价】直接计算
          （单位无歧义），回退时统一 ÷100，并设 30% 合理性上限过滤脏数据。

[2026-06-24] 引入 PEG 估值维度
  • 「估值合理」改为 PE ≤ 20 *或* PEG < 1.5 任一达标，避免错杀高增长动量股。
  • 报告各表新增 PEG 列。

[2026-06-24] 放宽阈值（应对当前市场过严）
  • PE 门槛 15 → 20；股息率 4% → 3%。
  • 技术面抗跌系数 RESIST_FACTOR 0.5 → 0.7（注：诊断显示该项非主要瓶颈，
    真正杠杆是上面的扩池；继续放宽会引入高 Beta 股、背离「抗跌」本意，故止于 0.7）。

[2026-06-24] 修正失效行业指数 + 下载重试机制
  • 房地产指数 ^AXRJ(失效) → ^AXPJ(A-REIT)；通信指数 ^AXKJ 无可用源，已移除
    （其成分股 TLS/TPG/REA 仍纳入个股池）。
  • safe_download / safe_get_info 加入 3 次退避重试，应对偶发连接重置。

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

# 价值筛选阈值（2026-06 放宽：当前市场下原 PE15/股息4% 过严，易出现 0 完美候选）
PE_MAX     = 20.0    # 市盈率 <= 20（原 15）
DY_MIN     = 0.03    # 股息率 >= 3%（原 4%）
ROE_MIN    = 0.15    # 净资产收益率 >= 15%
PEG_FAIR   = 1.5     # PEG < 1.5 视为估值合理（< 1 低估，1-1.5 合理）
# 营运现金流 > 0

# 技术面「抗跌」阈值：个股/板块下跌日均跌幅 > 基准跌幅 × 此系数 即算抗跌。
# 跌幅为负数，系数越大门槛越宽松。2026-06 由 0.5 放宽至 0.7。
RESIST_FACTOR = 0.7

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
    "WDS.AX": "Woodside", "COE.AX": "Cooper Energy",
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
    "CU6.AX": "Clarity Pharma", "RMD.AX": "ResMed",
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


def safe_get_info(full_ticker):
    """获取基本面 info，带重试。失败返回空 dict。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            info = yf.Ticker(full_ticker).info
            if info:
                return info
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                print(f"  [基本面获取失败] {full_ticker}: {e}")
    return {}


def get_returns(df, min_len=20):
    if df.empty or "Close" not in df.columns:
        return None
    r = df["Close"].squeeze().pct_change().dropna()
    return r if len(r) >= min_len else None


def compute_dividend_yield(info):
    """稳健计算股息率（小数形式，如 0.055=5.5%）。
    优先用 每股股息 dividendRate / 现价（单位无歧义）；
    回退到 dividendYield 字段——当前 yfinance 该字段为百分数单位（0.89 表示 0.89%），故统一 ÷100。
    设 30% 合理性上限，超出视为脏数据返回 None。"""
    rate = info.get("dividendRate")
    price = info.get("currentPrice") or info.get("previousClose") or info.get("regularMarketPrice")
    dy = None
    try:
        if rate is not None and price:
            dy = float(rate) / float(price)
    except (TypeError, ValueError, ZeroDivisionError):
        dy = None
    if dy is None:
        raw = info.get("dividendYield")
        try:
            dy = float(raw) / 100.0 if raw is not None else None  # 字段为百分数单位
        except (TypeError, ValueError):
            dy = None
    if dy is not None and (dy < 0 or dy > 0.30):   # 合理性上限，过滤脏数据
        return None
    return dy


def fmt_pct(x, dp=3):
    return "N/A" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{dp}%}"


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
# Block B —— 行业板块
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
# Block C —— 个股技术面 (相对强度 + 50 日均线)
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
            above = price > ma50v

            ret = close.pct_change().dropna()
            aligned = ret.reindex(bm["ret"].index).dropna()
            if len(aligned) < 20:
                continue
            bm_a = bm["ret"].reindex(aligned.index)
            avg_up = float(aligned[bm_a > 0].mean())
            avg_down = float(aligned[bm_a < 0].mean())
            beat_up = avg_up > bm["avg_up"]
            resist_down = avg_down > bm["avg_down"] * RESIST_FACTOR

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
    print(f"  共扫描 {len(rows)} 只 | 技术面通过 {len(passing)} 只")
    return rows, passing


# ─────────────────────────────────────────────────────────────────────────────
# Block D —— 基本面交叉验证 (巴菲特价值)
# ─────────────────────────────────────────────────────────────────────────────
def fundamental_validation(passing_stocks):
    print("\n正在做基本面交叉验证 (巴菲特价值四项) ...")
    for s in passing_stocks:
        pe = dy = roe = ocf = peg = None
        info = safe_get_info(s["full_ticker"])
        if info:
            pe = info.get("trailingPE") or info.get("forwardPE")
            dy = compute_dividend_yield(info)
            roe = info.get("returnOnEquity")
            ocf = info.get("operatingCashflow") or info.get("freeCashflow")
            peg = info.get("trailingPegRatio") or info.get("pegRatio")

        def _f(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        pe, roe, ocf, peg = _f(pe), _f(roe), _f(ocf), _f(peg)

        # 估值合理：PE 达标 或 PEG 合理（任一即可，避免错杀高增长动量股）
        pe_ok  = pe is not None and pe > 0 and pe <= PE_MAX
        peg_ok = peg is not None and peg > 0 and peg < PEG_FAIR
        val_ok = pe_ok or peg_ok
        dy_ok  = dy is not None and dy >= DY_MIN
        roe_ok = roe is not None and roe >= ROE_MIN
        ocf_ok = ocf is not None and ocf > 0

        s.update(pe=pe, dy=dy, roe=roe, ocf=ocf, peg=peg,
                 pe_ok=pe_ok, peg_ok=peg_ok, val_ok=val_ok,
                 dy_ok=dy_ok, roe_ok=roe_ok, ocf_ok=ocf_ok,
                 criteria_met=sum([val_ok, dy_ok, roe_ok, ocf_ok]),
                 dbl_play=bool(val_ok and dy_ok and roe_ok and ocf_ok))

        pe_str = f"{pe:.1f}x" if pe is not None else "N/A"
        peg_str = f"{peg:.2f}" if peg is not None else "N/A"
        dy_str = f"{dy:.2%}" if dy is not None else "N/A"
        roe_str = f"{roe:.2%}" if roe is not None else "N/A"
        mark = "⭐ 双击!" if s["dbl_play"] else f"{s['criteria_met']}/4"
        print(f"  {s['ticker']:<6} PE={pe_str:<7} PEG={peg_str:<6} DY={dy_str:<7} "
              f"ROE={roe_str:<8} OCF={'+' if ocf_ok else '-'}  -> {mark}")

    double_play = [s for s in passing_stocks if s["dbl_play"]]
    print(f"  动量+价值双击标的: {len(double_play)} 只")
    return double_play


# ─────────────────────────────────────────────────────────────────────────────
# Block E —— 生成 Markdown 报告
# ─────────────────────────────────────────────────────────────────────────────
def build_report(bm, sectors, all_stocks, passing_stocks, double_play):
    today = datetime.today().strftime("%Y-%m-%d")
    period = f"{START_DATE.strftime('%Y-%m-%d')} ~ {today}"
    L = []
    A = L.append

    A("# ASX 相对强度 + 价值双重筛选报告")
    A(f"**报告日期**: {today}  |  **分析区间**: {period}  |  **数据源**: Yahoo Finance")
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
    A("## 二、筛选框架\n")
    A("**阶段一 · 技术面「领涨抗跌」**")
    A("> - 条件 A：XJO 上涨日，平均涨幅 > XJO 均值")
    A(f"> - 条件 B：XJO 下跌日，平均跌幅 < XJO 跌幅的 {RESIST_FACTOR:.0%}（抗跌）")
    A("> - 条件 C（个股）：价格站上 50 日均线\n")
    A("**阶段二 · 巴菲特价值基本面**")
    A(f"> - **估值合理**：市盈率 PE ≤ {PE_MAX:.0f} 倍 **或** PEG < {PEG_FAIR}"
      f"（任一即可，PEG 用于避免错杀高增长动量股）")
    A(f"> - 股息率 ≥ {DY_MIN:.0%}")
    A(f"> - 净资产收益率 ROE ≥ {ROE_MIN:.0%}")
    A("> - 营运现金流 (Operating Cash Flow) 为正")
    A(f"> - *PEG 参考：< 1 低估，1~{PEG_FAIR} 合理*\n")
    A("---\n")

    # 行业板块
    A("## 三、行业板块筛选\n")
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

    # 个股技术面
    A("## 四、个股技术面筛选（50日均线上 + 领涨抗跌）\n")
    if passing_stocks:
        A("| 代码 | 名称 | 现价(A$) | 50日均线 | 高于均线 | 上涨日均涨幅 | 下跌日均跌幅 |")
        A("|------|------|--------:|--------:|:-------:|:-----------:|:-----------:|")
        for r in sorted(passing_stocks, key=lambda x: -x["avg_up"]):
            pct = r["price"] / r["ma50"] - 1
            A(f"| **{r['ticker']}** | {r['name']} | {r['price']} | {r['ma50']} | "
              f"+{pct:.1%} | {r['avg_up']:+.3%} | {r['avg_down']:+.3%} |")
        A(f"\n**共 {len(passing_stocks)} 只标的通过技术面筛选。**")
    else:
        A("**本周无个股同时满足全部技术条件。**\n")
        A("观察名单（满足条件A且站上均线，但抗跌性不足）：")
        watch = [r for r in all_stocks
                 if r["above_ma50"] and r["beat_up"] and not r["resist_down"]]
        if watch:
            A("| 代码 | 名称 | 现价 | 上涨日均涨幅 | 下跌日均跌幅 |")
            A("|------|------|-----:|:-----------:|:-----------:|")
            for r in sorted(watch, key=lambda x: -x["avg_up"])[:10]:
                A(f"| {r['ticker']} | {r['name']} | A${r['price']} | "
                  f"{r['avg_up']:+.3%} | {r['avg_down']:+.3%} |")
    A("\n---\n")

    # 基本面交叉验证
    A("## 五、基本面交叉验证（巴菲特价值四项）\n")
    if passing_stocks:
        A("| 代码 | 名称 | PE | PEG | 股息率 | ROE | 营运现金流 | 估值✓ | 股息✓ | ROE✓ | OCF✓ | 双击 |")
        A("|------|------|---:|----:|------:|----:|:---------:|:----:|:----:|:----:|:----:|:---:|")
        for r in sorted(passing_stocks, key=lambda x: -x["criteria_met"]):
            pe_s = f"{r['pe']:.1f}x" if r["pe"] is not None else "N/A"
            peg_s = f"{r['peg']:.2f}" if r["peg"] is not None else "N/A"
            dy_s = f"{r['dy']:.2%}" if r["dy"] is not None else "N/A"
            roe_s = f"{r['roe']:.2%}" if r["roe"] is not None else "N/A"
            ocf_s = "正" if r["ocf_ok"] else ("负" if r["ocf"] is not None else "N/A")
            A(f"| {r['ticker']} | {r['name']} | {pe_s} | {peg_s} | {dy_s} | {roe_s} | {ocf_s} | "
              f"{'✓' if r['val_ok'] else '✗'} | {'✓' if r['dy_ok'] else '✗'} | "
              f"{'✓' if r['roe_ok'] else '✗'} | {'✓' if r['ocf_ok'] else '✗'} | "
              f"{'⭐' if r['dbl_play'] else ''} |")
        A("\n*估值✓ = PE≤" + f"{PE_MAX:.0f}" + " 或 PEG<" + f"{PEG_FAIR}" + " 任一达标*")
    else:
        A("无技术面通过标的，基本面验证已跳过。")
    A("\n---\n")

    # 双击高亮
    A("## ⭐ 六、动量 + 价值「双击」标的\n")
    A("> 同时满足**技术面领涨抗跌**与**全部四项价值标准**的稀有标的。\n")
    if double_play:
        A("| 代码 | 名称 | 现价(A$) | PE | PEG | 股息率 | ROE | 超额涨幅 | 抗跌比率 |")
        A("|------|------|--------:|---:|----:|------:|----:|:-------:|:-------:|")
        for r in sorted(double_play, key=lambda x: -x["avg_up"]):
            excess = r["avg_up"] - bm["avg_up"]
            resist_ratio = r["avg_down"] / bm["avg_down"] if bm["avg_down"] else 0
            pe_s = f"{r['pe']:.1f}x" if r["pe"] is not None else "N/A"
            peg_s = f"{r['peg']:.2f}" if r["peg"] is not None else "N/A"
            A(f"| **{r['ticker']}** | {r['name']} | {r['price']} | {pe_s} | {peg_s} | "
              f"{r['dy']:.2%} | {r['roe']:.2%} | {excess:+.3%} | {resist_ratio:.0%} |")
        A("")
        for r in sorted(double_play, key=lambda x: -x["avg_up"])[:5]:
            pct = r["price"] / r["ma50"] - 1
            resist_ratio = r["avg_down"] / bm["avg_down"] if bm["avg_down"] else 0
            pe_s = f"{r['pe']:.1f}x" if r["pe"] is not None else "N/A"
            peg_s = f"{r['peg']:.2f}" if r["peg"] is not None else "N/A"
            val_note = "PE 达标" if r["pe_ok"] else (f"PE偏高但 PEG={peg_s} 合理" if r["peg_ok"] else "")
            A(f"### {r['ticker']} — {r['name']}")
            A(f"- **技术面**: 现价 A${r['price']}，高于50日均线 **{pct:+.1%}**；"
              f"上涨日均涨 {r['avg_up']:+.3%}（超 XJO {r['avg_up']-bm['avg_up']:+.3%}），"
              f"下跌日仅承受 XJO 跌幅的 {resist_ratio:.0%}")
            A(f"- **基本面**: PE **{pe_s}**，PEG **{peg_s}**，股息率 **{r['dy']:.2%}**，"
              f"ROE **{r['roe']:.2%}**，营运现金流为正（{val_note}）")
            A(f"- **简评**: 兼具上行动量、抗跌韧性与价值安全边际，建议结合最新财报、"
              f"行业催化剂及负债结构进一步确认。")
            A("")
    elif passing_stocks:
        A("**本周无完美双击标的。** 以下为最接近的候选（按满足价值条件数排序）：\n")
        A("| 代码 | 名称 | PE | PEG | 股息率 | ROE | 营运现金流 | 满足条件 |")
        A("|------|------|---:|----:|------:|----:|:---------:|:-------:|")
        for r in sorted(passing_stocks, key=lambda x: -x["criteria_met"])[:5]:
            pe_s = f"{r['pe']:.1f}x" if r["pe"] is not None else "N/A"
            peg_s = f"{r['peg']:.2f}" if r["peg"] is not None else "N/A"
            dy_s = f"{r['dy']:.2%}" if r["dy"] is not None else "N/A"
            roe_s = f"{r['roe']:.2%}" if r["roe"] is not None else "N/A"
            ocf_s = "正" if r["ocf_ok"] else ("负" if r["ocf"] is not None else "N/A")
            A(f"| {r['ticker']} | {r['name']} | {pe_s} | {peg_s} | {dy_s} | {roe_s} | {ocf_s} | "
              f"{r['criteria_met']}/4 |")
    else:
        A("本周无满足技术面的标的，故无双击候选。")
    A("\n---\n")

    # 免责声明
    A("## 免责声明\n")
    A("本报告由本地 Python 脚本基于 Yahoo Finance 历史数据自动生成，分析区间约 3 个月。")
    A("**仅供参考，不构成投资建议。** 过去的相对强度与基本面不代表未来收益，"
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
    print("ASX 相对强度 + 巴菲特价值 双重选股扫描器")
    print("=" * 60)

    bm = analyse_benchmark()
    sectors = screen_sectors(bm)
    all_stocks, passing_stocks = screen_stocks_technical(bm)
    double_play = fundamental_validation(passing_stocks) if passing_stocks else []

    report = build_report(bm, sectors, all_stocks, passing_stocks, double_play)

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
