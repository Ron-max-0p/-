import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (操盘手版)", 
    layout="wide", 
    page_icon="📈",
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
    .trade-leg {
        padding: 5px 10px; border-radius: 5px; margin-bottom: 4px; font-family: monospace; font-weight: bold;
    }
    .sell-leg { background-color: #4a1c1c; color: #ff9999; border-left: 4px solid #ff4b4b; }
    .buy-leg { background-color: #1c3321; color: #99ffbb; border-left: 4px solid #00cc96; }
    /* 流动性警告 */
    .spread-warning { color: #ffca28; font-weight: bold; font-size: 0.9em; }
    .spread-good { color: #00cc96; font-weight: bold; font-size: 0.9em; }
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
                
                # 辅助函数：计算价差和中点
                def enrich_data(df):
                    df['mid'] = (df['bid'] + df['ask']) / 2
                    df['spread'] = df['ask'] - df['bid']
                    df['spread_pct'] = df.apply(lambda x: (x['spread'] / x['mid']) * 100 if x['mid'] > 0 else 0, axis=1)
                    return df

                calls = enrich_data(calls)
                puts = enrich_data(puts)

                # --- 策略构建 ---
                if strat_code == 'CSP': 
                    candidates = puts[(puts['strike'] >= lower_bound) & (puts['strike'] <= upper_bound)].copy()
                    candidates['credit'] = candidates['bid'] # 保守计算用 bid
                    candidates['mid_credit'] = candidates['mid'] # 参考成交价
                    candidates['capital'] = candidates['strike'] * 100
                    candidates['roi'] = candidates.apply(lambda x: x['mid_credit'] * 100 / x['capital'] if x['capital'] > 0 else 0, axis=1)
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL PUT ${x}")
                    candidates['risk_type'] = 'undefined' # 风险无限
                    
                elif strat_code == 'CC': 
                    candidates = calls[(calls['strike'] >= lower_bound) & (calls['strike'] <= upper_bound)].copy()
                    candidates['credit'] = candidates['bid']
                    candidates['mid_credit'] = candidates['mid']
                    candidates['capital'] = current_price * 100
                    candidates['roi'] = candidates['mid_credit'] * 100 / candidates['capital']
                    candidates['leg_desc'] = candidates['strike'].apply(lambda x: f"SELL CALL ${x}")
                    candidates['risk_type'] = 'undefined'

                elif strat_code == 'IRON_CONDOR':
                    # 简化版铁鹰筛选
                    put_shorts = puts[(puts['strike'] < current_price) & (puts['strike'] >= lower_bound)]
                    call_shorts = calls[(calls['strike'] > current_price) & (calls['strike'] <= upper_bound)]
                    
                    # 简单构建逻辑：只找 Put 和 Call 距离现价 % 差不多的
                    candidates_list = []
                    
                    # 取前 5 个 Put
                    for _, p in put_shorts.head(5).iterrows():
                        # 找对应的 Long Leg
                        p_longs = puts[abs(puts['strike'] - (p['strike'] - spread_width)) < 0.5]
                        if p_longs.empty: continue
                        p_long = p_longs.iloc[0]
                        
                        # 在 Call 端找对称的
                        target_dist = abs((current_price - p['strike']) / current_price)
                        c_shorts = call_shorts.copy()
                        c_shorts['dist_diff'] = abs(((c_shorts['strike'] - current_price) / current_price) - target_dist)
                        match_calls = c_shorts.sort_values('dist_diff').head(2)
                        
                        for _, c in match_calls.iterrows():
                             c_longs = calls[abs(calls['strike'] - (c['strike'] + spread_width)) < 0.5]
                             if c_longs.empty: continue
                             c_long = c_longs.iloc[0]
                             
                             # 计算总权利金 (Mid Price 更真实，但 Bid 更安全)
                             # 这里用 Mid Price 计算推荐排序，用 Bid 做保底
                             total_mid = (p['mid'] - p_long['mid']) + (c['mid'] - c_long['mid'])
                             total_bid = (p['bid'] - p_long['ask']) + (c['bid'] - c_long['ask']) # 最差成交
                             
                             max_loss = spread_width - total_mid
                             if max_loss > 0:
                                 candidates_list.append({
                                     'strike': f"IC {p['strike']}/{c['strike']}",
                                     'credit': total_bid,
                                     'mid_credit': total_mid,
                                     'capital': max_loss * 100,
                                     'distance_pct': target_dist,
                                     'roi': total_mid / max_loss,
                                     'p_short': p['strike'], 'p_long': p_long['strike'],
                                     'c_short': c['strike'], 'c_long': c_long['strike'],
                                     'spread_avg': (p['spread'] + c['spread']) / 2, # 平均价差
                                     'risk_type': 'defined'
                                 })
                    candidates = pd.DataFrame(candidates_list)

                # 为了代码简洁，只处理这几个主要策略，其他逻辑类似...
                elif 'SPREAD' in strat_code: # 占位，防止报错
                     candidates = pd.DataFrame()

                if not candidates.empty:
                    candidates['days_to_exp'] = days
                    candidates['expiration_date'] = date
                    candidates['annualized_return'] = candidates['roi'] * (365 / days)
                    candidates['distance_pct'] = candidates.get('distance_pct', 0)
                    all_opportunities.append(candidates)
                    
            except Exception:
                continue

        if not all_opportunities: return None, current_price, history, "没有找到符合条件的合约"

        df = pd.concat(all_opportunities)
        return df, current_price, history, None

    except Exception as e:
        return None, 0, None, f"API 错误: {str(e)}"

# 画损益图
def render_payoff(strategy_type, current_price, r):
    # 生成 X 轴 (股价范围)
    x = np.linspace(current_price * 0.8, current_price * 1.2, 100)
    y = []
    
    premium = r['mid_credit'] # 使用中间价计算 P/L
    
    if strategy_type == 'CSP':
        strike = r['strike'] if 'strike' in r and isinstance(r['strike'], float) else float(r['strike'].split(' ')[-1].replace('$',''))
        # 卖Put损益：如果股价 > 行权价，赚权利金；否则亏损
        y = np.where(x > strike, premium * 100, (x - strike + premium) * 100)
        breakeven = strike - premium
        
    elif strategy_type == 'IRON_CONDOR':
        p_s, p_l = r['p_short'], r['p_long']
        c_s, c_l = r['c_short'], r['c_long']
        
        # 铁鹰损益函数
        for price in x:
            # Put Spread P/L
            put_val = 0
            if price < p_l: put_val = p_l - p_s # 最大亏损
            elif price < p_s: put_val = price - p_s # 部分亏损
            # Call Spread P/L
            call_val = 0
            if price > c_l: call_val = c_s - c_l # 最大亏损
            elif price > c_s: call_val = c_s - price
            
            total_val = (put_val + call_val + premium) * 100
            y.append(total_val)
        
        breakeven = f"${p_s - premium:.2f} / ${c_s + premium:.2f}"
    
    else:
        # 简单处理其他情况
        y = np.zeros(len(x))
        breakeven = "N/A"

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=y, mode='lines', fill='tozeroy', name='P/L'))
    
    # 颜色区域：绿色盈利，红色亏损
    fig.add_hrect(y0=0, y1=max(y)*1.2, fillcolor="green", opacity=0.1, line_width=0)
    fig.add_hrect(y0=min(y)*1.2, y1=0, fillcolor="red", opacity=0.1, line_width=0)
    
    # 现价线
    fig.add_vline(x=current_price, line_dash="dot", annotation_text="现价")
    
    fig.update_layout(
        title="📊 到期损益模拟 (P/L Diagram)",
        xaxis_title="股票价格",
        yaxis_title="盈亏金额 ($)",
        height=350,
        template="plotly_dark",
        margin=dict(l=20, r=20, t=40, b=20)
    )
    st.plotly_chart(fig, use_container_width=True)
    return breakeven

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("📈 操盘手控制台")
    
    strat_map = {
        "🟢 没货: CSP (单腿Put)": "CSP",
        "🦅 震荡: Iron Condor (铁鹰)": "IRON_CONDOR"
    }
    selected_strat_label = st.radio("选择策略", list(strat_map.keys()))
    strat_code = strat_map[selected_strat_label]
    
    spread_width = 5
    if strat_code == 'IRON_CONDOR': spread_width = st.slider("铁鹰翼展 (Width)", 1, 25, 5)

    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    strike_range_pct = st.slider("扫描范围 (±%)", 10, 40, 20)
    if st.button("🚀 生成分析报告", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---
st.title(f"📈 {ticker} 交易分析终端")

with st.spinner('计算买卖价差与损益模型...'):
    df, current_price, history, error_msg = fetch_market_data(ticker, 0, 180, strat_code, spread_width, strike_range_pct)

if error_msg:
    st.error(error_msg)
else:
    # 推荐逻辑
    if strat_code == 'IRON_CONDOR':
        best_pick = df.sort_values('annualized_return', ascending=False).head(1)
    else:
        df['score_val'] = df['distance_pct'] * 100
        best_pick = df[df['score_val'] >= 5].sort_values('annualized_return', ascending=False).head(1)

    if not best_pick.empty:
        r = best_pick.iloc[0]
        
        c1, c2 = st.columns([1.5, 1])
        
        with c1:
            st.subheader("🛠️ 交易指令单 (Order Ticket)")
            
            # 1. 腿部展示
            legs_html = ""
            if strat_code == 'CSP':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["strike"]}</div>'
            elif strat_code == 'IRON_CONDOR':
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL CALL ${r["c_short"]}</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY CALL ${r["c_long"]}</div>'
                legs_html += f'<div class="trade-leg sell-leg">🔴 SELL PUT ${r["p_short"]}</div>'
                legs_html += f'<div class="trade-leg buy-leg">🟢 BUY PUT ${r["p_long"]}</div>'
            st.markdown(legs_html, unsafe_allow_html=True)
            
            # 2. 价格分析 (Bid/Ask)
            st.markdown("---")
            st.markdown("#### 💰 价格分析 (Liquidity Check)")
            
            col_p1, col_p2, col_p3 = st.columns(3)
            col_p1.metric("保守卖价 (Bid)", f"${r['credit']:.2f}")
            col_p2.metric("中间价 (Mid)", f"${r['mid_credit']:.2f}", help="这通常是你能成交的真实价格")
            col_p3.metric("买一价 (Ask)", "---") # Ask对于卖方来说是对手盘，不用看
            
            # 流动性警告逻辑
            spread_gap = r['mid_credit'] - r['credit']
            if spread_gap > 0.2: # 差价超过0.2，警告
                st.markdown(f"<span class='spread-warning'>⚠️ 流动性预警：价差较大 (约 ${spread_gap:.2f})，请务必使用限价单(Limit Order)在中间价挂单！</span>", unsafe_allow_html=True)
            else:
                st.markdown("<span class='spread-good'>✅ 流动性良好：价差较小，容易成交。</span>", unsafe_allow_html=True)

        with c2:
            st.subheader("📊 损益模拟")
            be_points = render_payoff(strat_code, current_price, r)
            
            st.info(f"""
            **关键点位**
            * **最大盈利**: ${r['mid_credit']*100:.0f}
            * **最大亏损**: {'无限' if strat_code=='CSP' else f'${r["capital"]:.0f}'}
            * **盈亏平衡点**: {be_points}
            """)
            
    else:
        st.warning("暂无合适机会")

    st.divider()
    with st.expander("📋 完整数据"):
        st.dataframe(df, use_container_width=True)
