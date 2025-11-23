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
    page_icon="🦅", # 图标换成了鹰
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
def fetch_market_data(ticker, min_days, max_days, strat_code, spread_width, strike_range_pct):
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
        # 扩大扫描范围以支持宽跨式
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                # --- 策略分支 ---
                
                # 1. 单腿策略
                if strat_code == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    
                elif strat_code == 'CC': 
                    candidates = calls[(calls['strike'] >= lower_bound) & (calls['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                
                # 2. 垂直价差 (Bull Put Spread / Bear Call Spread)
                elif strat_code == 'BULL_PUT':
                    shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    candidates = build_vertical_spread(shorts, puts, spread_width, current_price, 'put')
                    
                elif strat_code == 'BEAR_CALL':
                    shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    candidates = build_vertical_spread(shorts, calls, spread_width, current_price, 'call')

                # 3. 铁鹰 (Iron Condor)
                elif strat_code == 'IRON_CONDOR':
                    # 这是一个组合策略：Bull Put Spread + Bear Call Spread
                    # 为了简化计算，我们寻找行权价距离现价百分比相近的组合
                    
                    # 找 Put 端 (下方)
                    put_shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    put_spreads = build_vertical_spread(put_shorts, puts, spread_width, current_price, 'put')
                    
                    # 找 Call 端 (上方)
                    call_shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    call_spreads = build_vertical_spread(call_shorts, calls, spread_width, current_price, 'call')
                    
                    if put_spreads.empty or call_spreads.empty: continue

                    condors = []
                    # 简单匹配：距离现价距离差不多的配对 (比如下方 5% 和 上方 5%)
                    # 为了性能，我们只取 Top 10 最优 Put 组合去匹配 Call
                    top_puts = put_spreads.sort_values('roi', ascending=False).head(10)
                    
                    for _, p_row in top_puts.iterrows():
                        # 找距离相当的 Call
                        target_dist = abs(p_row['distance_pct'])
                        # 容差 2%
                        matching_calls = call_spreads[abs(call_spreads['distance_pct'] - target_dist) < 0.02]
                        
                        for _, c_row in matching_calls.iterrows():
                            total_credit = p_row['bid'] + c_row['bid']
                            # 铁鹰保证金 = 单边最大亏损 (通常是价差宽 - 权利金)
                            # 因为股价不可能同时跌穿下方又涨穿上方
                            max_loss = spread_width - total_credit
                            
                            if max_loss > 0:
                                condor_data = {
                                    'strike': f"P{p_row['strike_val']} / C{c_row['strike_val']}", # 显示关键 Short Strike
                                    'put_strike': p_row['strike_val'],
                                    'call_strike': c_row['strike_val'],
                                    'bid': total_credit,
                                    'distance_pct': min(abs(p_row['distance_pct']), abs(c_row['distance_pct'])), # 取最近一边的安全垫
                                    'capital': max_loss * 100,
                                    'roi': total_credit / max_loss
                                }
                                condors.append(condor_data)
                    
                    if condors: candidates = pd.DataFrame(condors)
                    else: candidates = pd.DataFrame()

                # --- 统一收尾 ---
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

def build_vertical_spread(shorts, options_chain, width, current_price, type='put'):
    spreads = []
    for index, short_row in shorts.iterrows():
        if type == 'put':
            target_long = short_row['strike'] - width
            longs = options_chain[abs(options_chain['strike'] - target_long) < 0.5]
            dist = (current_price - short_row['strike']) / current_price
        else:
            target_long = short_row['strike'] + width
            longs = options_chain[abs(options_chain['strike'] - target_long) < 0.5]
            dist = (short_row['strike'] - current_price) / current_price
            
        if not longs.empty:
            long_row = longs.iloc[0]
            net_credit = short_row['bid'] - long_row['ask']
            if net_credit > 0.01:
                max_loss = width - net_credit
                spreads.append({
                    'strike': f"{short_row['strike']} / {long_row['strike']}",
                    'strike_val': short_row['strike'], # 存数值用于计算
                    'bid': net_credit,
                    'distance_pct': dist,
                    'capital': max_loss * 100,
                    'roi': net_credit / max_loss
                })
    return pd.DataFrame(spreads)

def render_chart(history_df, ticker, lower_strike=None, upper_strike=None):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")

    # 铁鹰画图逻辑：画上下两条线，中间涂色
    if lower_strike and upper_strike:
        fig.add_hline(y=upper_strike, line_color="red", line_dash="dash", annotation_text=f"Call墙 ${upper_strike}")
        fig.add_hline(y=lower_strike, line_color="green", line_dash="dash", annotation_text=f"Put墙 ${lower_strike}", annotation_position="bottom right")
        # 填充中间盈利区
        fig.add_hrect(y0=lower_strike, y1=upper_strike, fillcolor="green", opacity=0.1, line_width=0)
    
    # 单边逻辑
    elif lower_strike:
        fig.add_hline(y=lower_strike, line_color="green", line_dash="dash", annotation_text=f"行权价 ${lower_strike}")
        fig.add_hrect(y0=lower_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
    elif upper_strike:
        fig.add_hline(y=upper_strike, line_color="red", line_dash="dash", annotation_text=f"行权价 ${upper_strike}")
        fig.add_hrect(y0=current_price, y1=upper_strike, fillcolor="red", opacity=0.1, line_width=0)

    fig.update_layout(title=f"{ticker} 策略可视化 (盈利区间)", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🦅 策略军火库 (全配版)")
    
    # 策略映射
    strat_map = {
        "🟢 没货: CSP (单腿Put收租)": "CSP",
        "🔴 有货: CC (单腿Call止盈)": "CC",
        "🐂 牛市: Bull Put Spread (价差收租)": "BULL_PUT",
        "🐻 熊市: Bear Call Spread (价差收租)": "BEAR_CALL",
        "🦅 震荡: Iron Condor (铁鹰双向收租)": "IRON_CONDOR"
    }
    
    selected_strat_label = st.radio("选择你的战场：", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    # 价差宽度控制
    spread_width = 5
    if strat_code in ['BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR']:
        spread_width = st.slider("保护层宽度 (Spread Width)", 1, 25, 5)

    st.divider()
    ticker = st.text_input("代码 (Ticker)", value="NVDA").upper()
    strike_range_pct = st.slider("扫描范围 (±%)", 10, 40, 20)
    
    if st.button("🚀 启动策略引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"🦅 {ticker} 智能期权终端")

with st.spinner('正在构建多腿策略组合...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 0, 180, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 筛选推荐
    df['score_val'] = df['distance_pct'] * 100
    if strat_code == 'IRON_CONDOR':
        # 铁鹰比较复杂，选 ROI 高且距离适中的
        best_pick = df.sort_values('annualized_return', ascending=False).head(1)
    elif 'SPREAD' in strat_code: # Not strictly used key but logic same
        best_pick = df[df['score_val'] >= 2].sort_values('annualized_return', ascending=False).head(1)
    else:
        best_pick = df[df['score_val'] >= 5].sort_values('annualized_return', ascending=False).head(1)
    
    # 提取画图坐标
    p_strike = None
    c_strike = None
    
    if not best_pick.empty:
        row = best_pick.iloc[0]
        if strat_code == 'IRON_CONDOR':
            p_strike = row['put_strike']
            c_strike = row['call_strike']
        elif strat_code in ['CSP', 'BULL_PUT']:
            p_strike = row['strike'] if 'strike_val' not in row else row['strike_val']
        elif strat_code in ['CC', 'BEAR_CALL']:
            c_strike = row['strike'] if 'strike_val' not in row else row['strike_val']

    # 1. 可视化图表
    if history is not None:
        render_chart(history, ticker, p_strike, c_strike)

    # 2. 核心推荐卡片
    st.subheader("🤖 AI 最佳策略推荐")
    
    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        # 不同的策略显示不同的文案
        info_text = ""
        if strat_code == 'IRON_CONDOR':
            info_text = f"""
            🦅 **铁鹰式 (Iron Condor)**
            **上方压力位**: ${r['call_strike']} | **下方支撑位**: ${r['put_strike']}
            只要 {r['expiration_date']} 之前股价维持在这两个价格中间，你就全赢！
            """
        else:
            info_text = f"**行权价**: {r['strike']}"

        c1, c2 = st.columns([1, 1])
        with c1:
            st.info(f"""
            **{selected_strat_label}**
            
            📅 **到期日**: {r['expiration_date']} (剩{r['days_to_exp']}天)
            💰 **总权利金**: ${r['bid']*100:.0f}
            🛡️ **安全垫**: {r['distance_pct']:.1%}
            🚀 **年化收益**: :red[{r['annualized_return']:.1%}]
            
            {info_text}
            """)
        
        with c2:
            st.warning("👮‍♂️ **风控检查**")
            st.checkbox(f"1. 确认股价 ${current_price:.2f} 准确")
            if strat_code == 'IRON_CONDOR':
                st.checkbox("2. 确认上方和下方 Delta 绝对值均 < 0.2")
            else:
                st.checkbox("2. 确认 Delta < 0.3")
            st.checkbox("3. 确认无财报风险")

    # 3. 列表
    st.divider()
    with st.expander("📋 完整数据列表"):
        st.dataframe(df, use_container_width=True, hide_index=True)
