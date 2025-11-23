import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (策略扩充版)", 
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
                
                # --- 策略逻辑 ---

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
                
                # 2. 垂直价差
                elif strat_code == 'BULL_PUT':
                    shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    candidates = build_vertical_spread(shorts, puts, spread_width, current_price, 'put')
                elif strat_code == 'BEAR_CALL':
                    shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    candidates = build_vertical_spread(shorts, calls, spread_width, current_price, 'call')

                # 3. 铁鹰 (Iron Condor)
                elif strat_code == 'IRON_CONDOR':
                    candidates = build_iron_condor(puts, calls, current_price, lower_bound, upper_bound, spread_width)

                # 4. 铁蝶 (Iron Butterfly) - 新增
                elif strat_code == 'IRON_BUTTERFLY':
                    # 逻辑：卖 ATM Put + 卖 ATM Call (最中心)，然后两边买保护
                    # 1. 找到最接近现价的行权价 (ATM Strike)
                    atm_strike = min(puts['strike'], key=lambda x:abs(x-current_price))
                    
                    # 2. 获取四个腿
                    p_short = puts[puts['strike'] == atm_strike]
                    c_short = calls[calls['strike'] == atm_strike]
                    p_long = puts[puts['strike'] == atm_strike - spread_width]
                    c_long = calls[calls['strike'] == atm_strike + spread_width]
                    
                    butterfly_list = []
                    if not (p_short.empty or c_short.empty or p_long.empty or c_long.empty):
                        ps, cs, pl, cl = p_short.iloc[0], c_short.iloc[0], p_long.iloc[0], c_long.iloc[0]
                        
                        total_credit = ps['bid'] + cs['bid'] - pl['ask'] - cl['ask'] # 净收入
                        max_loss = spread_width - total_credit
                        
                        if max_loss > 0:
                            butterfly_list.append({
                                'strike': f"ATM ${atm_strike}",
                                'bid': total_credit,
                                'distance_pct': 0, # 铁蝶本身就是赌不动的，安全垫为0
                                'capital': max_loss * 100,
                                'roi': total_credit / max_loss,
                                'p_short': ps['strike'], 'p_long': pl['strike'],
                                'c_short': cs['strike'], 'c_long': cl['strike']
                            })
                    candidates = pd.DataFrame(butterfly_list)

                # 5. 宽跨式 (Short Strangle) - 新增
                elif strat_code == 'SHORT_STRANGLE':
                    # 逻辑：裸卖 OTM Put + 裸卖 OTM Call
                    # 类似铁鹰，但没有买入保护腿
                    p_shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    c_shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    
                    strangle_list = []
                    # 简单匹配：距离现价 % 差不多的
                    top_puts = p_shorts.head(10) # 取离现价最近的几个OTM
                    
                    for _, p in top_puts.iterrows():
                        target_dist = abs((current_price - p['strike']) / current_price)
                        # 找 Call 端距离差不多的
                        c_shorts_copy = c_shorts.copy()
                        c_shorts_copy['dist_diff'] = abs(((c_shorts_copy['strike'] - current_price) / current_price) - target_dist)
                        match_calls = c_shorts_copy.sort_values('dist_diff').head(2)
                        
                        for _, c in match_calls.iterrows():
                            total_credit = p['bid'] + c['bid']
                            # 风险无限，保证金通常估算为股价的20%左右，这里为了计算 ROI，暂用名义本金的一定比例做分母
                            # 注意：这是估算，实战中看券商要求
                            margin_est = current_price * 0.2 * 100 
                            
                            strangle_list.append({
                                'strike': f"Strangle {p['strike']}/{c['strike']}",
                                'bid': total_credit,
                                'distance_pct': min(abs((current_price - p['strike']) / current_price), abs((c['strike'] - current_price) / current_price)),
                                'capital': margin_est, # 仅供参考
                                'roi': total_credit * 100 / margin_est,
                                'p_short': p['strike'], 'c_short': c['strike']
                            })
                    candidates = pd.DataFrame(strangle_list)


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

# 辅助函数：构建垂直价差
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
                    'strike': f"{short_row['strike']}/{long_row['strike']}",
                    'short_leg': short_row['strike'],
                    'long_leg': long_row['strike'],
                    'bid': net_credit,
                    'distance_pct': dist,
                    'capital': max_loss * 100,
                    'roi': net_credit / max_loss
                })
    return pd.DataFrame(spreads)

# 辅助函数：构建铁鹰
def build_iron_condor(puts, calls, current_price, lower_bound, upper_bound, width):
    put_shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
    put_spreads = build_vertical_spread(put_shorts, puts, width, current_price, 'put')
    
    call_shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
    call_spreads = build_vertical_spread(call_shorts, calls, width, current_price, 'call')
    
    if put_spreads.empty or call_spreads.empty: return pd.DataFrame()

    condors = []
    top_puts = put_spreads.sort_values('roi', ascending=False).head(10)
    
    for _, p_row in top_puts.iterrows():
        target_dist = abs(p_row['distance_pct'])
        matching_calls = call_spreads[abs(call_spreads['distance_pct'] - target_dist) < 0.02]
        
        for _, c_row in matching_calls.iterrows():
            total_credit = p_row['bid'] + c_row['bid']
            max_loss = width - total_credit
            
            if max_loss > 0:
                condors.append({
                    'strike': f"IC {p_row['short_leg']}/{c_row['short_leg']}", 
                    'bid': total_credit,
                    'distance_pct': min(abs(p_row['distance_pct']), abs(c_row['distance_pct'])), 
                    'capital': max_loss * 100,
                    'roi': total_credit / max_loss,
                    'p_short': p_row['short_leg'], 'p_long': p_row['long_leg'],
                    'c_short': c_row['short_leg'], 'c_long': c_row['long_leg']
                })
    return pd.DataFrame(condors)

def render_chart(history_df, ticker, p_strike=None, c_strike=None):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index,
                open=history_df['Open'], high=history_df['High'],
                low=history_df['Low'], close=history_df['Close'],
                name=ticker)])
    
    current_price = history_df['Close'].iloc[-1]
    fig.add_hline(y=current_price, line_dash="dot", line_color="gray", annotation_text="现价")

    if p_strike and c_strike: # 双卖逻辑
        fig.add_hline(y=c_strike, line_color="red", line_dash="dash", annotation_text=f"Short Call ${c_strike}")
        fig.add_hline(y=p_strike, line_color="green", line_dash="dash", annotation_text=f"Short Put ${p_strike}", annotation_position="bottom right")
        fig.add_hrect(y0=p_strike, y1=c_strike, fillcolor="green", opacity=0.1, line_width=0)
    elif p_strike: 
        fig.add_hline(y=p_strike, line_color="green", line_dash="dash", annotation_text=f"Short Put ${p_strike}")
        fig.add_hrect(y0=p_strike, y1=current_price, fillcolor="green", opacity=0.1, line_width=0)
    elif c_strike: 
        fig.add_hline(y=c_strike, line_color="red", line_dash="dash", annotation_text=f"Short Call ${c_strike}")
        fig.add_hrect(y0=current_price, y1=c_strike, fillcolor="red", opacity=0.1, line_width=0)

    fig.update_layout(title=f"{ticker} 策略可视化", height=350, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🦅 策略军火库")
    
    strat_map = {
        "🟢 没货: CSP (卖Put收租)": "CSP",
        "🔴 有货: CC (卖Call止盈)": "CC",
        "🐂 牛市: Bull Put Spread (价差)": "BULL_PUT",
        "🐻 熊市: Bear Call Spread (价差)": "BEAR_CALL",
        "🦅 震荡: Iron Condor (铁鹰)": "IRON_CONDOR",
        "🦋 极度横盘: Iron Butterfly (铁蝶)": "IRON_BUTTERFLY",
        "⚡ 狂野收租: Short Strangle (宽跨式)": "SHORT_STRANGLE"
    }
    selected_strat_label = st.radio("选择战场：", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    spread_width = 5
    if strat_code in ['BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY']:
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
    
    # 推荐排序逻辑
    if strat_code == 'IRON_BUTTERFLY':
        # 铁蝶没得选，通常就一个ATM，选权利金最高的
        best_pick = df.sort_values('bid', ascending=False).head(1)
    elif strat_code == 'SHORT_STRANGLE':
         # 宽跨式选安全垫适中的
         best_pick = df[df['score_val'] >= 5].sort_values('annualized_return', ascending=False).head(1)
    elif strat_code == 'IRON_CONDOR':
        best_pick = df.sort_values('annualized_return', ascending=False).head(1)
    elif 'SPREAD' in strat_code: 
        best_pick = df[df['score_val'] >= 2].sort_values('annualized_return', ascending=False).head(1)
    else:
        best_pick = df[df['score_val'] >= 5].sort_values('annualized_return', ascending=False).head(1)
    
    # 画图参数
    p_s, c_s = None, None
    if not best_pick.empty:
        r = best_pick.iloc[0]
        if strat_code in ['IRON_CONDOR', 'IRON_BUTTERFLY']: 
            p_s, c_s = r['p_short'], r['c_short']
        elif strat_code == 'SHORT_STRANGLE':
            p_s, c_s = r['p_short'], r['c_short']
        elif strat_code in ['CSP', 'BULL_PUT']: 
            p_s = r.get('short_leg', r['strike'])
        elif strat_code in ['CC', 'BEAR_CALL']: 
            c_s = r.get('short_leg', r['strike'])
            
    if history is not None:
        render_chart(history, ticker, p_s, c_s)

    # >>> 核心：交易指令卡片 <<<
    st.subheader("🛠️ 推荐交易指令 (Actionable Order)")
    
    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        c1, c2 = st.columns([1.2, 1])
        
        with c1:
            st.markdown(f"**到期日**: {r['expiration_date']} (剩 {r['days_to_exp']} 天)")
            st.markdown("请在券商期权链中依次添加以下合约：")
            
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
            elif strat_code == 'IRON_CONDOR' or strat_code == 'IRON_BUTTERFLY':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["c_short"]} (中轴)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY CALL ${r["c_long"]} (上保)</div>'
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["p_short"]} (中轴)</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY PUT ${r["p_long"]} (下保)</div>'
            elif strat_code == 'SHORT_STRANGLE':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["c_short"]}</div>'
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["p_short"]}</div>'
            
            st.markdown(legs_html, unsafe_allow_html=True)

        with c2:
            roi_display = f"{r['annualized_return']:.1%}" if r['capital'] > 0 else "无限 (Risk Undefined)"
            capital_display = f"${r['capital']:.0f}" if r['capital'] > 0 else "保证金 (Margin)"
            
            st.success(f"""
            **💰 预期收益分析**
            
            * **净收权利金**: ${r['bid']*100:.0f}
            * **最大风险**: {capital_display}
            * **年化收益**: {roi_display}
            * **安全垫**: {r['distance_pct']:.1%}
            """)
            
            if strat_code == 'SHORT_STRANGLE':
                st.error("⚠️ **高风险警告**：宽跨式策略理论风险无限。请确保保证金充足！")
            if strat_code == 'IRON_BUTTERFLY':
                st.info("💡 **铁蝶提示**：此策略赌股价**极度横盘**。如果股价大涨大跌都会亏损，但收到的权利金非常厚。")
            
    else:
        st.warning("暂无合适推荐，请放宽扫描条件。")

    st.divider()
    with st.expander("📋 完整列表"):
        st.dataframe(df, use_container_width=True, hide_index=True)
