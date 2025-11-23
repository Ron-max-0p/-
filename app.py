import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (指令版)", 
    layout="wide", 
    page_icon="📝",
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
    /* 指令单样式 */
    .trade-leg {
        padding: 5px 10px;
        border-radius: 5px;
        margin-bottom: 4px;
        font-family: monospace;
        font-weight: bold;
    }
    .sell-leg { background-color: #4a1c1c; color: #ff9999; border-left: 4px solid #ff4b4b; }
    .buy-leg { background-color: #1c3321; color: #99ffbb; border-left: 4px solid #00cc96; }
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
        lower_bound = current_price * (1 - strike_range_pct / 100)
        upper_bound = current_price * (1 + strike_range_pct / 100)
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                # 1. 单腿策略
                if strat_code == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates.apply(lambda x: x['credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL PUT ${x}")
                    
                elif strat_code == 'CC': 
                    candidates = calls[(calls['strike'] >= lower_bound) & (calls['strike'] <= upper_bound)].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL CALL ${x}")
                
                # 2. 垂直价差
                elif strat_code == 'BULL_PUT':
                    shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    candidates = build_vertical_spread(shorts, puts, spread_width, current_price, 'put')
                    
                elif strat_code == 'BEAR_CALL':
                    shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    candidates = build_vertical_spread(shorts, calls, spread_width, current_price, 'call')

                # 3. 铁鹰
                elif strat_code == 'IRON_CONDOR':
                    put_shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    put_spreads = build_vertical_spread(put_shorts, puts, spread_width, current_price, 'put')
                    
                    call_shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    call_spreads = build_vertical_spread(call_shorts, calls, spread_width, current_price, 'call')
                    
                    if put_spreads.empty or call_spreads.empty: continue

                    condors = []
                    top_puts = put_spreads.sort_values('roi', ascending=False).head(10)
                    
                    for _, p_row in top_puts.iterrows():
                        target_dist = abs(p_row['distance_pct'])
                        matching_calls = call_spreads[abs(call_spreads['distance_pct'] - target_dist) < 0.02]
                        
                        for _, c_row in matching_calls.iterrows():
                            total_credit = p_row['bid'] + c_row['bid']
                            max_loss = spread_width - total_credit
                            
                            if max_loss > 0:
                                condors.append({
                                    'strike': f"IC {p_row['short_leg']}/{c_row['short_leg']}", 
                                    'bid': total_credit,
                                    'distance_pct': min(abs(p_row['distance_pct']), abs(c_row['distance_pct'])), 
                                    'capital': max_loss * 100,
                                    'roi': total_credit / max_loss,
                                    # 存储具体的腿，用于前端显示
                                    'p_short': p_row['short_leg'], 'p_long': p_row['long_leg'],
                                    'c_short': c_row['short_leg'], 'c_long': c_row['long_leg']
                                })
                    
                    if condors: candidates = pd.DataFrame(condors)
                    else: candidates = pd.DataFrame()

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
    type_label = "PUT" if type == 'put' else "CALL"
    
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
                    'strike': f"{short_row['strike']}/{long_row['strike']}",
                    'short_leg': short_row['strike'],
                    'long_leg': long_row['strike'],
                    'bid': net_credit,
                    'distance_pct': dist,
                    'capital': max_loss * 100,
                    'roi': net_credit / max_loss
                })
    return pd.DataFrame(spreads)

def render_chart(history_df, ticker, p_strike=None, c_strike=None):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")

    if p_strike and c_strike: # 铁鹰
        fig.add_hline(y=c_strike, line_color="red", line_dash="dash", annotation_text=f"Short Call ${c_strike}")
        fig.add_hline(y=p_strike, line_color="green", line_dash="dash", annotation_text=f"Short Put ${p_strike}", annotation_position="bottom right")
        fig.add_hrect(y0=p_strike, y1=c_strike, fillcolor="green", opacity=0.1, line_width=0)
    elif p_strike: # Put端
        fig.add_hline(y=p_strike, line_color="green", line_dash="dash", annotation_text=f"Short Put ${p_strike}")
        fig.add_hrect(y0=p_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
    elif c_strike: # Call端
        fig.add_hline(y=c_strike, line_color="red", line_dash="dash", annotation_text=f"Short Call ${c_strike}")
        fig.add_hrect(y0=current_price, y1=c_strike, fillcolor="red", opacity=0.1, line_width=0)

    fig.update_layout(title=f"{ticker} 策略可视化", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🦅 策略军火库")
    
    strat_map = {
        "🟢 没货: CSP (单腿Put)": "CSP",
        "🔴 有货: CC (单腿Call)": "CC",
        "🐂 牛市: Bull Put Spread (价差)": "BULL_PUT",
        "🐻 熊市: Bear Call Spread (价差)": "BEAR_CALL",
        "🦅 震荡: Iron Condor (铁鹰)": "IRON_CONDOR"
    }
    selected_strat_label = st.radio("选择战场：", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    spread_width = 5
    if strat_code in ['BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR']:
        spread_width = st.slider("保护层宽度", 1, 25, 5)

    st.divider()
    ticker = st.text_input("代码 (Ticker)", value="NVDA").upper()
    strike_range_pct = st.slider("扫描范围 (±%)", 10, 40, 20)
    if st.button("🚀 启动策略引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"📝 {ticker} 智能指令单")

with st.spinner('AI 正在拆解策略组合...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 0, 180, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    df['score_val'] = df['distance_pct'] * 100
    if strat_code == 'IRON_CONDOR':
        best_pick = df.sort_values('annualized_return', ascending=False).head(1)
    elif 'SPREAD' in strat_code: 
        best_pick = df[df['score_val'] >= 2].sort_values('annualized_return', ascending=False).head(1)
    else:
        best_pick = df[df['score_val'] >= 5].sort_values('annualized_return', ascending=False).head(1)
    
    # 画图参数准备
    p_s, c_s = None, None
    if not best_pick.empty:
        r = best_pick.iloc[0]
        if strat_code == 'IRON_CONDOR': p_s, c_s = r['p_short'], r['c_short']
        elif strat_code in ['CSP', 'BULL_PUT']: p_s = r.get('short_leg', r['strike'])
        elif strat_code in ['CC', 'BEAR_CALL']: c_s = r.get('short_leg', r['strike'])
            
    if history is not None:
        render_chart(history, ticker, p_s, c_s)

    # >>> 核心升级：交易指令卡片 <<<
    st.subheader("🛠️ 推荐交易指令 (Actionable Order)")
    
    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        c1, c2 = st.columns([1.2, 1])
        
        with c1:
            st.markdown(f"**到期日**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            st.markdown("请在券商期权链中依次添加以下合约：")
            
            # 动态生成“腿”的显示 HTML
            legs_html = ""
            
            if strat_code == 'CSP':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["strike"]}</div>'
            elif strat_code == 'CC':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["strike"]}</div>'
            elif strat_code == 'BULL_PUT':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["short_leg"]} (义务)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY PUT ${r["long_leg"]} (保护)</div>'
            elif strat_code == 'BEAR_CALL':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["short_leg"]} (义务)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY CALL ${r["long_leg"]} (保护)</div>'
            elif strat_code == 'IRON_CONDOR':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["c_short"]} (上压力)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY CALL ${r["c_long"]} (上保护)</div>'
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["p_short"]} (下支撑)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY PUT ${r["p_long"]} (下保护)</div>'
            
            st.markdown(legs_html, unsafe_allow_html=True)

        with c2:
            st.success(f"""
            **💰 预期收益分析**
            
            * **净收权利金**: ${r['bid']*100:.0f}
            * **最大风险**: ${r['capital']:.0f}
            * **年化收益**: {r['annualized_return']:.1%}
            * **安全垫**: {r['distance_pct']:.1%}
            """)
            
    else:
        st.warning("暂无合适推荐，请放宽扫描条件。")

    st.divider()
    with st.expander("📋 完整列表"):
        st.dataframe(df, use_container_width=True, hide_index=True)
