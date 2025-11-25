import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import numpy as np
import scipy.stats as si

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="包子铺", 
    layout="wide", 
    page_icon="🥟",
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
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心引擎 ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0: return 0
    try:
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        if option_type == 'call': return si.norm.cdf(d1)
        else: return si.norm.cdf(d1) - 1.0
    except:
        return 0 # 计算出错返回0

def process_chain(df, current_price, days_to_exp, type, risk_free_rate=0.045):
    T = days_to_exp / 365.0
    df['type'] = type
    # 填充缺失值，防止报错
    df['impliedVolatility'] = df['impliedVolatility'].fillna(0)
    df['openInterest'] = df['openInterest'].fillna(0)
    df['bid'] = df['bid'].fillna(0)
    
    # 计算 Delta
    df['delta'] = df.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, risk_free_rate, x['impliedVolatility'], type), axis=1)
    
    # v16修改：不再进行严格过滤，保留所有数据，在策略层再筛
    return df.copy()

def get_earnings_date(ticker_obj):
    try:
        cal = ticker_obj.calendar
        if cal and 'Earnings Date' in cal: return cal['Earnings Date'][0]
        return None
    except: return None

# --- 策略构建器 ---
def build_spread(longs, shorts, width, type='credit'):
    spreads = []
    for _, s in shorts.iterrows():
        target = s['strike'] - width if s['type']=='put' else s['strike'] + width
        matches = longs[abs(longs['strike'] - target) < 0.5] # 放宽匹配容差
        if not matches.empty:
            l = matches.iloc[0]
            net = s['bid'] - l['ask']
            # 放宽价格限制，哪怕没肉也先显示出来，方便调试
            loss = width - net
            roi = net/loss if loss > 0 else 0
            spreads.append({
                'desc': f"SELL {s['type'].upper()} ${s['strike']} / BUY {l['type'].upper()} ${l['strike']}",
                'price_display': net, 'capital': loss*100, 'roi': roi,
                'delta': s['delta'] - l['delta'],
                'breakeven': s['strike'] - net if s['type']=='put' else s['strike'] + net,
                'legs': [{'side':'SELL', 'type':s['type'].upper(), 'strike':s['strike']}, {'side':'BUY', 'type':l['type'].upper(), 'strike':l['strike']}]
            })
    return pd.DataFrame(spreads)

@st.cache_data(ttl=300)
def fetch_market_data(ticker, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="3mo") 
        if history.empty: return None, 0, None, None, "无法获取股价数据，请检查代码是否正确或网络"
        current_price = history['Close'].iloc[-1]
        next_earnings = get_earnings_date(stock)
        
        expirations = stock.options
        if not expirations: return None, current_price, history, next_earnings, "未获取到期权链，可能是非交易时间或数据源问题"

        today = datetime.now().date()
        date_map = []
        for d_str in expirations:
            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
                days = (d_obj - today).days
                date_map.append((d_str, days))
            except: continue

        all_opps = []
        
        # 简单的日期筛选逻辑
        target_dates = []
        for d_str, days in date_map:
            # 放宽日期限制，只要没过期的都拿来看
            if days >= 2: target_dates.append((d_str, days))

        lower = current_price * (1 - strike_range_pct/100)
        upper = current_price * (1 + strike_range_pct/100)

        for date, days in target_dates:
            try:
                opt = stock.option_chain(date)
                calls = process_chain(opt.calls, current_price, days, 'call')
                puts = process_chain(opt.puts, current_price, days, 'put')
                
                # 基础范围过滤
                calls = calls[(calls['strike'] >= lower) & (calls['strike'] <= upper)]
                puts = puts[(puts['strike'] >= lower) & (puts['strike'] <= upper)]

                if calls.empty and puts.empty: continue

                # === 策略逻辑 (带自动降级) ===
                
                # 1. CSP (卖Put)
                if strat_code == 'CSP':
                    # 尝试找 Delta 合适的
                    df = puts[(puts['delta'] > -0.4) & (puts['delta'] < -0.1)]
                    # 降级：如果没找到，直接找虚值的
                    if df.empty:
                        df = puts[puts['strike'] < current_price * 0.98]
                    
                    for _, r in df.iterrows():
                        all_opps.append({
                            'expiration_date': date, 'days_to_exp': days, 'desc': f"SELL PUT ${r['strike']}",
                            'price_display': r['bid'], 'capital': r['strike']*100, 'roi': r['bid']/r['strike'] if r['strike']>0 else 0,
                            'delta': r['delta'], 'breakeven': f"${r['strike']-r['bid']:.2f}",
                            'legs': [{'side':'SELL', 'type':'PUT', 'strike':r['strike']}]
                        })

                # 2. CC (卖Call)
                elif strat_code == 'CC':
                    df = calls[(calls['delta'] < 0.4) & (calls['delta'] > 0.1)]
                    if df.empty: df = calls[calls['strike'] > current_price * 1.02]
                    
                    for _, r in df.iterrows():
                        all_opps.append({
                            'expiration_date': date, 'days_to_exp': days, 'desc': f"SELL CALL ${r['strike']}",
                            'price_display': r['bid'], 'capital': current_price*100, 'roi': r['bid']/current_price,
                            'delta': r['delta'], 'breakeven': f"${current_price-r['bid']:.2f}",
                            'legs': [{'side':'SELL', 'type':'CALL', 'strike':r['strike']}]
                        })

                # 3. 垂直价差 (Bull Put / Bear Call)
                elif strat_code == 'BULL_PUT':
                    shorts = puts[(puts['delta'] > -0.5) & (puts['delta'] < -0.1)] # 放宽范围
                    if shorts.empty: shorts = puts[puts['strike'] < current_price]
                    res = build_spread(puts, shorts, spread_width, 'credit')
                    for _, r in res.iterrows():
                        r.update({'expiration_date': date, 'days_to_exp': days})
                        all_opps.append(r)

                elif strat_code == 'BEAR_CALL':
                    shorts = calls[(calls['delta'] < 0.5) & (calls['delta'] > 0.1)]
                    if shorts.empty: shorts = calls[calls['strike'] > current_price]
                    res = build_spread(calls, shorts, spread_width, 'credit')
                    for _, r in res.iterrows():
                        r.update({'expiration_date': date, 'days_to_exp': days})
                        all_opps.append(r)

                # 4. Iron Condor
                elif strat_code == 'IRON_CONDOR':
                    p_s = puts[(puts['delta'] > -0.3) & (puts['delta'] < -0.1)]
                    c_s = calls[(calls['delta'] < 0.3) & (calls['delta'] > 0.1)]
                    if p_s.empty: p_s = puts[(puts['strike'] < current_price*0.95)]
                    if c_s.empty: c_s = calls[(calls['strike'] > current_price*1.05)]
                    
                    p_spr = build_spread(puts, p_s, spread_width, 'credit')
                    c_spr = build_spread(calls, c_s, spread_width, 'credit')
                    
                    if not p_spr.empty and not c_spr.empty:
                        p_list = p_spr.head(5).to_dict('records')
                        c_list = c_spr.head(5).to_dict('records')
                        for p in p_list:
                            for c in c_list:
                                net = p['price_display'] + c['price_display']
                                loss = spread_width - net
                                all_opps.append({
                                    'expiration_date': date, 'days_to_exp': days,
                                    'desc': f"IC Put ${p['legs'][0]['strike']} / Call ${c['legs'][0]['strike']}",
                                    'price_display': net, 'capital': loss*100, 'roi': net/loss if loss>0 else 0,
                                    'delta': p['delta'] + c['delta'], 
                                    'breakeven': f"${p['legs'][0]['strike']-net:.1f}/${c['legs'][0]['strike']+net:.1f}",
                                    'legs': p['legs'] + c['legs']
                                })

            except Exception: continue

        if not all_opps: return None, current_price, history, next_earnings, "策略匹配为空（建议放宽扫描范围）"
        df = pd.DataFrame(all_opps)
        # 统一计算年化
        df['annualized_return'] = df.apply(lambda x: x['roi'] * (365/x['days_to_exp']) if x['roi']>0 and x['days_to_exp']>0 else 0, axis=1)
        return df, current_price, history, next_earnings, None

    except Exception as e: return None, 0, None, None, f"API 错误: {str(e)}"

def render_chart(history_df, ticker, r):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index, open=history_df['Open'], high=history_df['High'], low=history_df['Low'], close=history_df['Close'], name=ticker)])
    cp = history_df['Close'].iloc[-1]
    fig.add_hline(y=cp, line_dash="dot", line_color="gray", annotation_text="现价")
    if 'legs' in r:
        for leg in r['legs']:
            col = "red" if "SELL" in leg['side'] else "green"
            fig.add_hline(y=leg['strike'], line_color=col, line_dash="dash")
    fig.update_layout(height=350, margin=dict(l=20, r=20, t=20, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染 ---

with st.sidebar:
    st.header("🥟 包子铺配置")
    
    strat_map = {
        "CSP (卖Put收租)": "CSP", 
        "CC (卖Call收租)": "CC", 
        "Bull Put Spread": "BULL_PUT", 
        "Bear Call Spread": "BEAR_CALL", 
        "Iron Condor": "IRON_CONDOR"
    }
    
    s_name = st.radio("选择战术", list(strat_map.keys()))
    strat_code = strat_map[s_name]
    
    spread_width = 5
    if "Spread" in s_name or "Condor" in s_name:
        spread_width = st.slider("价差宽度", 1, 20, 5)

    st.divider()
    ticker = st.text_input("代码", value="AMD").upper()
    # 关键修改：默认范围调大，方便捕捉数据
    strike_range_pct = st.slider("扫描范围 (%)", 5, 50, 30)
    
    # 调试开关
    show_debug = st.checkbox("🐞 开启调试模式 (如果没数据请勾选)")
    
    if st.button("🚀 启动引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

st.title(f"{ticker} 策略")

with st.spinner(f'正在扫描 {s_name}...'):
    df, current_price, history, next_earnings, err = fetch_market_data(ticker, strat_code, spread_width, strike_range_pct)

if err:
    st.error(f"❌ 发生错误: {err}")
    if show_debug:
        st.info("可能是网络问题或 yfinance 数据源暂时不可用。请稍后再试。")
else:
    if not df.empty:
        # 排序逻辑
        best = df.sort_values('annualized_return', ascending=False).head(1)
        r = best.iloc[0]

        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("🏆 最佳推荐")
            st.markdown(f"**合约**: {r['expiration_date']}")
            if 'legs' in r:
                for leg in r['legs']:
                    c = "sell-leg" if "SELL" in leg['side'] else "buy-leg"
                    st.markdown(f'<div class="trade-leg {c}">{leg["side"]} {leg["type"]} ${leg["strike"]}</div>', unsafe_allow_html=True)
            else:
                st.markdown(r['desc'])

        with c2:
            st.metric("预估收入", f"${r['price_display']*100:.0f}")
            st.metric("年化收益", f"{r['annualized_return']:.1%}")
            st.metric("盈亏平衡", r['breakeven'])

        if history is not None:
            render_chart(history, ticker, r)
            
        st.divider()
        with st.expander("📋 完整列表"):
            st.dataframe(df, use_container_width=True)
    else:
        st.warning("⚠️ 数据获取成功，但在当前筛选条件下没找到策略。")
        st.markdown("**建议：**\n1. 调大左侧的【扫描范围】\n2. 勾选【调试模式】查看详情")

# --- 调试区域 ---
if show_debug:
    st.divider()
    st.markdown("### 🐞 调试面板")
    try:
        stock = yf.Ticker(ticker)
        exps = stock.options
        st.write(f"1. 获取到的到期日: {exps}")
        if exps:
            opt = stock.option_chain(exps[0])
            st.write(f"2. {exps[0]} 的原始数据样本 (Calls):")
            st.dataframe(opt.calls.head())
    except Exception as e:
        st.error(f"调试信息获取失败: {e}")

