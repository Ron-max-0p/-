import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (深度版)", 
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
            if min_days <= days_to_exp <= max_days:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, history, "选定范围内无到期日"

        all_opportunities = []
        
        # 计算价格筛选范围 (上下限)
        # 比如现价100，范围20%，则找 80 ~ 120 之间的所有期权
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                # --- 核心修改：放宽筛选逻辑 ---
                if strategy_type == 'CSP': 
                    # 以前是只找比现价低的，现在允许用户看全范围
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['credit'] = candidates['bid']
                    # 避免除以0
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    
                elif strategy_type == 'CC': 
                    candidates = calls[(calls['strike'] >= lower_bound) & (calls['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'SPREAD':
                    # Spread 策略逻辑复杂，还是只找 OTM (价外) 的比较合理，否则计算量太大
                    if strat_code == 'SPREAD':
                         # 默认只看现价以下的 Put (Bull Put Spread)
                         shorts = puts[puts['strike'] < current_price].copy()
                         # 也要符合范围限制
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
                    # 只过滤掉 bid 为 0 的垃圾数据
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
                      annotation_text=f"行权价 ${target_strike}", annotation_position="bottom right")
        if target_strike < current_price: 
            fig.add_hrect(y0=target_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
        else: 
            fig.add_hrect(y0=current_price, y1=target_strike, fillcolor="red", opacity=0.1, line_width=0)

    fig.update_layout(title=f"{ticker} K线图", height=400, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🏭 策略参数")
    
    # 策略选择
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
    if strat_code == 'SPREAD':
        spread_width = st.slider("价差宽度", 1, 20, 5)

    st.divider()
    
    # >>> 新增：行权价范围控制 <<<
    st.markdown("### 🔍 扫描深度")
    strike_range_pct = st.slider(
        "行权价覆盖范围 (±%)", 
        min_value=5, 
        max_value=30, 
        value=15, 
        step=5,
        help="数值越大，看到的行权价越多（包含深虚值和实值）。"
    )
    
    st.divider()
    
    preset_tickers = {"NVDA": "NVDA", "TSLA": "TSLA", "QQQ": "QQQ", "SPY": "SPY", "MSTR": "MSTR", "COIN": "COIN"}
    ticker_key = st.selectbox("选择标的", list(preset_tickers.keys()) + ["自定义..."])
    ticker = st.text_input("代码", value="AMD").upper() if ticker_key == "自定义..." else preset_tickers[ticker_key]
    
    c1, c2 = st.columns(2)
    min_dte = c1.number_input("最近", 14)
    max_dte = c2.number_input("最远", 45)
    
    if st.button("🚀 全面扫描", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"📊 {ticker} 深度数据")

with st.spinner('正在拉取全量数据...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, min_dte, max_dte, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 还是保留一个 AI 推荐，给不想看表的人
    df['score_val'] = df['distance_pct'] * 100
    if strat_code == 'SPREAD':
        best_pick = df[(df['score_val'] >= 3) & (df['score_val'] < 10)].sort_values('annualized_return', ascending=False).head(1)
    else:
        best_pick = df[(df['score_val'] >= 4) & (df['score_val'] < 10)].sort_values('annualized_return', ascending=False).head(1)
    
    target_strike_line = None
    if not best_pick.empty:
        target_strike_line = best_pick.iloc[0]['strike']

    if history is not None:
        render_chart(history, ticker, target_strike_line)

    if not best_pick.empty:
        r = best_pick.iloc[0]
        st.info(f"💡 **AI 参考**: 行权价 ${r['strike']} (年化 {r['annualized_return']:.1%})")

    st.divider()
    st.subheader(f"📋 完整期权链 (已加载 {len(df)} 条数据)")
    
    final_df = df.copy()
    if 'display_strike' in final_df.columns:
        final_df['strike'] = final_df['display_strike']

    # 完整表格配置
    st.dataframe(
        final_df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']],
        column_config={
            "expiration_date": st.column_config.DateColumn("到期日"),
            "strike": st.column_config.TextColumn("行权价"),
            "bid": st.column_config.NumberColumn("权利金", format="$%.2f"),
            "distance_pct": st.column_config.ProgressColumn(
                "安全垫 (Distance)", 
                format="%.2f%%", 
                min_value=-0.2, # 允许显示负数（价内）
                max_value=0.2
            ),
            "annualized_return": st.column_config.NumberColumn("年化收益", format="%.2f%%"),
        },
        use_container_width=True,
        hide_index=True,
        height=800 # 增加高度，看更多行
    )
