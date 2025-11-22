import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (说明书版)", 
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
    /* 隐藏表格索引 */
    thead tr th:first-child {display:none}
    tbody th {display:none}
    /* 说明书样式微调 */
    .streamlit-expanderHeader {
        font-weight: bold;
        color: #FF4B4B;
    }
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strategy_type):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="1d")
        if history.empty:
            return None, 0, "无法获取股价数据"
        current_price = history['Close'].iloc[-1]
        
        expirations = stock.options
        if not expirations:
            return None, current_price, "该标的没有期权链数据"

        valid_dates = []
        today = datetime.now().date()
        
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            if min_days <= days_to_exp <= max_days:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates:
            return None, current_price, "选定范围内无到期日"

        all_options = []
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                
                if strategy_type == 'CSP':
                    options = opt.puts
                    options = options[options['strike'] < current_price * 1.05].copy()
                    options['distance_pct'] = (current_price - options['strike']) / current_price
                    capital_required = options['strike']
                    
                else: 
                    options = opt.calls
                    options = options[options['strike'] > current_price * 0.95].copy()
                    options['distance_pct'] = (options['strike'] - current_price) / current_price
                    capital_required = current_price

                options['days_to_exp'] = days
                options['expiration_date'] = date
                options = options[options['bid'] > 0.01] 
                
                options['roi'] = options['bid'] / capital_required
                options['annualized_return'] = options['roi'] * (365 / days)
                
                all_options.append(options)
            except Exception:
                continue

        if not all_options:
            return None, current_price, "没有找到符合条件的合约"

        df = pd.concat(all_options)
        return df, current_price, None

    except Exception as e:
        return None, 0, f"API 连接错误: {str(e)}"

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🏭 策略工厂")
    
    strategy = st.radio(
        "选择你的持仓状态:",
        ("🟢 没货，想抄底收租 (CSP)", "🔴 有货，想止盈回血 (CC)"),
        captions=["Cash-Secured Put", "Covered Call"]
    )
    strat_code = 'CSP' if "CSP" in strategy else 'CC'

    st.divider()
    
    preset_tickers = {
        "QQQ (纳指100)": "QQQ",
        "SPY (标普500)": "SPY",
        "NVDA (英伟达)": "NVDA",
        "TSLA (特斯拉)": "TSLA",
        "AAPL (苹果)": "AAPL",
        "MSFT (微软)": "MSFT",
        "MARA (比特币矿股)": "MARA",
        "COIN (Coinbase)": "COIN",
        "自定义...": "CUSTOM"
    }
    selected_label = st.selectbox("选择标的", list(preset_tickers.keys()))
    if selected_label == "自定义...":
        ticker = st.text_input("输入股票代码", value="AMD").upper()
    else:
        ticker = preset_tickers[selected_label]
    
    st.divider()
    col_d1, col_d2 = st.columns(2)
    min_dte = col_d1.number_input("最近天数", value=14, step=1)
    max_dte = col_d2.number_input("最远天数", value=45, step=1)
    
    if st.button("🔄 运行策略", use_container_width=True, type="primary"):
        st.cache_data.clear()

# --- 主界面 ---

st.title(f"💸 {ticker} 收租雷达")

# >>>>>>> 这里是新加的产品说明书 <<<<<<<
with st.expander("📖 产品说明书 / 新手指南 (点击展开)", expanded=False):
    st.markdown("""
    ### 欢迎使用美股收租工厂 (The Option Wheel)
    本工具旨在帮助投资者寻找**高胜率**的期权收租机会。请根据您的持仓情况选择模式：
    
    #### 1️⃣ 模式一：🟢 没货 (Cash-Secured Put)
    * **适用场景**：你现在持有现金，想以打折价买入股票，或者单纯想赚点权利金。
    * **核心逻辑**：作为“保险公司”，承诺在未来以**行权价**接盘股票。
    * **最好情况**：股价没跌破行权价 -> **白赚权利金**。
    * **最坏情况**：股价大跌 -> 你必须以行权价买入股票（此时你的持仓成本 = 行权价 - 权利金）。
    * **指标解释**：
        * `安全垫`：股价还要跌多少你才开始亏损。
    
    #### 2️⃣ 模式二：🔴 有货 (Covered Call)
    * **适用场景**：你已经被套了，或者长期持有正股，想在持有的同时赚外快。
    * **核心逻辑**：承诺在未来如果股价涨得太高，就以**行权价**卖出股票。
    * **最好情况**：股价没涨到行权价 -> **股票还在，白赚权利金**。
    * **最坏情况**：股价暴涨 -> 股票被行权价卖飞（少赚了暴涨的部分，但没亏钱）。
    * **指标解释**：
        * `踏空垫`：股价还能涨多少才会被强制卖出。
    
    ---
    ⚠️ **风险提示**：本工具仅基于数学模型进行筛选，不构成投资建议。期权交易存在风险，请结合财报日期和技术面综合判断。
    """)

# 动态标题逻辑
if strat_code == 'CSP':
    dist_label = "安全垫 (跌幅保护)"
    dist_help = "股票跌多少以内，你都是赚的"
else:
    dist_label = "踏空垫 (上涨空间)"
    dist_help = "股票涨多少以内，股票不会被卖飞"

with st.spinner(f'正在计算 {ticker} 的最佳 {strat_code} 策略...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte, strat_code)

if error_msg:
    st.error(f"出错啦: {error_msg}")
else:
    st.metric("📊 当前股价", f"${current_price:.2f}")

    # --- 智能推荐 ---
    st.subheader("🤖 智能推荐")
    
    df_calc = df.copy()
    df_calc['dist_pct_val'] = df_calc['distance_pct'] * 100
    
    if strat_code == 'CSP':
        aggressive = df_calc[(df_calc['dist_pct_val'] < 4) & (df_calc['dist_pct_val'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
        balanced = df_calc[(df_calc['dist_pct_val'] >= 4) & (df_calc['dist_pct_val'] < 8)].sort_values('annualized_return', ascending=False).head(1)
        safe = df_calc[df_calc['dist_pct_val'] >= 8].sort_values('annualized_return', ascending=False).head(1)
    else:
        aggressive = df_calc[(df_calc['dist_pct_val'] < 3) & (df_calc['dist_pct_val'] >= 0)].sort_values('annualized_return', ascending=False).head(1)
        balanced = df_calc[(df_calc['dist_pct_val'] >= 3) & (df_calc['dist_pct_val'] < 7)].sort_values('annualized_return', ascending=False).head(1)
        safe = df_calc[df_calc['dist_pct_val'] >= 7].sort_values('annualized_return', ascending=False).head(1)

    c1, c2, c3 = st.columns(3)

    def render_mini_card(col, title, data, tag_color):
        if not data.empty:
            row = data.iloc[0]
            col.markdown(f"##### {title}")
            col.markdown(f"**行权价**: :blue[${row['strike']}]")
            col.markdown(f"**年化**: :{tag_color}[{row['annualized_return']:.1%}]")
            col.caption(f"到期: {row['expiration_date']} | 权利金: ${row['bid']*100:.0f}")
        else:
            col.info(f"{title} 暂无")

    render_mini_card(c1, "🔥 激进 (高收益)", aggressive, "red")
    render_mini_card(c2, "⚖️ 平衡 (推荐)", balanced, "orange")
    render_mini_card(c3, "🛡️ 保守 (稳健)", safe, "green")

    # --- 数据表格 ---
    st.divider()
    st.subheader(f"📋 策略详情 ({strat_code})")
    
    display_df = df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']].copy()
    
    st.dataframe(
        display_df,
        column_order=("expiration_date", "strike", "bid", "distance_pct", "annualized_return"),
        column_config={
            "expiration_date": st.column_config.DateColumn("到期日"),
            "strike": st.column_config.NumberColumn(
                "行权价", format="$%.1f"
            ),
            "bid": st.column_config.NumberColumn(
                "权利金", format="$%.2f"
            ),
            "distance_pct": st.column_config.ProgressColumn(
                dist_label,
                help=dist_help,
                format="%.2f%%",
                min_value=-0.05,
                max_value=0.15,
            ),
            "annualized_return": st.column_config.NumberColumn(
                "年化收益率", format="%.2f%%"
            ),
        },
        hide_index=True,
        use_container_width=True,
        height=600
    )
