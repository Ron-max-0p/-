import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np
import scipy.stats as si # 引入科学计算库，用于计算 Black-Scholes

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股期权军火库 (量化版)", 
    layout="wide", 
    page_icon="🧠",
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
    /* 风险标签 */
    .risk-badge { padding: 2px 6px; border-radius: 4px; font-size: 0.8em; font-weight: bold; }
    .risk-high { background-color: #ff4b4b; color: white; }
    .risk-safe { background-color: #00cc96; color: black; }
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心区 (Black-Scholes & Greeks) ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    """
    S: 标的价格
    K: 行权价
    T: 剩余年化时间 (Days/365)
    r: 无风险利率 (取 0.045)
    sigma: 隐含波动率 (IV)
    """
    if T <= 0 or sigma <= 0: return 0
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    
    if option_type == 'call':
        delta = si.norm.cdf(d1, 0.0, 1.0)
    else:
        delta = si.norm.cdf(d1, 0.0, 1.0) - 1.0
        
    return delta

def get_earnings_date(ticker_obj):
    """获取下一次财报日期"""
    try:
        # yfinance 的 calendar 有时会返回空，做个容错
        cal = ticker_obj.calendar
        if cal and 'Earnings Date' in cal:
            return cal['Earnings Date'][0] # 返回最近的一个日期
        return None
    except:
        return None

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="6mo") 
        if history.empty: return None, 0, None, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        
        # 获取财报日
        next_earnings = get_earnings_date(stock)
        
        expirations = stock.options
        if not expirations: return None, current_price, history, next_earnings, "无期权链数据"

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            
            # 策略日期筛选
            if "LEAPS" in strat_code or "LONG_PUT" in strat_code:
                if days_to_exp > 90: valid_dates.append((date_str, days_to_exp))
            else:
                if 0 <= days_to_exp <= 60: valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, next_earnings, "无期权链"

        all_opportunities = []
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        # 风险参数
        RISK_FREE_RATE = 0.045 # 4.5% 近期美债收益率
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                T = days / 365.0 # 年化时间

                # --- 策略逻辑 ---
                # A. 收租区
                if strat_code == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['credit'] = candidates['bid']
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL PUT ${x}")
                    candidates['breakeven'] = candidates['strike'] - candidates['credit']
                    # 计算 Delta
                    candidates['delta'] = candidates.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, RISK_FREE_RATE, x['impliedVolatility'], 'put'), axis=1)

                # B. 博弈区
                elif strat_code == 'LONG_CALL': 
                    candidates = calls[(calls['strike'] >= current_price) & (calls['strike'] <= upper_bound)].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100 
                    candidates['leverage'] = (current_price / candidates['debit']) * 0.5 
                    candidates['roi'] = candidates['leverage'] 
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY CALL ${x}")
                    candidates['breakeven'] = candidates['strike'] + candidates['debit']
                    candidates['delta'] = candidates.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, RISK_FREE_RATE, x['impliedVolatility'], 'call'), axis=1)

                elif strat_code == 'LONG_PUT':
                    candidates = puts[(puts['strike'] <= current_price) & (puts['strike'] >= lower_bound)].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100
                    candidates['leverage'] = (current_price / candidates['debit']) * 0.5
                    candidates['roi'] = candidates['leverage']
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY PUT ${x}")
                    candidates['breakeven'] = candidates['strike'] - candidates['debit']
                    candidates['delta'] = candidates.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, RISK_FREE_RATE, x['impliedVolatility'], 'put'), axis=1)

                # C. 长期投资
                elif strat_code == 'LEAPS_CALL': 
                    deep_itm_strike = current_price * 0.75 
                    candidates = calls[calls['strike'] <= deep_itm_strike].copy()
                    candidates['debit'] = candidates['ask']
                    candidates['capital'] = candidates['debit'] * 100
                    candidates['breakeven'] = candidates['strike'] + candidates['debit']
                    candidates['roi'] = (current_price / candidates['breakeven']) - 1 
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"BUY LEAPS CALL ${x}")
                    candidates['delta'] = candidates.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, RISK_FREE_RATE, x['impliedVolatility'], 'call'), axis=1)
                
                # 简单处理 Spread 类策略 (只取 Short Leg 的 Delta 近似)
                else: 
                     candidates = pd.DataFrame() # 暂时略过复杂策略展示，聚焦核心功能的绝对正确性

                # 通用数据清洗
                if not candidates.empty:
                    candidates['days_to_exp'] = days
                    candidates['expiration_date'] = date
                    price_col = 'ask' if 'LONG' in strat_code or 'LEAPS' in strat_code else 'bid'
                    
                    # >>> 绝对正确：流动性过滤 <<<
                    # 必须有成交量(Volume)或者持仓量(openInterest)，且有人出价
                    candidates = candidates[
                        (candidates[price_col] > 0) & 
                        ((candidates['openInterest'] > 10) | (candidates['volume'] > 5)) # 至少得有点活气
                    ] 
                    
                    if 'annualized_return' not in candidates.columns:
                        candidates['annualized_return'] = 0 
                    else:
                        candidates['annualized_return'] = candidates['roi'] * (365 / days)
                    
                    # >>> 绝对正确：财报风险标记 <<<
                    candidates['has_earnings_risk'] = False
                    if next_earnings:
                        # 如果财报日在 到期日 之前，说明期权包含财报风险
                        exp_dt = datetime.strptime(date, "%Y-%m-%d").date()
                        if next_earnings <= exp_dt:
                            candidates['has_earnings_risk'] = True

                    all_opportunities.append(candidates)
            except Exception: continue

        if not all_opportunities: return None, current_price, history, next_earnings, "无符合流动性标准的期权"
        df = pd.concat(all_opportunities)
        return df, current_price, history, next_earnings, None

    except Exception as e: return None, 0, None, None, f"API 错误: {str(e)}"

def render_chart(history_df, ticker, r, strat_code):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")

    strike = r['strike'] if 'strike' in r else 0
    try:
        if isinstance(strike, str): strike_val = float(strike.split(' ')[-1].replace('$',''))
        else: strike_val = strike
    except: strike_val = current_price

    color = "green" if "CALL" in strat_code else "red"
    fig.add_hline(y=strike_val, line_color=color, annotation_text="行权价")
    fig.update_layout(title=f"{ticker} 走势图", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🧠 量化指挥部")
    
    zone = st.radio("作战目的", ["💰 现金流区", "🎰 博弈区", "📈 长期看涨", "📉 长期看跌"])
    st.divider()
    
    strat_map = {}
    if "现金流" in zone: strat_map = {"卖Put收租 (CSP)": "CSP"} # 简化演示核心量化功能
    elif "博弈" in zone: strat_map = {"买Call (Long Call)": "LONG_CALL", "买Put (Long Put)": "LONG_PUT"}
    elif "看涨" in zone: strat_map = {"LEAPS Call": "LEAPS_CALL"}
    else: strat_map = {"Put 对冲": "LONG_PUT"}

    selected_strat_label = st.selectbox("战术", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    strike_range_pct = st.slider("行权价范围", 5, 50, 20)
    
    if st.button("🚀 启动量化引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"{zone.split(' ')[0]} {ticker} 量化分析终端")

with st.spinner('正在进行 Black-Scholes 建模与流动性过滤...'):
    df, current_price, history, next_earnings, error_msg = fetch_market_data(ticker, 0, 0, strat_code, 0, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 推荐逻辑：使用 Delta 进行科学排序
    if "现金流" in zone:
        # 收租最爱：Delta 绝对值在 0.2-0.3 之间 (既有肉吃又相对安全)
        # 先过滤掉太危险的，再按回报率排
        safe_pool = df[abs(df['delta']) < 0.4]
        if not safe_pool.empty:
            best_pick = safe_pool.sort_values('annualized_return', ascending=False).head(1)
        else:
            best_pick = df.sort_values('annualized_return', ascending=False).head(1)
    elif "博弈" in zone:
        # 博弈最爱：Delta 0.5 左右 (平值附近，爆发力强)
        df['delta_dist'] = abs(abs(df['delta']) - 0.5)
        best_pick = df.sort_values('delta_dist').head(1)
    else:
        best_pick = df.head(1)

    # 财报提醒
    if next_earnings:
        days_to_earnings = (next_earnings - datetime.now().date()).days
        if days_to_earnings <= 45:
             st.warning(f"⚠️ **财报警报**：{ticker} 预计在 **{next_earnings}** ({days_to_earnings}天后) 发布财报。请注意波动率风险！")

    if history is not None and not best_pick.empty:
        render_chart(history, ticker, best_pick.iloc[0], strat_code)

    st.subheader("🛠️ 量化指令单")
    
    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.markdown(f"**合约**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            
            # 财报风险标
            earnings_tag = ""
            if r['has_earnings_risk']:
                earnings_tag = " <span class='risk-badge risk-high'>⚡ 包含财报日</span>"
            else:
                earnings_tag = " <span class='risk-badge risk-safe'>🛡️ 无财报风险</span>"
            
            st.markdown(f"**风险属性**: {earnings_tag}", unsafe_allow_html=True)
            
            # 腿部展示
            color_class = "sell-leg" if "SELL" in r['leg_desc'] else "buy-leg"
            st.markdown(f'<div class="trade-leg {color_class}">{r["leg_desc"]} (Delta: {r["delta"]:.2f})</div>', unsafe_allow_html=True)
            
            st.info(f"🧠 **AI 解析**: 该合约的 Delta 为 **{r['delta']:.2f}**。这意味着市场定价认为它有 **{abs(r['delta'])*100:.1f}%** 的概率在到期时变成实值。")

        with c2:
            price_display = r['debit'] if 'debit' in r else r.get('credit', 0)
            
            st.success(f"""
            **💰 核心数据**
            * **价格**: ${price_display*100:.0f}
            * **Delta**: {r['delta']:.2f}
            * **持仓量 (OI)**: {r['openInterest']}
            * **流动性**: {"✅ 优" if r['openInterest']>100 else "⚠️ 一般"}
            """)
            
    st.divider()
    with st.expander("📋 完整量化数据列表 (含 Delta & OI)"):
        # 格式化显示
        display_df = df.copy()
        display_df['impliedVolatility'] = display_df['impliedVolatility'].apply(lambda x: f"{x:.1%}")
        display_df['delta'] = display_df['delta'].apply(lambda x: f"{x:.2f}")
        
        cols = ['expiration_date', 'strike', 'leg_desc', 'delta', 'openInterest', 'impliedVolatility']
        if 'annualized_return' in display_df.columns: cols.append('annualized_return')
        
        st.dataframe(display_df[cols], use_container_width=True, hide_index=True)
