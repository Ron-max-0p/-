import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np
import scipy.stats as si

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股期权军火库 (宗师版)", 
    layout="wide", 
    page_icon="🐉",
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
    .ratio-tag { background-color: #4b0082; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; }
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心引擎 ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0: return 0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == 'call': return si.norm.cdf(d1)
    else: return si.norm.cdf(d1) - 1.0

def process_chain(df, current_price, days_to_exp, type, risk_free_rate=0.045):
    T = days_to_exp / 365.0
    df['type'] = type
    df['delta'] = df.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, risk_free_rate, x['impliedVolatility'], type), axis=1)
    return df[(df['openInterest'] > 10) & (df['bid'] > 0)].copy()

@st.cache_data(ttl=300)
def fetch_market_data(ticker, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="6mo") 
        if history.empty: return None, 0, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        
        expirations = stock.options
        if not expirations: return None, current_price, history, "无期权链"

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if 14 <= days <= 60: valid_dates.append((date_str, days)) # 宗师策略通常做波段
        
        if not valid_dates: return None, current_price, history, "该时段无合适期权"

        all_opps = []
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = process_chain(opt.calls, current_price, days, 'call')
                puts = process_chain(opt.puts, current_price, days, 'put')

                if calls.empty or puts.empty: continue
                
                # === 宗师级策略构建 ===

                # 1. 玉蜥蜴 (Jade Lizard)
                # 结构：Sell OTM Put + Sell OTM Call Spread (Bear Call Spread)
                # 核心：收到的总权利金 > Call Spread 的宽度。这样上方就没有风险。
                if strat_code == 'JADE_LIZARD':
                    # A. 找卖 Put (Delta ~ -0.3)
                    short_puts = puts[(puts['delta'] > -0.35) & (puts['delta'] < -0.2)]
                    
                    # B. 找 Call Spread (卖近买远)
                    short_calls = calls[(calls['delta'] < 0.35) & (calls['delta'] > 0.2)]
                    
                    for _, p_row in short_puts.head(3).iterrows():
                        for _, c_short in short_calls.head(3).iterrows():
                            # 找 Long Call (保护)
                            target_long = c_short['strike'] + spread_width
                            c_longs = calls[abs(calls['strike'] - target_long) < 0.5]
                            
                            if not c_longs.empty:
                                c_long = c_longs.iloc[0]
                                
                                # 计算钱
                                credit_put = p_row['bid']
                                credit_call_spread = c_short['bid'] - c_long['ask']
                                total_credit = credit_put + credit_call_spread
                                
                                # 宗师级风控：无风险验证
                                # 如果总权利金 > 价差宽，说明哪怕暴涨穿了 Call，你也赚钱
                                upside_risk = spread_width - total_credit
                                
                                # 我们只筛选那些 "接近零风险" 或者 "完全零风险" 的
                                if upside_risk < 0.5: # 允许一点点风险，或者负数(完全无风险)
                                    risk_status = "🛡️ 上方无忧" if upside_risk <= 0 else f"⚠️ 上方微险 ${upside_risk*100:.0f}"
                                    
                                    all_opps.append({
                                        'expiration_date': date, 'days_to_exp': days,
                                        'desc': f"SELL PUT ${p_row['strike']} + SELL CALL ${c_short['strike']}/BUY ${c_long['strike']}",
                                        'price_display': total_credit,
                                        'capital': p_row['strike'] * 100 * 0.2, # 估算保证金
                                        'roi': total_credit * 100 / (p_row['strike'] * 100 * 0.2),
                                        'breakeven': f"下方 ${p_row['strike'] - total_credit:.2f}",
                                        'special_note': risk_status,
                                        'legs': [
                                            {'side': 'SELL', 'type': 'PUT', 'strike': p_row['strike']},
                                            {'side': 'SELL', 'type': 'CALL', 'strike': c_short['strike']},
                                            {'side': 'BUY', 'type': 'CALL', 'strike': c_long['strike']}
                                        ]
                                    })

                # 2. 比例价差 (Ratio Spread) - Call Front Ratio
                # 结构：Buy 1 ATM Call + Sell 2 OTM Calls
                # 核心：Net Credit (收钱开仓) 或 Zero Cost
                elif strat_code == 'RATIO_SPREAD':
                    # A. Buy 1 ATM Call (Delta ~ 0.6)
                    long_calls = calls[(calls['delta'] > 0.55) & (calls['delta'] < 0.65)]
                    
                    for _, l_row in long_calls.head(3).iterrows():
                        # B. Sell 2 OTM Calls (Delta ~ 0.3)
                        # 我们希望 2 * Short_Bid > 1 * Long_Ask
                        target_short_strike = l_row['strike'] + spread_width # 这里 spread_width 当作间距
                        short_candidates = calls[abs(calls['strike'] - target_short_strike) < 2.0] # 稍微放宽搜索
                        
                        if not short_candidates.empty:
                            s_row = short_candidates.iloc[0]
                            
                            net = (s_row['bid'] * 2) - l_row['ask']
                            
                            # 只找 收钱开仓 或者 极低成本 的
                            if net > -0.5: 
                                profit_peak = (s_row['strike'] - l_row['strike']) + net
                                
                                all_opps.append({
                                    'expiration_date': date, 'days_to_exp': days,
                                    'desc': f"BUY 1 CALL ${l_row['strike']} / SELL 2 CALLs ${s_row['strike']}",
                                    'price_display': net, # 正数代表收钱
                                    'capital': s_row['strike'] * 100 * 0.3, # 裸卖风险保证金估算
                                    'roi': profit_peak * 100 / (s_row['strike'] * 100 * 0.3), # 这是一个很虚的ROI
                                    'breakeven': f"上方 ${s_row['strike'] + profit_peak:.2f}",
                                    'special_note': "🔥 裸卖风险 (Unlimited Risk)",
                                    'legs': [
                                        {'side': 'BUY (x1)', 'type': 'CALL', 'strike': l_row['strike']},
                                        {'side': 'SELL (x2)', 'type': 'CALL', 'strike': s_row['strike']}
                                    ]
                                })

            except Exception as e: continue

        if not all_opps: return None, current_price, history, "未找到符合宗师级风控的套利机会"
        df = pd.DataFrame(all_opps)
        return df, current_price, history, None

    except Exception as e: return None, 0, None, f"API 错误: {str(e)}"

# --- 4. 界面渲染 ---

with st.sidebar:
    st.header("🐉 宗师级工场")
    
    strat_map = {
        "🦎 玉蜥蜴 (Jade Lizard - 无惧暴涨)": "JADE_LIZARD",
        "⚖️ 比例价差 (Ratio Spread - 空手套白狼)": "RATIO_SPREAD"
    }
    
    selected = st.selectbox("选择宗师策略", list(strat_map.keys()))
    strat_code = strat_map[selected]
    
    st.info("💡 **策略说明**：\n\n**玉蜥蜴**：稍微看涨/横盘。如果暴涨，因为你的权利金够厚，抵消了空头亏损。\n\n**比例价差**：买1卖2。如果温和上涨赚最多；如果跌了，白赚权利金；唯独怕暴涨。")
    
    spread_width = st.slider("结构宽度 / 间距", 2, 20, 5)
    
    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    if st.button("🚀 寻找套利机会", type="primary", use_container_width=True):
        st.cache_data.clear()

st.title(f"{ticker} 结构化套利终端 v14.0")

with st.spinner('正在进行多腿对冲计算...'):
    df, current_price, history, err = fetch_market_data(ticker, strat_code, spread_width, 0)

if err:
    st.error(err)
else:
    # 推荐逻辑
    if strat_code == 'JADE_LIZARD':
        # 找上方风险最小的 (upside_risk 越小越好，即 price_display 越大越好)
        best = df.sort_values('price_display', ascending=False).head(1)
    else:
        # 找收钱最多的 Ratio
        best = df.sort_values('price_display', ascending=False).head(1)

    if not best.empty:
        r = best.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("🏆 最佳套利结构")
            st.markdown(f"**合约时间**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            
            # 动态显示 Note
            if "无忧" in str(r['special_note']):
                st.success(r['special_note'])
            else:
                st.warning(r['special_note'])
            
            # 显示多腿
            for leg in r['legs']:
                color = "sell-leg" if "SELL" in leg['side'] else "buy-leg"
                st.markdown(f'<div class="trade-leg {color}">{leg["side"]} {leg["type"]} ${leg["strike"]}</div>', unsafe_allow_html=True)

        with c2:
            lbl = "净收入 (Credit)"
            val = r['price_display']
            st.metric(lbl, f"${val*100:.0f}")
            st.metric("估算保证金", f"${r['capital']:.0f}")
            st.metric("主要盈亏平衡点", r['breakeven'])

    # 画图 (简单版)
    fig = go.Figure(data=[go.Candlestick(x=history.index, open=history['Open'], high=history['High'], low=history['Low'], close=history['Close'], name=ticker)])
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")
    # 画主要腿
    if not best.empty:
        main_leg = r['legs'][0]['strike']
        fig.add_hline(y=main_leg, line_color="orange", annotation_text="核心行权价")
    
    fig.update_layout(height=350, template="plotly_dark", xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    with st.expander("📋 完整套利列表"):
        st.dataframe(df[['expiration_date', 'desc', 'price_display', 'special_note', 'breakeven']], use_container_width=True)
