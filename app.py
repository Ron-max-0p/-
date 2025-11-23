import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (纯净版)", 
    layout="wide", 
    page_icon="🏭",
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
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strategy_type, spread_width=5):
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
            if min_days <= days_to_exp <= max_days:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, "选定范围内无到期日"

        all_opportunities = []
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                if strategy_type == 'CSP': 
                    candidates = puts[puts['strike'] < current_price * 1.05].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'CC': 
                    candidates = calls[calls['strike'] > current_price * 0.95].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'SPREAD':
                    shorts = puts[puts['strike'] < current_price].copy()
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
                    candidates = candidates[candidates['bid'] > 0.01] 
                    
                    # >>> 删除了胜率估算，只保留确定性数学计算 <<<
                    
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
    """画K线图和安全线"""
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'],
                high=history_df['High'],
                low=history_df['Low'],
                close=history_df['Close'],
                name=ticker)])

    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", annotation_text="现价", annotation_position="top right", line_color="gray")

    if target_strike:
        fig.add_hline(y=target_strike, line_dash="dash", line_color="red", 
                      annotation_text=f"行权价 ${target_strike}", annotation_position="bottom right")
        if target_strike < current_price: 
            fig.add_hrect(y0=target_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
        else: 
            fig.add_hrect(y0=current_price, y1=target_strike, fillcolor="red", opacity=0.1, line_width=0)

    fig.update_layout(
        title=f"{ticker} 走势与安全垫可视化",
        height=400,
        margin=dict(l=20, r=20, t=40, b=20),
        xaxis_rangeslider_visible=False,
        template="plotly_dark"
    )
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🏭 策略军火库")
    cat_map = {
        "🔰 入门收租 (单腿)": ["CSP (现金担保Put)", "CC (持股备兑Call)"],
        "🚀 进阶杠杆 (垂直价差)": ["Bull Put Spread (牛市看跌价差)"]
    }
    category = st.selectbox("选择策略等级", list(cat_map.keys()))
    strategy_name = st.selectbox("选择具体策略", cat_map[category])
    
    if "CSP" in strategy_name: strat_code = 'CSP'
    elif "CC" in strategy_name: strat_code = 'CC'
    else: strat_code = 'SPREAD'
    
    spread_width = 5
    if strat_code == 'SPREAD':
        spread_width = st.slider("价差宽度", 1, 20, 5)

    st.divider()
    preset_tickers = {"NVDA": "NVDA", "TSLA": "TSLA", "QQQ": "QQQ", "SPY": "SPY", "MSTR": "MSTR", "COIN": "COIN"}
    ticker_key = st.selectbox("选择标的", list(preset_tickers.keys()) + ["自定义..."])
    ticker = st.text_input("代码", value="AMD").upper() if ticker_key == "自定义..." else preset_tickers[ticker_key]
    
    st.divider()
    if st.button("🚀 扫描机会", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"📊 {ticker} 策略可视化")

# 说明书
expander_title = "📖 策略说明书 (点击展开)"
help_text = ""
if strat_code == 'CSP':
    help_text = "### 🟢 Cash-Secured Put\n我是土豪，我有钱。如果跌到行权价，我愿意全款买入股票。"
elif strat_code == 'CC':
    help_text = "### 🔴 Covered Call\n我有股票。如果涨到行权价，我愿意卖出股票止盈。"
else:
    help_text = f"### 🚀 Bull Put Spread\n用小资金收租。卖出一个贵的Put，买入一个便宜的Put做保护。最大亏损锁定为 ${spread_width*100}。"

with st.expander(expander_title):
    st.markdown(help_text)

with st.spinner('正在获取数据并绘图...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 14, 45, strat_code, spread_width)

if error_msg:
    st.error(error_msg)
else:
    df['score_val'] = df['distance_pct'] * 100
    if strat_code == 'SPREAD':
        rec_col = 'annualized_return'
        best_pick = df[(df['score_val'] >= 3) & (df['score_val'] < 10)].sort_values(rec_col, ascending=False).head(1)
    else:
        best_pick = df[(df['score_val'] >= 4) & (df['score_val'] < 10)].sort_values('annualized_return', ascending=False).head(1)
    
    target_strike_line = None
    if not best_pick.empty:
        target_strike_line = best_pick.iloc[0]['strike']

    if history is not None:
        render_chart(history, ticker, target_strike_line)

    if not best_pick.empty:
        r = best_pick.iloc[0]
        # 删除了胜率显示，只保留硬数据
        st.success(f"🤖 **AI 推荐**: 行权价 **${r['strike']}** | 年化收益 **{r['annualized_return']:.1%}** | 安全垫 **{r['distance_pct']:.1%}**")

    st.divider()
    st.subheader("📋 机会列表 (纯净数据)")
    
    final_df = df.copy()
    if 'display_strike' in final_df.columns:
        final_df['strike'] = final_df['display_strike']

    # 表格里也删除了 win_rate 列
    st.dataframe(
        final_df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']],
        column_config={
            "expiration_date": st.column_config.DateColumn("到期日"),
            "strike": st.column_config.TextColumn("行权价"),
            "bid": st.column_config.NumberColumn("权利金", format="$%.2f"),
            "distance_pct": st.column_config.ProgressColumn("安全垫", format="%.2f%%", min_value=-0.1, max_value=0.2),
            "annualized_return": st.column_config.NumberColumn("年化收益", format="%.2f%%"),
        },
        use_container_width=True,
        hide_index=True
    )
