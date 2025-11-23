import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np
import scipy.stats as si

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股期权军火库 (跨时空版)", 
    layout="wide", 
    page_icon="🌌",
    initial_sidebar_state="expanded"
)

# --- 2. 自定义 CSS ---
st.markdown("""
<style>
    .metric-card { background-color: #1E1E1E; border: 1px solid #333; padding: 20px; border-radius: 10px; margin-bottom: 10px; }
    thead tr th:first-child {display:none}
    tbody th {display:none}
    .trade-leg { padding: 4px 8px; border-radius: 4px; margin-bottom: 3px; font-family: monospace; font-size: 0.9em; }
    .sell-leg { background-color: #3d0000; color: #ff9999; border-left: 3px solid #ff4b4b; }
    .buy-leg { background-color: #002b00; color: #99ffbb; border-left: 3px solid #00cc96; }
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心引擎 ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0: return 0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == 'call': return si.norm.cdf(d1)
    else: return si.norm.cdf(d1) - 1.0

def get_earnings_date(ticker_obj):
    try:
        cal = ticker_obj.calendar
        if cal and 'Earnings Date' in cal: return cal['Earnings Date'][0]
        return None
    except: return None

# 通用数据处理与 Delta 计算
def process_chain(df, current_price, days_to_exp, type, risk_free_rate=0.045):
    T = days_to_exp / 365.0
    df['type'] = type
    df['delta'] = df.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, risk_free_rate, x['impliedVolatility'], type), axis=1)
    # 严格流动性过滤
    return df[(df['openInterest'] > 10) & (df['bid'] > 0)].copy()

@st.cache_data(ttl=300)
def fetch_market_data(ticker, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="6mo") 
        if history.empty: return None, 0, None, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        next_earnings = get_earnings_date(stock)
        
        expirations = stock.options
        if not expirations: return None, current_price, history, next_earnings, "无期权链"

        # 日期预处理
        today = datetime.now().date()
        date_map = [] # [(date_str, days)]
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if days >= 7: date_map.append((date_str, days))

        all_opps = []
        RISK_FREE_RATE = 0.045
        
        # === 跨期策略逻辑 (PMCC / Calendar) ===
        if strat_code in ['PMCC', 'CALENDAR']:
            # 1. 确定远期腿 (Long Leg)
            # PMCC 找 > 150天, Calendar 找 > 60天
            min_far_days = 150 if strat_code == 'PMCC' else 60
            far_dates = [d for d in date_map if d[1] > min_far_days]
            near_dates = [d for d in date_map if 20 <= d[1] <= 45]
            
            if not far_dates or not near_dates: return None, current_price, history, next_earnings, "无合适的跨期日期组合"
            
            # 为了速度，只取第一个合适的远期和近期
            far_date, far_days = far_dates[0]
            near_date, near_days = near_dates[0]
            
            # 拉取两条链
            opt_far = stock.option_chain(far_date)
            opt_near = stock.option_chain(near_date)
            
            calls_far = process_chain(opt_far.calls, current_price, far_days, 'call')
            calls_near = process_chain(opt_near.calls, current_price, near_days, 'call')
            
            # --- 构建 PMCC (穷人盖楼) ---
            if strat_code == 'PMCC':
                # Long Leg: Deep ITM Call (Delta > 0.80) 替代正股
                long_candidates = calls_far[calls_far['delta'] > 0.80]
                # Short Leg: OTM Call (Delta ~ 0.30) 收租
                short_candidates = calls_near[(calls_near['delta'] < 0.40) & (calls_near['delta'] > 0.20)]
                
                for _, l_row in long_candidates.iterrows():
                    # 匹配逻辑：Short Strike 必须 > Long Strike (防止倒挂)
                    valid_shorts = short_candidates[short_candidates['strike'] > l_row['strike']]
                    
                    for _, s_row in valid_shorts.iterrows():
                        # PMCC 黄金法则：(Short Strike - Long Strike) + Net Credit > 0
                        # 也就是说：即使暴涨，价差盈利也要能覆盖掉你的借记成本
                        width = s_row['strike'] - l_row['strike']
                        debit = l_row['ask'] - s_row['bid'] # 净支出
                        
                        # 只有当总成本 < 宽度时，才是无风险死角的 PMCC
                        # 但实际上为了更容易成交，通常只要 debit < width * 0.9 即可
                        if debit < width: 
                            max_profit = width - debit + s_row['bid'] # 估算
                            roi = (width - debit) / debit # 这是一个保守估算
                            
                            all_opps.append({
                                'type': 'PMCC',
                                'expiration_date': f"Near: {near_date} / Far: {far_date}",
                                'days_to_exp': near_days, # 以近期为准
                                'desc': f"BUY LEAPS ${l_row['strike']} ({far_date}) / SELL CALL ${s_row['strike']} ({near_date})",
                                'capital': debit * 100,
                                'price_display': debit,
                                'delta': l_row['delta'] - s_row['delta'],
                                'roi': roi, # 这里显示为最大潜在回报
                                'annualized_return': 0, # 复杂策略不以此排序
                                'breakeven': f"${l_row['strike'] + debit:.2f}"
                            })

            # --- 构建 Calendar Spread (日历) ---
            elif strat_code == 'CALENDAR':
                # 找 ATM (平值) 附近的 Call
                atm_strikes = calls_near[abs(calls_near['delta'] - 0.5) < 0.1]['strike']
                
                for k in atm_strikes:
                    # 找同价的 Far Call
                    far_match = calls_far[calls_far['strike'] == k]
                    near_match = calls_near[calls_near['strike'] == k]
                    
                    if not far_match.empty and not near_match.empty:
                        l_row = far_match.iloc[0]
                        s_row = near_match.iloc[0]
                        
                        debit = l_row['ask'] - s_row['bid']
                        if debit > 0:
                            all_opps.append({
                                'type': 'Calendar',
                                'expiration_date': f"Short: {near_date} / Long: {far_date}",
                                'days_to_exp': near_days,
                                'desc': f"SELL CALL ${k} ({near_date}) / BUY CALL ${k} ({far_date})",
                                'capital': debit * 100,
                                'price_display': debit,
                                'delta': l_row['delta'] - s_row['delta'], # 应该是中性的
                                'roi': 0, # 日历策略很难算确切 ROI
                                'annualized_return': 0,
                                'breakeven': "依赖波动率"
                            })

        # === 同期策略逻辑 (之前的逻辑) ===
        else: 
            # 遍历单个日期
            for date, days in date_map:
                if days < 14 or days > 60: continue # 标准收租周期
                try:
                    opt = stock.option_chain(date)
                    calls = process_chain(opt.calls, current_price, days, 'call')
                    puts = process_chain(opt.puts, current_price, days, 'put')
                    
                    if strat_code == 'STRADDLE':
                        # Long Straddle: Buy ATM Call + Buy ATM Put
                        # 找 Delta 最接近 0.5 的
                        atm_call = calls.iloc[(calls['delta'] - 0.5).abs().argsort()[:1]]
                        atm_put = puts.iloc[(puts['delta'].abs() - 0.5).abs().argsort()[:1]]
                        
                        if not atm_call.empty and not atm_put.empty:
                            c = atm_call.iloc[0]
                            p = atm_put.iloc[0]
                            # 必须 strike 相同
                            if c['strike'] == p['strike']:
                                debit = c['ask'] + p['ask']
                                all_opps.append({
                                    'expiration_date': date, 'days_to_exp': days,
                                    'desc': f"BUY CALL ${c['strike']} / BUY PUT ${p['strike']}",
                                    'capital': debit * 100,
                                    'price_display': debit,
                                    'delta': c['delta'] + p['delta'],
                                    'roi': 0, # 博弈类
                                    'annualized_return': 0,
                                    'breakeven': f"${c['strike']-debit:.1f} / ${c['strike']+debit:.1f}"
                                })

                    # ... (保留之前的 CSP 等逻辑，为了篇幅简略，核心逻辑与 v12 一致) ...
                    # 为了完整性，这里简单加上 CSP 以便演示
                    elif strat_code == 'CSP':
                        df = puts[(puts['delta'] > -0.3) & (puts['delta'] < -0.15)]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days,
                                'desc': f"SELL PUT ${r['strike']}",
                                'capital': r['strike'] * 100,
                                'price_display': r['bid'],
                                'delta': r['delta'],
                                'roi': r['bid'] / r['strike'],
                                'annualized_return': (r['bid'] / r['strike']) * (365/days),
                                'breakeven': f"${r['strike'] - r['bid']:.2f}"
                            })

                except: continue

        if not all_opps: return None, current_price, history, next_earnings, "未扫描到符合严苛条件的策略"
        df = pd.DataFrame(all_opps)
        return df, current_price, history, next_earnings, None

    except Exception as e: return None, 0, None, None, f"API 错误: {str(e)}"

# --- 4. 界面渲染 ---

with st.sidebar:
    st.header("🌌 跨时空战舰")
    
    cat = st.radio("战术维度", ["单一时间 (Standard)", "跨期套利 (Time Spreads)", "波动率博弈 (Volatility)"])
    
    strat_map = {}
    if cat == "单一时间 (Standard)":
        strat_map = {"卖Put收租 (CSP)": "CSP"} # 简化显示，可按需加回 Spread
    elif cat == "跨期套利 (Time Spreads)":
        strat_map = {
            "穷人盖楼 (PMCC - Diagonal)": "PMCC",
            "日历价差 (Calendar Spread)": "CALENDAR"
        }
    else:
        strat_map = {
            "双买爆破 (Long Straddle)": "STRADDLE"
        }

    selected = st.selectbox("选择策略", list(strat_map.keys()))
    strat_code = strat_map[selected]
    
    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    if st.button("🚀 启动引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

st.title(f"{ticker} 期权终端 v13.0")

with st.spinner('正在进行多维期权链匹配...'):
    df, current_price, history, next_earnings, err = fetch_market_data(ticker, strat_code, 5, 20)

if err:
    st.error(err)
else:
    # 推荐逻辑
    if strat_code == 'PMCC':
        best = df.sort_values('roi', ascending=False).head(1)
    elif strat_code == 'STRADDLE':
        best = df.head(1) # Straddle 通常就一个 ATM 最优
    else:
        best = df.sort_values('annualized_return', ascending=False).head(1)

    if not best.empty:
        r = best.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("🏆 最佳战术指令")
            
            # 财报检查
            earnings_alert = ""
            if next_earnings:
                earnings_alert = f" (注意：下一次财报 {next_earnings})"

            st.markdown(f"**合约时间**: {r['expiration_date']}{earnings_alert}")
            
            # 指令拆解
            desc = r['desc']
            parts = desc.split(' / ')
            for p in parts:
                color = "sell-leg" if "SELL" in p else "buy-leg"
                st.markdown(f'<div class="trade-leg {color}">{p}</div>', unsafe_allow_html=True)
            
            if strat_code == 'PMCC':
                st.info("💡 **PMCC 原理**：你买入的 LEAPS Call (远期) 就像“虚构的正股”。你卖出的近端 Call 是在收租。只要股价缓慢上涨，你就能享受正股涨幅+租金双重收益。")
            elif strat_code == 'STRADDLE':
                st.info("💡 **双买原理**：不在乎方向，只在乎幅度。只要 NVDA 暴涨或暴跌超过盈亏平衡点，你就赚钱。")

        with c2:
            lbl = "净支出 (Debit)" if strat_code in ['PMCC', 'CALENDAR', 'STRADDLE'] else "净收入 (Credit)"
            st.metric(lbl, f"${r['price_display']*100:.0f}")
            st.metric("最大资金占用", f"${r['capital']:.0f}")
            st.metric("盈亏平衡点", r['breakeven'])

    st.divider()
    with st.expander("📋 完整数据"):
        st.dataframe(df, use_container_width=True)
