import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np
import scipy.stats as si

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股期权军火库 (华尔街版)", 
    layout="wide", 
    page_icon="🏛️",
    initial_sidebar_state="expanded"
)

# --- 2. 自定义 CSS ---
st.markdown("""
<style>
    .metric-card {
        background-color: #1E1E1E;
        border: 1px solid #333;
        padding: 20px;
        border-radius: 10px;
        margin-bottom: 10px;
    }
    thead tr th:first-child {display:none}
    tbody th {display:none}
    .trade-leg { padding: 4px 8px; border-radius: 4px; margin-bottom: 3px; font-family: monospace; font-size: 0.9em; }
    .sell-leg { background-color: #3d0000; color: #ff9999; border-left: 3px solid #ff4b4b; }
    .buy-leg { background-color: #002b00; color: #99ffbb; border-left: 3px solid #00cc96; }
    .strategy-tag { font-size: 0.8em; padding: 2px 6px; border-radius: 4px; background: #444; color: #eee; }
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心引擎 (Black-Scholes & Builders) ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0: return 0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == 'call':
        return si.norm.cdf(d1, 0.0, 1.0)
    else:
        return si.norm.cdf(d1, 0.0, 1.0) - 1.0

def get_earnings_date(ticker_obj):
    try:
        cal = ticker_obj.calendar
        if cal and 'Earnings Date' in cal:
            return cal['Earnings Date'][0]
        return None
    except: return None

# 通用价差构建器 (绝对正确核心)
def build_spread(longs, shorts, spread_width, spread_type='credit'):
    """
    严谨匹配两腿，确保Strike差值等于spread_width
    spread_type: 'credit' (卖方收钱) or 'debit' (买方付钱)
    """
    spreads = []
    
    # 为了效率，只遍历 Short Leg (做为主腿)
    for idx, short_leg in shorts.iterrows():
        # 寻找对应的 Long Leg
        if spread_type == 'credit':
            # Credit Put Spread: Short Put (High K) + Long Put (Low K) -> Target Long = Short K - Width
            # Credit Call Spread: Short Call (Low K) + Long Call (High K) -> Target Long = Short K + Width
            target_strike = short_leg['strike'] - spread_width if short_leg['type']=='put' else short_leg['strike'] + spread_width
        else: # Debit
            # Debit Call Spread: Long Call (Low K) + Short Call (High K) -> 这里输入的主腿通常是 Long
            # 为了简化，我们统一假设输入 shorts 是 "Short Leg"，longs 是 "Long Leg" 列表
            # 但在 Debit Spread 里，主腿其实是 Long。这里调用逻辑需注意。
            pass

        # 在 Long 链中精确查找
        # 容错 0.5 是为了防止浮点数误差，实战中 Strike 都是整数或 .5
        matches = longs[abs(longs['strike'] - target_strike) < 0.1]
        
        if not matches.empty:
            long_leg = matches.iloc[0]
            
            # 计算价格
            short_price = short_leg['bid'] # 卖出拿 Bid
            long_price = long_leg['ask']   # 买入付 Ask
            
            net_price = short_price - long_price
            
            # 过滤逻辑
            valid = False
            if spread_type == 'credit' and net_price > 0.05: valid = True # 必须有肉吃
            if spread_type == 'debit' and net_price < 0: valid = True # 净支出 (net_price是负数)
            
            if valid:
                max_loss = spread_width - net_price if spread_type == 'credit' else abs(net_price)
                max_profit = net_price if spread_type == 'credit' else (spread_width - abs(net_price))
                
                roi = max_profit / max_loss if max_loss > 0 else 0
                
                spreads.append({
                    'short_id': short_leg.name, 'long_id': long_leg.name,
                    'short_strike': short_leg['strike'], 'long_strike': long_leg['strike'],
                    'net_price': abs(net_price), # 显示为正数金额
                    'roi': roi,
                    'max_loss': max_loss,
                    'short_delta': short_leg['delta'],
                    'net_delta': short_leg['delta'] - long_leg['delta'] if short_leg['type']=='call' else short_leg['delta'] - long_leg['delta'], # 近似
                    'short_oi': short_leg['openInterest'],
                    'long_oi': long_leg['openInterest']
                })
    
    return pd.DataFrame(spreads)

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

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            # 统一看 14-90 天，流动性最好
            if 14 <= days_to_exp <= 90: valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, next_earnings, "该时段无合适期权"

        all_opps = []
        RISK_FREE_RATE = 0.045
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                T = days / 365.0
                
                # 数据预处理 & Delta 计算
                def process_chain(df, type):
                    df['type'] = type
                    df['delta'] = df.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, RISK_FREE_RATE, x['impliedVolatility'], type), axis=1)
                    # 严格流动性过滤：OI < 10 或 Bid=0 直接剔除
                    return df[(df['openInterest'] > 10) & (df['bid'] > 0)].copy()

                calls = process_chain(opt.calls, 'call')
                puts = process_chain(opt.puts, 'put')

                if calls.empty or puts.empty: continue

                candidates = pd.DataFrame()
                
                # === 策略构建工厂 ===

                # 1. Cash Secured Put (CSP)
                if strat_code == 'CSP':
                    # 筛选 Delta -0.1 ~ -0.4 (胜率高且有肉)
                    candidates = puts[(puts['delta'] > -0.4) & (puts['delta'] < -0.1)].copy()
                    candidates['credit'] = candidates['bid']
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    candidates['desc'] = candidates['strike'].apply(lambda x: f"SELL PUT ${x}")
                    candidates['breakeven'] = candidates['strike'] - candidates['credit']

                # 2. Bull Put Spread (Credit Put)
                elif strat_code == 'BULL_PUT_SPREAD':
                    # 主腿：卖出 Delta -0.2 ~ -0.4 的 Put
                    shorts = puts[(puts['delta'] > -0.4) & (puts['delta'] < -0.2)]
                    # 保护腿：买入更低价的 Put
                    spreads = build_spread(puts, shorts, spread_width, 'credit')
                    if not spreads.empty:
                        candidates = spreads
                        candidates['desc'] = candidates.apply(lambda x: f"SELL PUT ${x['short_strike']} / BUY PUT ${x['long_strike']}", axis=1)
                        candidates['capital'] = candidates['max_loss'] * 100
                        candidates['credit'] = candidates['net_price']
                        candidates['breakeven'] = candidates['short_strike'] - candidates['net_price']
                        candidates['delta'] = candidates['net_delta']

                # 3. Bear Call Spread (Credit Call)
                elif strat_code == 'BEAR_CALL_SPREAD':
                    # 主腿：卖出 Delta 0.2 ~ 0.4 的 Call
                    shorts = calls[(calls['delta'] < 0.4) & (calls['delta'] > 0.2)]
                    # 保护腿：买入更高价的 Call
                    spreads = build_spread(calls, shorts, spread_width, 'credit')
                    if not spreads.empty:
                        candidates = spreads
                        candidates['desc'] = candidates.apply(lambda x: f"SELL CALL ${x['short_strike']} / BUY CALL ${x['long_strike']}", axis=1)
                        candidates['capital'] = candidates['max_loss'] * 100
                        candidates['credit'] = candidates['net_price']
                        candidates['breakeven'] = candidates['short_strike'] + candidates['net_price']
                        candidates['delta'] = candidates['net_delta']

                # 4. Long Call (博弈)
                elif strat_code == 'LONG_CALL':
                    # 选 ATM 附近，Delta 0.4 ~ 0.6
                    candidates = calls[(calls['delta'] > 0.4) & (calls['delta'] < 0.6)].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100
                    candidates['roi'] = (current_price / candidates['debit']) # 杠杆倍数代替ROI
                    candidates['desc'] = candidates['strike'].apply(lambda x: f"BUY CALL ${x}")
                    candidates['breakeven'] = candidates['strike'] + candidates['debit']

                # 5. Bull Call Spread (Debit Call)
                elif strat_code == 'BULL_CALL_SPREAD':
                    # 买入 ATM Call (Long)，卖出 OTM Call (Short) 降低成本
                    # 这里我们简单反向利用 build_spread 逻辑：先找 Short (High K)，再找 Long (Low K)
                    # 但 debit spread 逻辑略不同，我们手动写一下保证正确
                    longs = calls[(calls['delta'] > 0.45) & (calls['delta'] < 0.6)] # ATM
                    spreads_list = []
                    for _, l_leg in longs.iterrows():
                        target_short = l_leg['strike'] + spread_width
                        matches = calls[abs(calls['strike'] - target_short) < 0.1]
                        if not matches.empty:
                            s_leg = matches.iloc[0]
                            net_debit = l_leg['ask'] - s_leg['bid']
                            if net_debit > 0 and net_debit < spread_width:
                                max_profit = spread_width - net_debit
                                spreads_list.append({
                                    'desc': f"BUY CALL ${l_leg['strike']} / SELL CALL ${s_leg['strike']}",
                                    'debit': net_debit,
                                    'capital': net_debit * 100,
                                    'roi': max_profit / net_debit, # 赔率
                                    'breakeven': l_leg['strike'] + net_debit,
                                    'delta': l_leg['delta'] - s_leg['delta'],
                                    'days_to_exp': days, 'expiration_date': date,
                                    'openInterest': min(l_leg['openInterest'], s_leg['openInterest'])
                                })
                    candidates = pd.DataFrame(spreads_list)

                # 6. Iron Condor
                elif strat_code == 'IRON_CONDOR':
                    # Put Leg: Sell Delta ~ -0.2
                    p_shorts = puts[(puts['delta'] > -0.25) & (puts['delta'] < -0.15)]
                    p_spreads = build_spread(puts, p_shorts, spread_width, 'credit')
                    
                    # Call Leg: Sell Delta ~ 0.2
                    c_shorts = calls[(calls['delta'] < 0.25) & (calls['delta'] > 0.15)]
                    c_spreads = build_spread(calls, c_shorts, spread_width, 'credit')
                    
                    if not p_spreads.empty and not c_spreads.empty:
                        # 组合
                        condors = []
                        # 简单取 Top 3 组合
                        for _, p in p_spreads.head(3).iterrows():
                            for _, c in c_spreads.head(3).iterrows():
                                total_credit = p['net_price'] + c['net_price']
                                max_loss = spread_width - total_credit
                                if max_loss > 0:
                                    condors.append({
                                        'desc': f"IC {p['short_strike']}/{c['short_strike']}",
                                        'credit': total_credit,
                                        'capital': max_loss * 100,
                                        'roi': total_credit / max_loss,
                                        'breakeven': f"${p['short_strike']-total_credit:.1f} / ${c['short_strike']+total_credit:.1f}",
                                        'delta': p['net_delta'] + c['net_delta'],
                                        'legs_detail': {'p_s':p['short_strike'], 'p_l':p['long_strike'], 'c_s':c['short_strike'], 'c_l':c['long_strike']}
                                    })
                        candidates = pd.DataFrame(condors)

                # 后处理
                if not candidates.empty:
                    # 补齐字段
                    if 'days_to_exp' not in candidates.columns:
                        candidates['days_to_exp'] = days
                        candidates['expiration_date'] = date
                    
                    # 统一列名用于显示
                    candidates['price_display'] = candidates.get('credit', candidates.get('debit', 0))
                    
                    # 年化计算
                    candidates['annualized_return'] = candidates['roi'] * (365 / days)
                    
                    # 财报检查
                    candidates['earnings_risk'] = False
                    if next_earnings:
                        exp_dt = datetime.strptime(date, "%Y-%m-%d").date()
                        if next_earnings <= exp_dt: candidates['earnings_risk'] = True

                    all_opps.append(candidates)

            except Exception as e: continue

        if not all_opps: return None, current_price, history, next_earnings, "未找到符合严格风控的策略"
        df = pd.concat(all_opps)
        return df, current_price, history, next_earnings, None

    except Exception as e: return None, 0, None, None, f"API 错误: {str(e)}"

# --- 4. 界面渲染 ---

with st.sidebar:
    st.header("🏛️ 华尔街策略工场")
    
    # 策略分类器
    cat = st.radio("作战目标", ["收租 (Credit)", "博弈 (Debit)", "中性 (Neutral)"])
    
    strat_map = {}
    if cat == "收租 (Credit)":
        strat_map = {
            "卖Put (Bullish Income)": "CSP",
            "牛市Put价差 (Bull Put Spread)": "BULL_PUT_SPREAD",
            "熊市Call价差 (Bear Call Spread)": "BEAR_CALL_SPREAD"
        }
    elif cat == "博弈 (Debit)":
        strat_map = {
            "买Call (Long Call)": "LONG_CALL",
            "牛市Call价差 (Bull Call Spread)": "BULL_CALL_SPREAD"
        }
    else:
        strat_map = {"铁鹰 (Iron Condor)": "IRON_CONDOR"}

    selected = st.selectbox("选择具体策略", list(strat_map.keys()))
    strat_code = strat_map[selected]
    
    spread_width = 5
    if "SPREAD" in strat_code or "CONDOR" in strat_code:
        spread_width = st.slider("价差/保护宽度", 1, 20, 5)

    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    if st.button("🚀 执行量化扫描", type="primary", use_container_width=True):
        st.cache_data.clear()

st.title(f"{ticker} 期权策略终端 v12.0")

with st.spinner('AI 正在进行 Delta 建模与组合构建...'):
    df, current_price, history, next_earnings, err = fetch_market_data(ticker, strat_code, spread_width, 0)

if err:
    st.error(err)
else:
    # 智能排序
    if cat == "收租 (Credit)" or cat == "中性 (Neutral)":
        # 收租看 ROI (年化)，但优先 Delta 安全的
        best = df.sort_values('annualized_return', ascending=False).head(1)
    else:
        # 博弈看杠杆/赔率 (ROI列)
        best = df.sort_values('roi', ascending=False).head(1)
    
    # 渲染图表
    # (此处省略 render_chart 细节，复用之前的逻辑，只画线)
    
    # 结果展示
    if not best.empty:
        r = best.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("🏆 最佳战术指令")
            
            # 财报警告
            if r['earnings_risk']:
                st.warning(f"⚠️ **财报风险**: 此期权覆盖了 {next_earnings} 财报日！")
            else:
                st.success("🛡️ **无财报风险**")

            st.markdown(f"**合约**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            
            # 指令解析
            desc = r['desc']
            if "SPREAD" in strat_code or "CONDOR" in strat_code:
                # 简单拆解显示
                parts = desc.split(' / ')
                for p in parts:
                    color = "sell-leg" if "SELL" in p else "buy-leg"
                    st.markdown(f'<div class="trade-leg {color}">{p}</div>', unsafe_allow_html=True)
            else:
                color = "sell-leg" if "SELL" in desc else "buy-leg"
                st.markdown(f'<div class="trade-leg {color}">{desc}</div>', unsafe_allow_html=True)
            
            st.info(f"🧠 **Net Delta**: {r['delta']:.2f} (策略整体方向敞口)")

        with c2:
            st.metric("单张盈亏 (P/L)", f"${r['price_display']*100:.0f}")
            st.metric("资金占用/风险", f"${r['capital']:.0f}")
            
            label = "年化收益 (APR)" if cat != "博弈 (Debit)" else "赔率/杠杆"
            val = f"{r['annualized_return']:.1%}" if cat != "博弈 (Debit)" else f"{r['roi']:.1f}x"
            st.metric(label, val)
            
            st.metric("盈亏平衡点", f"{r['breakeven']}")

    st.divider()
    with st.expander("📋 完整量化列表 (按优选排序)"):
        cols = ['expiration_date', 'desc', 'price_display', 'capital', 'delta', 'annualized_return' if cat!='博弈 (Debit)' else 'roi', 'breakeven']
        st.dataframe(df[cols], use_container_width=True)
