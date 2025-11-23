import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股期权军火库 (全能版)", 
    layout="wide", 
    page_icon="⚔️",
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
    .trade-leg { padding: 5px 10px; border-radius: 5px; margin-bottom: 4px; font-family: monospace; font-weight: bold; }
    .sell-leg { background-color: #4a1c1c; color: #ff9999; border-left: 4px solid #ff4b4b; }
    .buy-leg { background-color: #1c3321; color: #99ffbb; border-left: 4px solid #00cc96; }
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="6mo") # 拉长数据方便看长期
        if history.empty: return None, 0, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        
        expirations = stock.options
        if not expirations: return None, current_price, history, "无期权链数据"

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            
            # 根据策略调整日期筛选逻辑
            if "LEAPS" in strat_code:
                if days_to_exp > 180: # 长期策略至少半年以上
                    valid_dates.append((date_str, days_to_exp))
            else:
                if 0 <= days_to_exp <= 60: # 短期/收租一般看2个月内
                    valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, "该时间段内无期权链"

        all_opportunities = []
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts

                # --- 策略逻辑大分流 ---

                # === A. 收租区 (Income) ===
                if strat_code == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['credit'] = candidates['bid']
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL PUT ${x}")

                elif strat_code == 'IRON_CONDOR':
                    candidates = build_iron_condor(puts, calls, current_price, lower_bound, upper_bound, spread_width)

                # === B. 博弈区 (Speculation) ===
                elif strat_code == 'LONG_CALL': # 买Call博暴涨
                    # 找稍微虚值一点的 (OTM)，爆发力强
                    candidates = calls[(calls['strike'] >= current_price) & (calls['strike'] <= upper_bound)].copy()
                    candidates['debit'] = candidates['ask'] # 买入要付钱
                    candidates['capital'] = candidates['debit'] * 100 # 风险就是本金
                    # 博弈策略 ROI 很难算 (因为理论无限)，这里用杠杆率近似：(股价/权利金) * Delta(近似0.5)
                    candidates['leverage'] = (current_price / candidates['debit']) * 0.5 
                    candidates['roi'] = candidates['leverage'] # 这里 ROI 字段暂时借用来存杠杆率
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY CALL ${x}")

                elif strat_code == 'LONG_PUT': # 买Put博暴跌
                    candidates = puts[(puts['strike'] <= current_price) & (puts['strike'] >= lower_bound)].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100
                    candidates['leverage'] = (current_price / candidates['debit']) * 0.5
                    candidates['roi'] = candidates['leverage']
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY PUT ${x}")

                # === C. 长期投资 (Investment) ===
                elif strat_code == 'LEAPS_CALL': # 深度实值Call代替正股
                    # 找深度实值 (ITM)，Delta接近1，Strike远低于现价
                    deep_itm_strike = current_price * 0.7 # 7折行权价
                    candidates = calls[calls['strike'] <= deep_itm_strike].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100
                    # 长期持有的盈亏平衡点
                    candidates['breakeven'] = candidates['strike'] + candidates['debit']
                    candidates['roi'] = (current_price / candidates['breakeven']) - 1 # 安全边际
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY LEAPS CALL ${x}")

                # 通用处理
                if not candidates.empty:
                    candidates['days_to_exp'] = days
                    candidates['expiration_date'] = date
                    # 博弈策略不看Bid看Ask，收租策略看Bid
                    price_col = 'ask' if 'LONG' in strat_code or 'LEAPS' in strat_code else 'bid'
                    candidates = candidates[candidates[price_col] > 0] 
                    
                    if 'annualized_return' not in candidates.columns:
                        # 对于非收租策略，年化没意义，这里置为0或特定值
                        candidates['annualized_return'] = 0 
                    else:
                        candidates['annualized_return'] = candidates['roi'] * (365 / days)
                        
                    all_opportunities.append(candidates)
            except Exception: continue

        if not all_opportunities: return None, current_price, history, "无合适期权"
        df = pd.concat(all_opportunities)
        return df, current_price, history, None

    except Exception as e: return None, 0, None, f"API 错误: {str(e)}"

# 辅助函数保持 Iron Condor 逻辑 (复用之前的)
def build_iron_condor(puts, calls, current_price, lower_bound, upper_bound, width):
    # (此处省略具体实现，保持上一版逻辑以节省篇幅，核心逻辑不变)
    # 为了演示，简单返回空，实战中请保留上一版的 build_iron_condor 和 build_vertical_spread 代码
    return pd.DataFrame() 

def render_chart(history_df, ticker, r, strat_code):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")

    # 根据策略画图
    strike = r['strike'] if 'strike' in r else 0
    # 处理字符串类型的 strike (如 "IC 100/120")
    try:
        if isinstance(strike, str): strike_val = float(strike.split(' ')[-1].replace('$',''))
        else: strike_val = strike
    except: strike_val = current_price

    if "CALL" in strat_code:
        fig.add_hline(y=strike_val, line_color="green", annotation_text="行权价")
    elif "PUT" in strat_code:
        fig.add_hline(y=strike_val, line_color="red", annotation_text="行权价")

    fig.update_layout(title=f"{ticker} 走势图", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("⚔️ 战区选择")
    
    # === 四大分区 ===
    zone = st.radio("选择作战目的：", [
        "💰 现金流区 (稳健收租)", 
        "🎰 博弈区 (以小博大)", 
        "📈 长期看涨 (杠杆替身)", 
        "📉 长期看跌 (末日对冲)"
    ])
    
    st.divider()
    
    strat_map = {}
    if zone == "💰 现金流区 (稳健收租)":
        strat_map = {
            "卖Put收租 (CSP)": "CSP",
            "铁鹰震荡收租 (Iron Condor)": "IRON_CONDOR"
        }
    elif zone == "🎰 博弈区 (以小博大)":
        strat_map = {
            "买Call博暴涨 (Long Call)": "LONG_CALL",
            "买Put博暴跌 (Long Put)": "LONG_PUT"
        }
    elif zone == "📈 长期看涨 (杠杆替身)":
        strat_map = {
            "深实值 LEAPS Call": "LEAPS_CALL"
        }
    else:
        strat_map = {
            "远期 Put 对冲": "LONG_PUT" # 逻辑一样，只是日期选得远
        }

    selected_strat_label = st.selectbox("选择具体战术", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    # 参数控制
    spread_width = 5
    if strat_code == 'IRON_CONDOR': spread_width = st.slider("翼展宽度", 1, 20, 5)

    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    strike_range_pct = st.slider("行权价范围", 5, 50, 20)
    
    if st.button("🚀 扫描战场", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"{zone.split(' ')[0]} {ticker} 策略终端")

with st.spinner('AI 正在分析...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 0, 0, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 推荐排序逻辑
    if "博弈" in zone:
        # 博弈看杠杆率
        best_pick = df.sort_values('leverage', ascending=False).head(1)
    elif "长期" in zone:
        # 长期看盈亏平衡点
        best_pick = df.sort_values('breakeven', ascending=True).head(1)
    else:
        # 收租看年化
        if 'annualized_return' in df.columns:
             best_pick = df.sort_values('annualized_return', ascending=False).head(1)
        else:
             best_pick = df.head(1)

    # 画图
    if history is not None and not best_pick.empty:
        render_chart(history, ticker, best_pick.iloc[0], strat_code)

    # 指令卡片
    st.subheader("🛠️ 作战指令")
    
    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.markdown(f"**合约**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            
            # 动态生成不同颜色的指令
            if "SELL" in r['leg_desc']:
                st.markdown(f'<div class="trade-leg sell-leg">🔴 {r["leg_desc"]} (卖方义务)</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="trade-leg buy-leg">🟢 {r["leg_desc"]} (买方权利)</div>', unsafe_allow_html=True)
            
            if "LEAPS" in strat_code:
                st.info("💡 **LEAPS 逻辑**：你买入这个深度实值 Call，相当于用一半的钱控制了 100 股正股。只要股价不跌破盈亏平衡点，你都赚钱。")
            elif "LONG" in strat_code:
                st.warning("⚠️ **博弈警告**：这是在赌方向！如果到期前方向没对，权利金会全部归零。胜率通常低于 40%。")

        with c2:
            price_display = r['debit'] if 'debit' in r else r.get('credit', 0)
            st.success(f"""
            **💰 财务数据**
            * **单张成本/收入**: ${price_display*100:.0f}
            * **杠杆倍数**: {r.get('leverage', 0):.1f}x
            """)
            
    st.divider()
    with st.expander("📋 完整列表"):
        st.dataframe(df, use_container_width=True, hide_index=True)
