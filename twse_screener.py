# -*- coding: utf-8 -*-
"""
台股纯动量「领涨抗跌」强势股筛选器 (TWSE Momentum Screener) — 动态市值池
====================================================================
核心且唯一的选股信条:
  "When the market goes up, certain stocks go up a lot more,
   and when the market goes down those things don't really fall."

逻辑 (与 ASX 扫描器一致的纯动量三条件, 同时满足才算「强势股」):
  · 条件 A（领涨）: 个股在 ^TWII 上涨日的平均涨幅 > ^TWII 平均涨幅
  · 条件 B（抗跌）: 个股在 ^TWII 下跌日的平均跌幅 < ^TWII 平均跌幅的 50%
                    (跌幅不及大盘一半, 极其抗跌甚至逆势上涨)
  · 条件 C（趋势护栏）: 当前价格站上 50 日均线

工作流:
  1. 每次运行自动获取台股 (上市 .TW + 上柜 .TWO) 市值前 TOP_N 大作为基础池。
  2. 对每只股票按上述三条件做纯技术面筛选 (基准 ^TWII)。
  3. 三条件全通过即入选「纯动量强势股」; 另列出仅差抗跌一项的观察名单。

报告 (Markdown) 列出: 代码、现价、50 日均线、上涨日均涨幅、下跌日均跌幅。
"""

import os
import sys
import time
from datetime import datetime

# 确保 Windows 控制台以 UTF-8 输出中文
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import yfinance as yf
    import pandas as pd
except ImportError as e:
    print(f"[错误] 缺少依赖库: {e.name}。请先运行: pip install yfinance pandas")
    sys.exit(1)


# ------------------------------------------------------------------
# 配置区
# ------------------------------------------------------------------
BENCHMARK = "^TWII"        # 台湾加权指数
DATA_LOOKBACK = "6mo"      # 价格下载区间 (需覆盖 50 日均线 + 收益分析窗口)
RETURN_WINDOW_TD = 63      # 「领涨抗跌」日收益分析窗口 (~3 个月交易日, 对齐 ASX)
MA_WINDOW = 50             # 趋势护栏: 50 日均线
RESIST_FACTOR = 0.5        # 抗跌系数: 个股下跌日跌幅须 < 大盘跌幅 × 0.5 (不及一半)

# 下载重试配置: 批量下载偶发限流会漏抓 (尤其权重股), 对漏抓标的逐只重试。
MAX_RETRIES = 4            # 单只最多尝试次数
RETRY_BACKOFF = 2.0        # 退避间隔基数(秒), 按尝试次数递增 (2,4,6...)
RETRY_PAUSE = 0.4          # 每次逐只重抓之间的固定延迟(秒), 给 yfinance 喘息
TOP_N_MAIN = 150           # 主板(上市 .TW) 取市值前 N 大
TOP_N_OTC = 100            # 上柜(OTC .TWO) 取市值前 N 大
# 合并后约 250 只的最终候选宇宙

# 候选宇宙: 台湾市值较大的一批上市公司 (~150 只), 运行时按实时市值取前 TOP_N。
# 不在前 TOP_N 的会被自动剔除; 取不到市值的标的也会被跳过。
CANDIDATE_UNIVERSE = {
    # 半导体 / 电子
    "2330.TW": "台积电", "2454.TW": "联发科", "2317.TW": "鸿海", "2308.TW": "台达电",
    "2303.TW": "联电", "3711.TW": "日月光投控", "2379.TW": "瑞昱", "2345.TW": "智邦",
    "3034.TW": "联咏", "3231.TW": "纬创", "2376.TW": "技嘉", "2382.TW": "广达",
    "2357.TW": "华硕", "4938.TW": "和硕", "2395.TW": "研华", "3008.TW": "大立光",
    "2474.TW": "可成", "2327.TW": "国巨", "2356.TW": "英业达", "2377.TW": "微星",
    "2353.TW": "宏碁", "2409.TW": "友达", "3481.TW": "群创", "3037.TW": "欣兴",
    "3017.TW": "奇鋐", "3661.TW": "世芯-KY", "3443.TW": "创意", "6415.TW": "矽力-KY",
    "5269.TW": "祥硕", "2368.TW": "金像电", "2383.TW": "台光电", "6770.TW": "力积电",
    "2344.TW": "华邦电", "8046.TW": "南电", "3533.TW": "嘉泽", "2492.TW": "华新科",
    "2301.TW": "光宝科", "2324.TW": "仁宝", "6285.TW": "启碁", "2360.TW": "致茂",
    "2449.TW": "京元电子", "3035.TW": "智原", "3653.TW": "健策", "2385.TW": "群光",
    "2347.TW": "联强", "2059.TW": "川湖", "6239.TW": "力成",
    "2049.TW": "上银", "1590.TW": "亚德客-KY", "3702.TW": "大联大", "2404.TW": "汉唐",
    "6213.TW": "聯茂", "6669.TW": "信骅", "6531.TW": "爱普", "3596.TW": "智易",
    "8016.TW": "矽创", "6789.TW": "采鈺", "2393.TW": "亿光",
    "3406.TW": "玉晶光",
    # 电信
    "2412.TW": "中华电信", "3045.TW": "台湾大", "4904.TW": "远传",
    # 重电 / 机电
    "1519.TW": "华城", "1513.TW": "中兴电", "1503.TW": "士电", "2371.TW": "大同",
    # 金融
    "2882.TW": "国泰金", "2881.TW": "富邦金", "2891.TW": "中信金", "2886.TW": "兆丰金",
    "2884.TW": "玉山金", "2885.TW": "元大金", "2892.TW": "第一金", "2880.TW": "华南金",
    "2887.TW": "台新金", "2890.TW": "永丰金", "2883.TW": "开发金", "5880.TW": "合库金",
    "2801.TW": "彰银", "5876.TW": "上海商银", "2889.TW": "国票金",
    "2834.TW": "臺企银", "5871.TW": "中租-KY", "2845.TW": "远东银", "2812.TW": "台中银",
    "2867.TW": "三商寿", "9941.TW": "裕融",
    # 塑化 / 原物料 / 钢铁 / 水泥 / 食品
    "1301.TW": "台塑", "1303.TW": "南亚", "1326.TW": "台化", "6505.TW": "台塑化",
    "2002.TW": "中钢", "1101.TW": "台泥", "1102.TW": "亚泥", "1216.TW": "统一",
    "1605.TW": "华新", "1722.TW": "台肥", "1227.TW": "佳格", "1229.TW": "联华",
    "1707.TW": "葡萄王", "9933.TW": "中鼎",
    # 汽车 / 轮胎 / 纺织 / 制鞋 / 自行车
    "2207.TW": "和泰车", "2105.TW": "正新", "1402.TW": "远东新", "9910.TW": "丰泰",
    "9904.TW": "宝成", "1476.TW": "儒鸿", "1477.TW": "聚阳", "9921.TW": "巨大",
    "9914.TW": "美利达", "2227.TW": "裕日车",
    # 航运 / 航空
    "2603.TW": "长荣", "2609.TW": "阳明", "2615.TW": "万海", "2618.TW": "长荣航",
    "2610.TW": "华航",
    # 零售 / 消费 / 医疗 / 营建 / 工程
    "2912.TW": "统一超", "8454.TW": "富邦媒", "1795.TW": "美时", "6446.TW": "藥華藥",
    "1504.TW": "东元", "2723.TW": "美食-KY", "9945.TW": "润泰新", "2915.TW": "润泰全",
    "2542.TW": "兴富发", "2548.TW": "华固", "8464.TW": "億豐",
    # 扩充段 (支撑市值前 150 的基础池)
    "6196.TW": "帆宣", "2451.TW": "创见", "2421.TW": "建准", "3030.TW": "德律",
    "8210.TW": "勤诚", "6271.TW": "同欣电", "5388.TW": "中磊",
    "2388.TW": "威盛", "4763.TW": "材料-KY", "6781.TW": "AES-KY",
    "4961.TW": "天钰", "1313.TW": "联成", "1314.TW": "中石化", "1717.TW": "长兴",
    "1210.TW": "大成", "1201.TW": "味全", "2820.TW": "华票", "2855.TW": "统一证",
    "6005.TW": "群益证", "1737.TW": "臺盐", "2204.TW": "中华车", "2231.TW": "为升",
    "3023.TW": "信邦", "3019.TW": "亚光", "2027.TW": "大成钢", "2015.TW": "丰兴",
    # 由上柜转上市的标的 (Yahoo 以 .TW 提供数据, 原误置于 OTC)
    "6472.TW": "保瑞", "1773.TW": "胜一", "8021.TW": "尖点", "3576.TW": "联合再生",
    "6491.TW": "晶碩", "8478.TW": "东哥游艇", "6589.TW": "台康生技", "4736.TW": "泰博",
    "8341.TW": "日友", "6533.TW": "晶心科", "6477.TW": "安集",
}

# 上柜 (OTC) 候选宇宙 (.TWO 后缀, ~45 只), 运行时按实时市值取前 TOP_N_OTC。
# 注: 環球晶/群聯/譜瑞-KY/中美晶/世界先进 等其实是上柜股, 之前用 .TW 会 404,
# 移到此处后即可正确抓取。
OTC_UNIVERSE = {
    # 半导体 / 电子 (上柜)
    "6488.TWO": "环球晶", "8299.TWO": "群联", "4966.TWO": "谱瑞-KY", "5483.TWO": "中美晶",
    "5347.TWO": "世界先进", "3105.TWO": "稳懋", "5274.TWO": "信驊", "3529.TWO": "力旺",
    "6510.TWO": "精测", "6147.TWO": "頎邦", "3680.TWO": "家登",
    "6643.TWO": "M31", "8086.TWO": "宏捷科", "6182.TWO": "合晶",
    "6411.TWO": "晶焱", "5371.TWO": "中光电", "3324.TWO": "双鸿", "3260.TWO": "威刚",
    "3081.TWO": "联亚",
    # 软件 / 游戏 / 消费 / 零售
    "3293.TWO": "鈊象", "6180.TWO": "橘子", "5904.TWO": "宝雅",
    # 生技 / 医疗 (上柜)
    "4743.TWO": "合一", "4147.TWO": "中裕", "1565.TWO": "精华",
}

# 主板候选宇宙别名 (向后兼容)
MAIN_UNIVERSE = CANDIDATE_UNIVERSE


# ------------------------------------------------------------------
# 动态基础池: 按实时市值取前 TOP_N
# ------------------------------------------------------------------
def fetch_market_cap(tk):
    """优先用 fast_info, 退回 info 取市值。失败返回 None。"""
    try:
        fi = yf.Ticker(tk).fast_info
        mc = fi.get("market_cap") if hasattr(fi, "get") else fi["market_cap"]
        if mc:
            return float(mc)
    except Exception:
        pass
    try:
        mc = yf.Ticker(tk).info.get("marketCap")
        return float(mc) if mc else None
    except Exception:
        return None


def _rank_universe(universe, top_n, label):
    """对单个候选宇宙取实时市值排序, 返回前 top_n 的 [(代码, 名称, 市值)]。"""
    print(f"  [{label}] 自 {len(universe)} 只候选抓取市值...")
    caps, missing = [], []
    for tk, name in universe.items():
        mc = fetch_market_cap(tk)
        if mc is not None:
            caps.append((tk, name, mc))
        else:
            missing.append((tk, name))
    ranked = sorted(caps, key=lambda x: x[2], reverse=True)[:top_n]
    print(f"  [{label}] 取得市值 {len(caps)} 只, 选取前 {len(ranked)} 只。")
    if missing:
        # 不静默丢弃: 明确列出未取到市值 (代码错误/板别错误/Yahoo无数据) 的标的
        print(f"  [{label}] ⚠️  {len(missing)} 只未取到市值, 已排除: "
              + ", ".join(f"{tk}({name})" for tk, name in missing))
    return ranked


def build_pool(n_main=TOP_N_MAIN, n_otc=TOP_N_OTC):
    """
    构建最终候选宇宙: 主板(.TW)市值前 n_main + 上柜(.TWO)市值前 n_otc, 合并。
    返回 {代码: 名称} 字典 (约 n_main + n_otc 只)。
    """
    print("=" * 60)
    print(f"动态基础池: 主板前 {n_main} 大 + 上柜前 {n_otc} 大 合并...")
    print("=" * 60)
    main_ranked = _rank_universe(MAIN_UNIVERSE, n_main, "主板 .TW")
    otc_ranked = _rank_universe(OTC_UNIVERSE, n_otc, "上柜 .TWO")
    combined = main_ranked + otc_ranked
    print(f"最终候选宇宙: 主板 {len(main_ranked)} + 上柜 {len(otc_ranked)} = {len(combined)} 只。")
    return {tk: name for tk, name, _ in combined}


# ------------------------------------------------------------------
# 基准统计: ^TWII 上涨日/下跌日平均收益
# ------------------------------------------------------------------
def benchmark_stats(bench_close):
    """
    计算基准 ^TWII 在收益分析窗口内的上涨日/下跌日平均日收益。
    返回 dict(ret, avg_up, avg_down, up_count, dn_count, cumret)。
    ret 的 index 即后续个股对齐所用的交易日集合。
    """
    ret = bench_close.pct_change().dropna()
    if RETURN_WINDOW_TD and len(ret) > RETURN_WINDOW_TD:
        ret = ret.iloc[-RETURN_WINDOW_TD:]
    up = ret[ret > 0]
    dn = ret[ret < 0]
    return {
        "ret": ret,
        "avg_up": float(up.mean()) if len(up) else 0.0,
        "avg_down": float(dn.mean()) if len(dn) else 0.0,
        "up_count": int(len(up)),
        "dn_count": int(len(dn)),
        "cumret": float((1.0 + ret).prod() - 1.0),
    }


def evaluate_stock(close, bm):
    """
    对单只个股做纯动量三条件评估 (基准 bm = benchmark_stats 输出)。
    返回 (record, status): status 为 "" 表示成功; 非空为失败原因, record 为 None。
    record 含: 现价 price, ma50, above_ma50(C), avg_up, avg_down,
               beat_up(A), resist_down(B), passed(A&B&C)。
    """
    if close is None or close.empty:
        return None, "无价格数据"
    if len(close) < MA_WINDOW + 5:
        return None, f"价格序列过短({len(close)}<{MA_WINDOW + 5}), 无法算 50 日均线"

    # 条件 C: 站上 50 日均线
    ma = close.rolling(MA_WINDOW).mean()
    price = float(close.iloc[-1])
    ma50 = float(ma.iloc[-1])
    if pd.isna(ma50):
        return None, "50 日均线为 NaN (有效数据不足)"
    above = price > ma50

    # 条件 A/B: 在大盘上涨日/下跌日的平均日收益
    ret = close.pct_change().dropna()
    aligned = ret.reindex(bm["ret"].index).dropna()
    if len(aligned) < 20:
        return None, f"与基准重叠交易日不足({len(aligned)}<20)"
    bm_a = bm["ret"].reindex(aligned.index)

    up_days = aligned[bm_a > 0]
    dn_days = aligned[bm_a < 0]
    if len(up_days) == 0:
        return None, "窗口内无大盘上涨日"
    if len(dn_days) == 0:
        return None, "窗口内无大盘下跌日"
    avg_up = float(up_days.mean())
    avg_down = float(dn_days.mean())

    beat_up = avg_up > bm["avg_up"]                          # A: 领涨
    resist_down = avg_down > bm["avg_down"] * RESIST_FACTOR  # B: 抗跌 (跌幅负, 越大越抗跌)

    record = {
        "price": round(price, 2), "ma50": round(ma50, 2), "above_ma50": above,
        "avg_up": avg_up, "avg_down": avg_down,
        "beat_up": bool(beat_up), "resist_down": bool(resist_down),
        "passed": bool(above and beat_up and resist_down),
    }
    return record, ""


def download_one_close(tk):
    """
    单只标的下载收盘价, 带退避重试 (应对批量下载偶发限流/漏抓)。
    成功返回去 NaN 后的 Close Series; 全部失败返回 None。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.download(
                tk, period=DATA_LOOKBACK, interval="1d",
                auto_adjust=True, progress=False,
            )
            if df is not None and not df.empty:
                close = df["Close"]
                if hasattr(close, "squeeze"):
                    close = close.squeeze()
                close = close.dropna()
                if not close.empty:
                    return close
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"     [重试 {attempt}/{MAX_RETRIES}] {tk}: {type(e).__name__}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return None


def screen(pool):
    """下载数据并对给定基础池做纯动量三条件筛选, 返回 (DataFrame, 基准统计)。"""
    print("=" * 60)
    print(f"纯动量三条件扫描 ({DATA_LOOKBACK}, 收益窗口 ~{RETURN_WINDOW_TD} 交易日):")
    print(f"  A 领涨: 上涨日均涨 > 大盘  |  "
          f"B 抗跌: 下跌日均跌 < 大盘 × {RESIST_FACTOR:.0%}  |  "
          f"C 趋势: 站上 {MA_WINDOW} 日均线")
    print("=" * 60)

    tickers = list(pool.keys()) + [BENCHMARK]
    try:
        raw = yf.download(
            tickers, period=DATA_LOOKBACK, interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
        )
    except Exception as e:
        raise RuntimeError(f"yfinance 批量下载失败: {type(e).__name__}: {e}") from e
    if raw is None or len(raw) == 0:
        raise RuntimeError("yfinance 返回空数据 — 可能是网络问题或全部代码无效。")

    def get_close(tk):
        try:
            return raw[tk]["Close"].dropna()
        except Exception:
            try:
                return raw["Close"][tk].dropna()
            except Exception:
                return None

    # 基准数据是计算的前提 — 批量漏抓时逐只重试, 仍缺失才直接报错, 绝不静默继续
    bench_close = get_close(BENCHMARK)
    if bench_close is None or bench_close.empty:
        print(f"  ⚠️  基准 {BENCHMARK} 批量未抓到, 逐只重试中...")
        bench_close = download_one_close(BENCHMARK)
    if bench_close is None or bench_close.empty:
        raise RuntimeError(
            f"严重错误: 无法获取基准指数 {BENCHMARK} 的价格数据, "
            "无法计算任何相对强度。请检查网络或 yfinance 状态后重试。"
        )
    bm = benchmark_stats(bench_close)
    print(f"  基准 ^TWII: {len(bm['ret'])} 交易日 | 上涨 {bm['up_count']} 下跌 {bm['dn_count']} | "
          f"均涨 {bm['avg_up']:+.3%} 均跌 {bm['avg_down']:+.3%} | 累计 {bm['cumret']:+.2%}")
    print(f"  抗跌阈值: 下跌日均跌须 > {bm['avg_down'] * RESIST_FACTOR:+.3%} (大盘跌幅 × {RESIST_FACTOR:.0%})")
    print("-" * 60)

    rows = []
    failures = []   # 数据获取/计算失败的标的 (代码, 名称, 原因)
    retried = []    # 批量漏抓、经逐只重试补回的标的 (便于核对权重股)
    for tk, name in pool.items():
        close = get_close(tk)
        # 批量下载漏抓 (常见于权重股被限流) — 逐只重试补抓, 带固定延迟
        if close is None or close.empty:
            time.sleep(RETRY_PAUSE)
            close = download_one_close(tk)
            if close is not None and not close.empty:
                retried.append(f"{tk}({name})")
                print(f"  🔁 [补抓成功] {tk} {name}: 批量漏抓, 逐只重试已补回")
        record, status = evaluate_stock(close, bm)
        if record is None:
            print(f"  ⚠️  [数据/计算失败] {tk} {name}: {status}")
            failures.append((tk, name, status))
            continue

        record.update({"ticker": tk, "name": name})
        rows.append(record)
        flag = "✅" if record["passed"] else "❌"
        a = "✓" if record["beat_up"] else "✗"
        b = "✓" if record["resist_down"] else "✗"
        c = "✓" if record["above_ma50"] else "✗"
        print(f"  {flag} {tk} {name}: A{a} B{b} C{c} | "
              f"上涨日 {record['avg_up']:+.3%}, 下跌日 {record['avg_down']:+.3%}, "
              f"现价 {record['price']} / 均线 {record['ma50']}")

    # 失败汇总: 让数据问题一目了然, 而不是默默吞掉
    total = len(pool)
    ok = len(rows)
    print("-" * 60)
    print(f"扫描完成: 共 {total} 只, 成功 {ok} 只, 失败/不可用 {len(failures)} 只。")
    if retried:
        print(f"🔁 经逐只重试补回 {len(retried)} 只 (批量曾漏抓): " + ", ".join(retried))
    if failures:
        print("⚠️  以下标的数据获取或计算失败 (已排除在入选/观察之外):")
        for tk, name, reason in failures:
            print(f"     - {tk} {name}: {reason}")
        if len(failures) > total * 0.3:
            print("‼️  失败比例偏高 (>30%), 强烈建议检查网络/yfinance 后重新运行!")

    df = pd.DataFrame(rows)
    df.attrs["failures"] = failures
    df.attrs["bm"] = bm
    return df, bm


# ------------------------------------------------------------------
# 报告生成 (纯动量三条件)
# ------------------------------------------------------------------
def compute_watchlist(df, top_n=10):
    """
    观察名单: 满足条件 A(领涨) 且 C(站上均线), 但 B(抗跌) 不足的近似标的。
    按上涨日均涨幅从高到低排序, 取前 top_n。排除已全通过者。
    """
    if df.empty:
        return df
    cand = df[
        (df["above_ma50"] == True)
        & (df["beat_up"] == True)
        & (df["resist_down"] != True)
        & (df["passed"] != True)
    ].copy()
    if cand.empty:
        return cand
    return cand.sort_values("avg_up", ascending=False).head(top_n)


def build_report(df, bm, today_str, title="台股纯动量「领涨抗跌」强势股报告"):
    final = df[df["passed"] == True] if not df.empty else df
    watchlist = compute_watchlist(df)
    resist_threshold = bm["avg_down"] * RESIST_FACTOR

    lines = []
    A = lines.append

    A(f"# {title}")
    A("")
    A(f"> 生成日期: **{today_str}**　|　基准: **^TWII**　|　收益窗口: **~{RETURN_WINDOW_TD} 交易日**　|　基础池: **市值前 {len(df) + len(df.attrs.get('failures', []))} 大**")
    A("")
    A("> *「大盘涨时领涨、大盘跌时抗跌（跌幅不及一半），且站上 50 日均线」*")
    A("")
    A("---")
    A("")

    # 一、市场总览
    A("## 一、市场总览（基准：^TWII）")
    A("")
    A("| 指标 | 数值 |")
    A("|------|------|")
    A(f"| 窗口累计涨跌 | {bm['cumret']:+.2%} |")
    A(f"| 总交易日数 | {len(bm['ret'])} 天 |")
    A(f"| 上涨日 | {bm['up_count']} 天（均涨 {bm['avg_up']:+.3%}）|")
    A(f"| 下跌日 | {bm['dn_count']} 天（均跌 {bm['avg_down']:+.3%}）|")
    A("")
    A("---")
    A("")

    # 二、筛选框架
    A("## 二、筛选框架（纯技术面，三条件须同时满足）")
    A("")
    A("> - **条件 A（领涨）**：个股在 ^TWII 上涨日的平均涨幅 **>** ^TWII 平均涨幅")
    A(f"> - **条件 B（抗跌）**：个股在 ^TWII 下跌日的平均跌幅 **<** ^TWII 平均跌幅的 "
      f"**{RESIST_FACTOR:.0%}**（即跌幅须 > {resist_threshold:+.3%}，极其抗跌甚至逆势上涨）")
    A(f"> - **条件 C（趋势护栏）**：当前价格站上 **{MA_WINDOW} 日均线**")
    A("")
    A("---")
    A("")

    # 三、纯动量强势股
    A("## ⭐ 三、纯动量强势股（同时满足 A / B / C 三条件）")
    A("")
    A("> 大盘涨时涨得更多、大盘跌时几乎不跌，且站稳 50 日均线之上的稀有标的。")
    A("")
    A("| 代码 | 名称 | 现价 | 50日均线 | 高于均线 | 上涨日均涨幅 | 超额涨幅 | 下跌日均跌幅 | 抗跌比率 |")
    A("|------|------|----:|--------:|:-------:|:-----------:|:-------:|:-----------:|:-------:|")
    if not final.empty:
        for r in final.sort_values("avg_up", ascending=False).to_dict("records"):
            pct = r["price"] / r["ma50"] - 1 if r["ma50"] else 0
            excess = r["avg_up"] - bm["avg_up"]
            ratio = r["avg_down"] / bm["avg_down"] if bm["avg_down"] else 0
            A(f"| **{r['ticker']}** | {r['name']} | {r['price']} | {r['ma50']} | "
              f"+{pct:.1%} | {r['avg_up']:+.3%} | {excess:+.3%} | {r['avg_down']:+.3%} | "
              f"{ratio:.0%} |")
        A("")
        A(f"**共 {len(final)} 只标的同时满足三个条件。**")
    else:
        A("| _无_ | _本周无个股同时满足全部三个条件_ | | | | | | | |")
    A("")

    # 四、观察名单 (差抗跌一项)
    A("## 🔭 四、观察名单（满足 A 领涨 + C 站上均线，但 B 抗跌不足）")
    A("")
    A("| 排名 | 代码 | 名称 | 现价 | 上涨日均涨幅 | 下跌日均跌幅 |")
    A("|:----:|------|------|----:|:-----------:|:-----------:|")
    if not watchlist.empty:
        for i, r in enumerate(watchlist.to_dict("records"), 1):
            A(f"| {i} | {r['ticker']} | {r['name']} | {r['price']} | "
              f"{r['avg_up']:+.3%} | {r['avg_down']:+.3%} |")
    else:
        A("| - | _无_ | _无满足 A+C 的近似标的_ | | | |")
    A("")

    # 数据质量提示: 把抓取/计算失败的标的明确写进报告, 不静默
    failures = df.attrs.get("failures", []) if hasattr(df, "attrs") else []
    if failures:
        A("## ⚠️ 数据质量提示")
        A("")
        A(f"> 本次有 **{len(failures)}** 只标的数据获取或计算失败，已排除在入选/观察之外：")
        A("")
        A("| 股票代码 | 失败原因 |")
        A("|----------|----------|")
        for tk, name, reason in failures:
            A(f"| {tk} {name} | {reason} |")
        A("")
    return "\n".join(lines), final, watchlist


# ------------------------------------------------------------------
# 桌面路径检测 (兼容 OneDrive 重定向)
# ------------------------------------------------------------------
def get_desktop_dir():
    candidates = []
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        val, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        candidates.append(os.path.expandvars(val))
    except Exception:
        pass
    for env_var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, "Desktop"))
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates.append(os.path.join(profile, "OneDrive", "Desktop"))
    candidates.append(os.path.join(profile, "Desktop"))
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return os.getcwd()


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    today_str = datetime.now().strftime("%Y-%m-%d")

    pool = build_pool(TOP_N_MAIN, TOP_N_OTC)
    df, bm = screen(pool)
    report, final, watchlist = build_report(df, bm, today_str)

    desktop = get_desktop_dir()
    out_path = os.path.join(desktop, f"TWSE_Weekly_Report_{today_str}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[路径] 检测到桌面目录: {desktop}")

    print("\n" + "=" * 60)
    n = 0 if final is None or final.empty else len(final)
    if n > 0:
        print(f"⭐ 纯动量强势股 {n} 只 (A 领涨 + B 抗跌 + C 站上均线): " +
              ", ".join(f"{r['ticker']}({r['name']})" for r in final.to_dict("records")))
    else:
        print("⚠️  纯动量强势股 0 只 —— 无个股同时满足 领涨 + 抗跌 + 站上 50 日均线。")
    if watchlist is not None and not watchlist.empty:
        print("🔭 观察名单 (满足 A 领涨 + C 站上均线, 但 B 抗跌不足, 按上涨日均涨降序):")
        for r in watchlist.to_dict("records"):
            print(f"     {r['ticker']} {r['name']}: "
                  f"上涨日 {r['avg_up']:+.3%}, 下跌日 {r['avg_down']:+.3%}, "
                  f"现价 {r['price']} / 均线 {r['ma50']}")
    print("=" * 60)
    print(f"✅ 报告已生成: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
