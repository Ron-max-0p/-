import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="蟹黄包子铺", 
    layout="wide", 
    page_icon="🛡️", # 图标换成了盾牌
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
    /* 强调核查区域 */
    .stCheckbox {
        background-color: #262730;
        padding: 10px;
        border-radius: 5px;
        margin-bottom: 5px;
    }
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strategy_type, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="3mo") 
        if history.empty: return None, 0, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        
        expirations = stock.options
        if not expirations: return None, current_price, history, "无期权链数据"

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            if 0 <= days_to_exp <= 180:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, "选定范围内无到期日"

        all_opportunities = []
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                if strategy_type == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    
                elif strategy_type == 'CC': 
                    candidates = calls[(calls['strike'] >= lower_bound) & (calls['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'SPREAD':
                    if strat_code == 'SPREAD':
                         shorts = puts[puts['strike'] < current_price].copy()
                         shorts = shorts[shorts['strike'] >= lower_bound]
                    
                    spreads = []
                    for index, short_row in shorts.iterrows():
                        target_long_strike = short_row['strike'] - spread_width
                        long_candidates = puts[abs(puts['strike'] - target_long_strike) < 0.5]
                        if not long_candidates.empty:
                            long_row = long_candidates.iloc[0]
                            net_credit = short_row['bid'] - long_row['ask']
                            if net_credit > 0.01:
                                max_loss = spread_width - net_credit
                                spread_data = {
                                    'strike': short_row['strike'],
                                    'display_strike': f"{short_row['strike']} / {long_row['strike']}",
                                    'bid': net_credit,
                                    'distance_pct': (current_price - short_row['strike']) / current_price,
                                    'capital': max_loss * 100,
                                    'roi': net_credit / max_loss
                                }
                                spreads.append(spread_data)
                    if spreads: candidates = pd.DataFrame(spreads)
                    else: continue

                if not candidates.empty:
                    candidates['days_to_exp'] = days
                    candidates['expiration_date'] = date
                    candidates = candidates[candidates['bid'] > 0] 
                    candidates['annualized_return'] = candidates['roi'] * (365 / days)
                    all_opportunities.append(candidates)
                    
            except Exception:
                continue

        if not all_opportunities: return None, current_price, history, "没有找到符合条件的合约"

        df = pd.concat(all_opportunities)
        return df, current_price, history, None

    except Exception as e:
        return None, 0, None, f"API 错误: {str(e)}"

def render_chart(history_df, ticker, target_strike=None):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", annotation_text="现价", annotation_position="top right", line_color="gray")
    if target_strike:
        fig.add_hline(y=target_strike, line_dash="dash", line_color="red", 
                      annotation_text=f"推荐 ${target_strike}", annotation_position="bottom right")
        if target_strike < current_price: 
            fig.add_hrect(y0=target_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
        else: 
            fig.add_hrect(y0=current_price, y1=target_strike, fillcolor="red", opacity=0.1, line_width=0)
    fig.update_layout(title=f"{ticker} K线图", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("风控")
    cat_map = {
        "🔰 入门收租 (单腿)": ["CSP (现金担保Put)", "CC (持股备兑Call)"],
        "🚀 进阶杠杆 (垂直价差)": ["Bull Put Spread (牛市看跌价差)"]
    }
    category = st.selectbox("策略类型", list(cat_map.keys()))
    strategy_name = st.selectbox("具体策略", cat_map[category])
    if "CSP" in strategy_name: strat_code = 'CSP'
    elif "CC" in strategy_name: strat_code = 'CC'
    else: strat_code = 'SPREAD'
    
    spread_width = 5
    if strat_code == 'SPREAD': spread_width = st.slider("价差宽度", 1, 20, 5)

    st.divider()
    preset_tickers = {"NVDA": "NVDA", "TSLA": "TSLA", "QQQ": "QQQ", "SPY": "SPY", "MSTR": "MSTR", "COIN": "COIN"}
    ticker_key = st.selectbox("选择标的", list(preset_tickers.keys()) + ["自定义..."])
    ticker = st.text_input("代码", value="AMD").upper() if ticker_key == "自定义..." else preset_tickers[ticker_key]
    
    strike_range_pct = st.slider("行权价扫描范围 (±%)", 10, 40, 20)
    
    if st.button("🚀 寻找实战机会", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"🛡️ {ticker} 实战风控终端")

with st.spinner('AI 正在扫描并执行风控检查...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 0, 180, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 筛选逻辑
    df['score_val'] = df['distance_pct'] * 100
    if strat_code == 'SPREAD':
        safe_df = df[(df['score_val'] >= 2)]
    else:
        safe_df = df[(df['score_val'] >= 5)]
    
    # 选出黄金月度作为首选
    mid_term = safe_df[(safe_df['days_to_exp'] > 14) & (safe_df['days_to_exp'] <= 45)].sort_values('annualized_return', ascending=False).head(1)

    target_strike_line = None
    if not mid_term.empty:
        target_strike_line = mid_term.iloc[0]['strike']

    # 1. K线图
    if history is not None:
        render_chart(history, ticker, target_strike_line)

    # 2. 核心：带风控的推荐卡片
    st.subheader("👮‍♂️ 交易前核查 (Pre-Trade Checklist)")
    
    if not mid_term.empty:
        row = mid_term.iloc[0]
        
        # 使用两列布局：左边是推荐，右边是检查表
        c1, c2 = st.columns([1, 1.5])
        
        with c1:
            st.info(f"""
            **🏆 系统推荐 (黄金月度)**
            
            📅 **到期**: {row['expiration_date']} (剩{row['days_to_exp']}天)
            🎯 **行权**: ${row['strike']}
            💰 **参考权利金**: ${row['bid']*100:.0f}
            🛡️ **安全垫**: {row['distance_pct']:.1%}
            🚀 **年化**: {row['annualized_return']:.1%}
            """)
        
        with c2:
            st.warning("⚠️ 必须完成以下核查，才可执行交易！")
            
            check1 = st.checkbox(f"1. 已在券商确认 **${ticker}** 实时股价 ({current_price:.2f}) 无巨大偏差")
            check2 = st.checkbox(f"2. 已确认该合约 **Delta 绝对值 < 0.3** (胜率较高)")
            check3 = st.checkbox(f"3. 已确认 **{row['expiration_date']}** 之前无财报发布")
            
            if check1 and check2 and check3:
                st.success(f"""
                ✅ **风控通过！建议执行方案：**
                
                👉 打开券商 App
                👉 搜索期权链: **{row['expiration_date']}**
                👉 选择 Strike: **{row['strike']}**
                👉 **Limit Order (限价单)** 挂在 **${row['bid']:.2f}** 附近
                """)
            else:
                st.markdown("🚨 *请逐项勾选上方检查项以解锁交易建议*")

    else:
        st.error("当前筛选条件下，未找到足够安全的“黄金月度”机会。建议调整侧边栏的扫描范围。")

    # 3. 详细数据表
    st.divider()
    with st.expander("查看所有原始数据 (点击展开)"):
        final_df = df.copy()
        if 'display_strike' in final_df.columns:
            final_df['strike'] = final_df['display_strike']

        st.dataframe(
            final_df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']],
            column_config={
                "expiration_date": st.column_config.DateColumn("日期"),
                "strike": st.column_config.TextColumn("行权价"),
                "bid": st.column_config.NumberColumn("权利金", format="$%.2f"),
                "distance_pct": st.column_config.ProgressColumn("安全垫", format="%.2f%%", min_value=-0.2, max_value=0.2),
                "annualized_return": st.column_config.NumberColumn("年化", format="%.2f%%"),
            },
            use_container_width=True,
            hide_index=True
        )

