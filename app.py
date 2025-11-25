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
    page_icon="♾️",
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
    .risk-badge { padding: 2px 6px; border-radius: 4px; font-size: 0.8em; font-weight: bold; background: #555; color: #fff; }
</style>
""", unsafe_allow_html=True)

# --- 3. 量化核心引擎 ---

def black_scholes_delta(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0: return 0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == 'call': return si.norm.cdf(d1)
    else: return si.norm.cdf(d1) - 1.0

def process_chain(df, current_price, days_to_exp, type, risk_free_rate=0.045):
    T = days_to_exp / 365.0
    df['type'] = type
    df['delta'] = df.apply(lambda x: black_scholes_delta(current_price, x['strike'], T, risk_free_rate, x['impliedVolatility'], type), axis=1)
    return df[(df['openInterest'] > 5) & (df['bid'] > 0)].copy()

def get_earnings_date(ticker_obj):
    try:
        cal = ticker_obj.calendar
        if cal and 'Earnings Date' in cal: return cal['Earnings Date'][0]
        return None
    except: return None

# --- 策略构建器集合 ---

def build_spread(longs, shorts, width, type='credit'):
    spreads = []
    for _, s in shorts.iterrows():
        target = s['strike'] - width if s['type']=='put' else s['strike'] + width
        matches = longs[abs(longs['strike'] - target) < 0.1]
        if not matches.empty:
            l = matches.iloc[0]
            net = s['bid'] - l['ask']
            if net > 0.01:
                loss = width - net
                spreads.append({
                    'desc': f"SELL {s['type'].upper()} ${s['strike']} / BUY {l['type'].upper()} ${l['strike']}",
                    'price_display': net, 'capital': loss*100, 'roi': net/loss,
                    'delta': s['delta'] - l['delta'],
                    'breakeven': s['strike'] - net if s['type']=='put' else s['strike'] + net,
                    'legs': [{'side':'SELL', 'type':s['type'].upper(), 'strike':s['strike']}, {'side':'BUY', 'type':l['type'].upper(), 'strike':l['strike']}]
                })
    return pd.DataFrame(spreads)

@st.cache_data(ttl=300)
def fetch_market_data(ticker, strat_code, spread_width, strike_range_pct):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="6mo") 
        if history.empty: return None, 0, None, None, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        next_earnings = get_earnings_date(stock)
        
        expirations = stock.options
        if not expirations: return None, current_price, history, next_earnings, "无期权链"

        today = datetime.now().date()
        date_map = []
        for d_str in expirations:
            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            days = (d_obj - today).days
            date_map.append((d_str, days))

        all_opps = []
        
        # === 跨期策略逻辑 (PMCC / Calendar) ===
        if strat_code in ['PMCC', 'CALENDAR']:
            min_far = 150 if strat_code == 'PMCC' else 60
            far_dates = [d for d in date_map if d[1] > min_far]
            near_dates = [d for d in date_map if 20 <= d[1] <= 45]
            
            if far_dates and near_dates:
                far_d, far_days = far_dates[0]
                near_d, near_days = near_dates[0]
                
                c_far = process_chain(stock.option_chain(far_d).calls, current_price, far_days, 'call')
                c_near = process_chain(stock.option_chain(near_d).calls, current_price, near_days, 'call')
                
                if strat_code == 'PMCC':
                    longs = c_far[c_far['delta'] > 0.8]
                    shorts = c_near[(c_near['delta'] > 0.2) & (c_near['delta'] < 0.4)]
                    for _, l in longs.iterrows():
                        valid_shorts = shorts[shorts['strike'] > l['strike']]
                        for _, s in valid_shorts.iterrows():
                            debit = l['ask'] - s['bid']
                            if debit < (s['strike'] - l['strike']):
                                all_opps.append({
                                    'expiration_date': f"Near: {near_d} / Far: {far_d}", 'days_to_exp': near_days,
                                    'desc': f"BUY LEAPS ${l['strike']} / SELL CALL ${s['strike']}",
                                    'price_display': debit, 'capital': debit*100, 'roi': 0, 'delta': l['delta'] - s['delta'],
                                    'breakeven': f"${l['strike'] + debit:.2f}",
                                    'legs': [{'side':'BUY', 'type':'CALL', 'strike':l['strike']}, {'side':'SELL', 'type':'CALL', 'strike':s['strike']}]
                                })
                
                elif strat_code == 'CALENDAR':
                    atm_strikes = c_near[abs(c_near['delta'] - 0.5) < 0.1]['strike']
                    for k in atm_strikes:
                        l = c_far[c_far['strike'] == k]
                        s = c_near[c_near['strike'] == k]
                        if not l.empty and not s.empty:
                            debit = l.iloc[0]['ask'] - s.iloc[0]['bid']
                            if debit > 0:
                                all_opps.append({
                                    'expiration_date': f"Short: {near_d} / Long: {far_d}", 'days_to_exp': near_days,
                                    'desc': f"SELL CALL ${k} ({near_d}) / BUY CALL ${k} ({far_d})",
                                    'price_display': debit, 'capital': debit*100, 'roi': 0, 'delta': 0, 'breakeven': "N/A",
                                    'legs': [{'side':'SELL', 'type':'CALL', 'strike':k}, {'side':'BUY', 'type':'CALL', 'strike':k}]
                                })

        # === 同期策略逻辑 ===
        else:
            target_dates = []
            for d_str, days in date_map:
                if "LEAPS" in strat_code:
                    if days > 180: target_dates.append((d_str, days))
                elif strat_code in ['CSP', 'CC', 'BULL_PUT', 'IRON_CONDOR', 'JADE_LIZARD']:
                    if 14 <= days <= 60: target_dates.append((d_str, days))
                else: 
                    if 7 <= days <= 45: target_dates.append((d_str, days))

            lower = current_price * (1 - strike_range_pct/100)
            upper = current_price * (1 + strike_range_pct/100)

            for date, days in target_dates:
                try:
                    opt = stock.option_chain(date)
                    calls = process_chain(opt.calls, current_price, days, 'call')
                    puts = process_chain(opt.puts, current_price, days, 'put')
                    
                    calls = calls[(calls['strike'] >= lower) & (calls['strike'] <= upper)]
                    puts = puts[(puts['strike'] >= lower) & (puts['strike'] <= upper)]

                    # 1. 基础单腿
                    if strat_code == 'CSP':
                        df = puts[(puts['delta'] > -0.35) & (puts['delta'] < -0.15)]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days, 'desc': f"SELL PUT ${r['strike']}",
                                'price_display': r['bid'], 'capital': r['strike']*100, 'roi': r['bid']/r['strike'],
                                'delta': r['delta'], 'breakeven': f"${r['strike']-r['bid']:.2f}",
                                'legs': [{'side':'SELL', 'type':'PUT', 'strike':r['strike']}]
                            })
                    
                    elif strat_code == 'CC':
                        df = calls[(calls['delta'] < 0.35) & (calls['delta'] > 0.15)]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days, 'desc': f"SELL CALL ${r['strike']}",
                                'price_display': r['bid'], 'capital': current_price*100, 'roi': r['bid']/current_price,
                                'delta': r['delta'], 'breakeven': f"${current_price-r['bid']:.2f}",
                                'legs': [{'side':'SELL', 'type':'CALL', 'strike':r['strike']}]
                            })

                    elif strat_code == 'LONG_CALL':
                        df = calls[(calls['delta'] > 0.4) & (calls['delta'] < 0.6)]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days, 'desc': f"BUY CALL ${r['strike']}",
                                'price_display': r['ask'], 'capital': r['ask']*100, 'roi': (current_price/r['ask'])*0.5,
                                'delta': r['delta'], 'breakeven': f"${r['strike']+r['ask']:.2f}",
                                'legs': [{'side':'BUY', 'type':'CALL', 'strike':r['strike']}]
                            })
                            
                    elif strat_code == 'LONG_PUT':
                        df = puts[(puts['delta'] > -0.6) & (puts['delta'] < -0.4)]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days, 'desc': f"BUY PUT ${r['strike']}",
                                'price_display': r['ask'], 'capital': r['ask']*100, 'roi': (current_price/r['ask'])*0.5,
                                'delta': r['delta'], 'breakeven': f"${r['strike']-r['ask']:.2f}",
                                'legs': [{'side':'BUY', 'type':'PUT', 'strike':r['strike']}]
                            })

                    elif strat_code == 'LEAPS_CALL':
                        df = calls[calls['delta'] > 0.85]
                        for _, r in df.iterrows():
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days, 'desc': f"BUY LEAPS ${r['strike']}",
                                'price_display': r['ask'], 'capital': r['ask']*100, 'roi': 0,
                                'delta': r['delta'], 'breakeven': f"${r['strike']+r['ask']:.2f}",
                                'legs': [{'side':'BUY', 'type':'CALL', 'strike':r['strike']}]
                            })

                    # 2. 垂直价差 (重点修复：补全 days_to_exp)
                    elif strat_code == 'BULL_PUT':
                        shorts = puts[(puts['delta'] > -0.4) & (puts['delta'] < -0.2)]
                        res = build_spread(puts, shorts, spread_width, 'credit')
                        if not res.empty:
                            for _, r in res.iterrows():
                                # 修复：手动添加日期信息到字典
                                r_dict = r.to_dict()
                                r_dict.update({'expiration_date': date, 'days_to_exp': days})
                                all_opps.append(r_dict)

                    elif strat_code == 'BEAR_CALL':
                        shorts = calls[(calls['delta'] < 0.4) & (calls['delta'] > 0.2)]
                        res = build_spread(calls, shorts, spread_width, 'credit')
                        if not res.empty:
                            for _, r in res.iterrows():
                                # 修复：手动添加日期信息到字典
                                r_dict = r.to_dict()
                                r_dict.update({'expiration_date': date, 'days_to_exp': days})
                                all_opps.append(r_dict)

                    # 3. 组合策略
                    elif strat_code == 'IRON_CONDOR':
                        p_s = puts[(puts['delta'] > -0.25) & (puts['delta'] < -0.15)]
                        c_s = calls[(calls['delta'] < 0.25) & (calls['delta'] > 0.15)]
                        p_spr = build_spread(puts, p_s, spread_width, 'credit')
                        c_spr = build_spread(calls, c_s, spread_width, 'credit')
                        if not p_spr.empty and not c_spr.empty:
                            # 简单取 Top 3 组合
                            p_list = p_spr.head(3).to_dict('records')
                            c_list = c_spr.head(3).to_dict('records')
                            
                            for p in p_list:
                                for c in c_list:
                                    net = p['price_display'] + c['price_display']
                                    loss = spread_width - net
                                    if loss > 0:
                                        all_opps.append({
                                            'expiration_date': date, 'days_to_exp': days,
                                            'desc': f"IC Put ${p['legs'][0]['strike']} / Call ${c['legs'][0]['strike']}",
                                            'price_display': net, 'capital': loss*100, 'roi': net/loss,
                                            'delta': p['delta'] + c['delta'], 
                                            'breakeven': f"${p['legs'][0]['strike']-net:.1f}/${c['legs'][0]['strike']+net:.1f}",
                                            'legs': p['legs'] + c['legs']
                                        })

                    elif strat_code == 'JADE_LIZARD':
                        s_p = puts[(puts['delta'] > -0.3) & (puts['delta'] < -0.2)]
                        s_c = calls[(calls['delta'] < 0.3) & (calls['delta'] > 0.2)]
                        c_spr = build_spread(calls, s_c, spread_width, 'credit')
                        if not s_p.empty and not c_spr.empty:
                            p = s_p.iloc[0]; 
                            c_list = c_spr.to_dict('records')
                            for c in c_list:
                                net = p['bid'] + c['price_display']
                                if net > spread_width - 0.5:
                                    all_opps.append({
                                        'expiration_date': date, 'days_to_exp': days,
                                        'desc': f"JL Put ${p['strike']} + Call Spr ${c['legs'][0]['strike']}",
                                        'price_display': net, 'capital': p['strike']*100*0.2, 'roi': 0,
                                        'delta': p['delta'] + c['delta'], 'breakeven': f"Down ${p['strike']-net:.1f}",
                                        'legs': [{'side':'SELL','type':'PUT','strike':p['strike']}] + c['legs']
                                    })

                    elif strat_code == 'RATIO':
                        l_c = calls[(calls['delta'] > 0.55) & (calls['delta'] < 0.65)]
                        for _, l in l_c.iterrows():
                            target = l['strike'] + spread_width
                            s_c = calls[abs(calls['strike'] - target) < 1.0]
                            if not s_c.empty:
                                s = s_c.iloc[0]
                                net = s['bid']*2 - l['ask']
                                if net > -0.5:
                                    all_opps.append({
                                        'expiration_date': date, 'days_to_exp': days,
                                        'desc': f"Ratio Buy ${l['strike']} / Sell 2x ${s['strike']}",
                                        'price_display': net, 'capital': s['strike']*100*0.2, 'roi': 0,
                                        'delta': l['delta'] - 2*s['delta'], 'breakeven': "Unlimited Downside Risk",
                                        'legs': [{'side':'BUY','type':'CALL','strike':l['strike']}, {'side':'SELL x2','type':'CALL','strike':s['strike']}]
                                    })
                                    
                    elif strat_code == 'STRADDLE':
                        atm_c = calls.iloc[(calls['delta'] - 0.5).abs().argsort()[:1]]
                        atm_p = puts.iloc[(puts['delta'].abs() - 0.5).abs().argsort()[:1]]
                        if not atm_c.empty:
                            c = atm_c.iloc[0]; p = atm_p.iloc[0]
                            cost = c['ask'] + p['ask']
                            all_opps.append({
                                'expiration_date': date, 'days_to_exp': days,
                                'desc': f"STRADDLE ${c['strike']}",
                                'price_display': cost, 'capital': cost*100, 'roi': 0,
                                'delta': c['delta'] + p['delta'], 'breakeven': f"±${cost:.2f}",
                                'legs': [{'side':'BUY','type':'CALL','strike':c['strike']}, {'side':'BUY','type':'PUT','strike':p['strike']}]
                            })

                except: continue

        if not all_opps: return None, current_price, history, next_earnings, "未扫描到符合策略逻辑的期权"
        df = pd.DataFrame(all_opps)
        # 年化处理
        df['annualized_return'] = df.apply(lambda x: x['roi'] * (365/x['days_to_exp']) if x['roi']>0 else 0, axis=1)
        return df, current_price, history, next_earnings, None

    except Exception as e: return None, 0, None, None, f"API 错误: {str(e)}"

def render_chart(history_df, ticker, r):
    fig = go.Figure(data=[go.Candlestick(x=history_df.index, open=history_df['Open'], high=history_df['High'], low=history_df['Low'], close=history_df['Close'], name=ticker)])
    cp = history_df['Close'].iloc[-1]
    fig.add_hline(y=cp, line_dash="dot", line_color="gray", annotation_text="现价")
    for leg in r['legs']:
        col = "red" if "SELL" in leg['side'] else "green"
        fig.add_hline(y=leg['strike'], line_color=col, line_dash="dash")
    fig.update_layout(height=350, margin=dict(l=20, r=20, t=20, b=20), xaxis_rangeslider_visible=False, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

# --- 4. 界面渲染 ---

with st.sidebar:
    st.header("包子类型")
    
    category = st.selectbox("1. 选择你爱的口味", [
        "现金流区 (Income)", 
        "方向博弈 (Speculation)", 
        "结构化/套利 (Advanced)", 
        "跨期时间 (Time)",
        "长期投资 (Long Term)"
    ])
    
    strat_map = {}
    if "现金流" in category:
        strat_map = {"CSP (卖Put)": "CSP", "CC (卖Call)": "CC", "Bull Put Spread": "BULL_PUT", "Bear Call Spread": "BEAR_CALL", "Iron Condor": "IRON_CONDOR"}
    elif "博弈" in category:
        strat_map = {"Long Call": "LONG_CALL", "Long Put": "LONG_PUT", "Straddle (双买)": "STRADDLE"}
    elif "结构化" in category:
        strat_map = {"Jade Lizard (玉蜥蜴)": "JADE_LIZARD", "Ratio Spread (比例)": "RATIO"}
    elif "跨期" in category:
        strat_map = {"PMCC (穷人盖楼)": "PMCC", "Calendar (日历)": "CALENDAR"}
    else:
        strat_map = {"LEAPS Call": "LEAPS_CALL"}

    s_name = st.radio("2. 选择具体包子", list(strat_map.keys()))
    strat_code = strat_map[s_name]
    
    spread_width = 5
    if strat_code in ['BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'JADE_LIZARD', 'RATIO']:
        spread_width = st.slider("价差宽度 / 间距", 1, 20, 5)

    st.divider()
    ticker = st.text_input("代码", value="NVDA").upper()
    strike_range_pct = st.slider("扫描范围", 5, 50, 20)
    
    if st.button("🚀 启动全能引擎", type="primary", use_container_width=True):
        st.cache_data.clear()

st.title(f"{ticker} 包子铺")

with st.spinner(f'正在构建 {s_name} 策略矩阵...'):
    df, current_price, history, next_earnings, err = fetch_market_data(ticker, strat_code, spread_width, strike_range_pct)

if err:
    st.error(err)
else:
    if "现金流" in category: best = df.sort_values('annualized_return', ascending=False).head(1)
    elif "博弈" in category: best = df.sort_values('roi', ascending=False).head(1) 
    elif "结构化" in category: best = df.sort_values('price_display', ascending=False).head(1)
    else: best = df.head(1)

    if not best.empty:
        r = best.iloc[0]
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("最佳战术指令")
            
            # 修复财报检测逻辑：安全地处理日期格式
            if next_earnings:
                try:
                    # 如果 expiration_date 包含 "Near" (跨期)，则取 Far Date 做检查
                    exp_str = r['expiration_date'].split(' / ')[-1] if ' / ' in r['expiration_date'] else r['expiration_date']
                    exp_str = exp_str.replace('Far: ', '').replace('Long: ', '')
                    exp_dt = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    
                    if next_earnings <= exp_dt: st.warning(f"⚠️ 包含财报风险 ({next_earnings})")
                    else: st.success("无财报风险")
                except:
                    pass

            st.markdown(f"**合约**: {r['expiration_date']}")
            
            for leg in r['legs']:
                c = "sell-leg" if "SELL" in leg['side'] else "buy-leg"
                st.markdown(f'<div class="trade-leg {c}">{leg["side"]} {leg["type"]} ${leg["strike"]}</div>', unsafe_allow_html=True)

        with c2:
            lbl = "净支出" if r['price_display'] > 0 and category in ["博弈", "跨期", "长期"] else "净收入" 
            st.metric("单张金额", f"${abs(r['price_display'])*100:.0f}")
            st.metric("资金/风险", f"${r['capital']:.0f}")
            st.metric("盈亏平衡", r['breakeven'])

    if history is not None:
        render_chart(history, ticker, r)
        
    st.divider()
    with st.expander("完整列表"):
        st.dataframe(df, use_container_width=True)


