import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (Pro版)", 
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

# --- 3. 核心逻辑区 (策略引擎) ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strategy_type, spread_width=5):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="1d")
        if history.empty: return None, 0, "无法获取股价"
        current_price = history['Close'].iloc[-1]
        
        expirations = stock.options
        if not expirations: return None, current_price, "无期权链数据"

        valid_dates = []
        today = datetime.now().date()
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            if min_days <= days_to_exp <= max_days:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates: return None, current_price, "选定范围内无到期日"

        all_opportunities = []
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                calls = opt.calls
                puts = opt.puts
                
                # --- 策略分支 ---
                
                if strategy_type == 'CSP': # 单腿 Put
                    candidates = puts[puts['strike'] < current_price * 1.05].copy()
                    candidates['distance_pct'] = (current_price - candidates['strike']) / current_price
                    candidates['capital'] = candidates['strike'] * 100 # 保证金是行权价
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'CC': # 单腿 Call
                    candidates = calls[calls['strike'] > current_price * 0.95].copy()
                    candidates['distance_pct'] = (candidates['strike'] - current_price) / current_price
                    candidates['capital'] = current_price * 100 # 成本是正股
                    candidates['credit'] = candidates['bid']
                    candidates['roi'] = candidates['credit'] * 100 / candidates['capital']
                    
                elif strategy_type == 'SPREAD': # 垂直价差 (Bull Put Spread)
                    # 1. 找卖单 (Short Leg) - 类似 CSP
                    shorts = puts[puts['strike'] < current_price].copy()
                    
                    spreads = []
                    for index, short_row in shorts.iterrows():
                        # 2. 找买单 (Long Leg) - 比卖单行权价更低，作为保护
                        target_long_strike = short_row['strike'] - spread_width
                        
                        # 在期权链里找最接近 target_long_strike 的合约
                        long_candidates = puts[abs(puts['strike'] - target_long_strike) < 0.5]
                        
                        if not long_candidates.empty:
                            long_row = long_candidates.iloc[0]
                            
                            # 计算价差核心数据
                            net_credit = short_row['bid'] - long_row['ask'] # 卖价 - 买价
                            
                            if net_credit > 0.01: # 必须有钱赚才算
                                max_loss = spread_width - net_credit # 最大亏损 = 价差宽 - 权利金
                                
                                spread_data = {
                                    'strike': f"{short_row['strike']} / {long_row['strike']}",
                                    'short_strike': short_row['strike'],
                                    'bid': net_credit, # 这里的 bid 指的是净权利金
                                    'distance_pct': (current_price - short_row['strike']) / current_price,
                                    'capital': max_loss * 100, # 保证金 = 最大亏损
                                    'roi': net_credit / max_loss
                                }
                                spreads.append(spread_data)
                    
                    if spreads:
                        candidates = pd.DataFrame(spreads)
                    else:
                        continue

                # --- 通用计算 ---
                if not candidates.empty:
                    candidates['days_to_exp'] = days
                    candidates['expiration_date'] = date
                    # 统一去掉无效数据
                    candidates = candidates[candidates['bid'] > 0.01] 
                    candidates['annualized_return'] = candidates['roi'] * (365 / days)
                    all_opportunities.append(candidates)
                    
            except Exception:
                continue

        if not all_opportunities: return None, current_price, "没有找到符合条件的合约"

        df = pd.concat(all_opportunities)
        return df, current_price, None

    except Exception as e:
        return None, 0, f"API 错误: {str(e)}"

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🏭 策略军火库")
    
    # 策略分类
    cat_map = {
        "🔰 入门收租 (单腿)": ["CSP (现金担保Put)", "CC (持股备兑Call)"],
        "🚀 进阶杠杆 (垂直价差)": ["Bull Put Spread (牛市看跌价差)"]
    }
    category = st.selectbox("选择策略等级", list(cat_map.keys()))
    strategy_name = st.selectbox("选择具体策略", cat_map[category])
    
    # 参数映射
    if "CSP" in strategy_name: strat_code = 'CSP'
    elif "CC" in strategy_name: strat_code = 'CC'
    else: strat_code = 'SPREAD'
    
    # 价差专属参数
    spread_width = 5
    if strat_code == 'SPREAD':
        st.info("💡 价差策略：用小资金博取高收益，但需要买一张低价Put做保护。")
        spread_width = st.slider("价差宽度 (保护层厚度)", 1, 20, 5, help="卖出价和买入价之间的距离。越宽风险越高，收益越高。")

    st.divider()
    
    preset_tickers = {
        "NVDA (英伟达)": "NVDA", "TSLA (特斯拉)": "TSLA", "QQQ (纳指)": "QQQ", 
        "SPY (标普)": "SPY", "MSTR (微策略)": "MSTR", "COIN (Coinbase)": "COIN"
    }
    ticker_key = st.selectbox("选择标的", list(preset_tickers.keys()) + ["自定义..."])
    ticker = st.text_input("输入代码", value="AMD").upper() if ticker_key == "自定义..." else preset_tickers[ticker_key]
    
    st.divider()
    c1, c2 = st.columns(2)
    min_dte = c1.number_input("最近天数", 14)
    max_dte = c2.number_input("最远天数", 45)
    
    if st.button("🚀 扫描机会", type="primary", use_container_width=True):
        st.cache_data.clear()

# --- 主界面 ---

st.title(f"📊 {ticker} 策略分析")

# 说明书逻辑
expander_title = "📖 策略说明书 (点击展开)"
help_text = ""
if strat_code == 'CSP':
    help_text = "### 🟢 Cash-Secured Put\n我是土豪，我有钱。如果跌到行权价，我愿意全款买入股票。"
elif strat_code == 'CC':
    help_text = "### 🔴 Covered Call\n我有股票。如果涨到行权价，我愿意卖出股票止盈。"
else:
    help_text = f"### 🚀 Bull Put Spread (垂直价差)\n**我不想占用几万块买股，我想用小资金收租。**\n\n* **操作**：卖出一个贵的Put，同时买入一个便宜的Put（低 ${spread_width}）。\n* **优点**：保证金极低（只需锁住 ${spread_width*100}）。\n* **缺点**：如果大跌，最大亏损被锁定，但权利金也少一点。"

with st.expander(expander_title):
    st.markdown(help_text)

with st.spinner('正在连接交易所数据...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte, strat_code, spread_width)

if error_msg:
    st.error(error_msg)
else:
    st.metric("当前股价", f"${current_price:.2f}")

    # 智能筛选逻辑
    st.subheader("🤖 AI 智能优选")
    
    df['score_val'] = df['distance_pct'] * 100
    
    if strat_code == 'SPREAD':
        # 价差策略看重 ROI，因为本金小，ROI通常很高
        rec_col = 'annualized_return'
        aggressive = df[df['score_val'] < 3].sort_values(rec_col, ascending=False).head(1)
        balanced = df[(df['score_val'] >= 3) & (df['score_val'] < 8)].sort_values(rec_col, ascending=False).head(1)
        safe = df[df['score_val'] >= 8].sort_values(rec_col, ascending=False).head(1)
    else:
        # 单腿策略逻辑
        aggressive = df[(df['score_val'] < 4) & (df['score_val'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
        balanced = df[(df['score_val'] >= 4) & (df['score_val'] < 8)].sort_values('annualized_return', ascending=False).head(1)
        safe = df[df['score_val'] >= 8].sort_values('annualized_return', ascending=False).head(1)

    c1, c2, c3 = st.columns(3)
    
    def show_card(col, title, row, color):
        if row.empty:
            col.info("暂无")
            return
        r = row.iloc[0]
        strike_display = r['strike']
        # 格式化显示
        col.markdown(f"##### {title}")
        col.markdown(f"**行权**: :blue[{strike_display}]")
        col.markdown(f"**年化**: :{color}[{r['annualized_return']:.1%}]")
        col.caption(f"保证金: ${r['capital']:.0f} | 净收入: ${r['bid']*100:.0f}")

    show_card(c1, "🔥 激进型", aggressive, "red")
    show_card(c2, "⚖️ 均衡型", balanced, "orange")
    show_card(c3, "🛡️ 稳健型", safe, "green")

    # 列表展示
    st.divider()
    st.subheader("📋 详细列表")
    
    cols_config = {
        "expiration_date": st.column_config.DateColumn("到期日"),
        "strike": st.column_config.TextColumn("行权价 (卖/买)"),
        "bid": st.column_config.NumberColumn("净权利金", format="$%.2f"),
        "distance_pct": st.column_config.ProgressColumn("安全垫", format="%.2f%%", min_value=-0.1, max_value=0.2),
        "capital": st.column_config.NumberColumn("保证金(风险)", format="$%.0f"),
        "annualized_return": st.column_config.NumberColumn("年化收益率", format="%.2f%%"),
    }
    
    st.dataframe(
        df[['expiration_date', 'strike', 'bid', 'distance_pct', 'capital', 'annualized_return']],
        column_config=cols_config,
        use_container_width=True,
        hide_index=True,
        height=500
    )
