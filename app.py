import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租工厂 (双策略版)", 
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
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days, strategy_type):
    """
    strategy_type: 'CSP' (卖Put) 或 'CC' (卖Call)
    """
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
                    # 卖Put: 找比现价低的 (OTM Puts)
                    options = opt.puts
                    # 筛选行权价 < 现价 * 1.05
                    options = options[options['strike'] < current_price * 1.05].copy()
                    # 安全垫计算: (现价 - 行权价) / 现价
                    options['distance_pct'] = (current_price - options['strike']) / current_price
                    # ROI 分母: 保证金 (行权价)
                    capital_required = options['strike']
                    
                else: # strategy_type == 'CC' (Covered Call)
                    # 卖Call: 找比现价高的 (OTM Calls)
                    options = opt.calls
                    # 筛选行权价 > 现价 * 0.95 (稍微给点容错)
                    options = options[options['strike'] > current_price * 0.95].copy()
                    # 上涨空间计算: (行权价 - 现价) / 现价
                    options['distance_pct'] = (options['strike'] - current_price) / current_price
                    # ROI 分母: 持仓成本 (假设为当前现价)
                    capital_required = current_price

                options['days_to_exp'] = days
                options['expiration_date'] = date
                options = options[options['bid'] > 0.01] 
                
                # 核心收益计算
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
    
    # 策略选择器
    strategy = st.radio(
        "选择你的持仓状态:",
        ("🟢 没货，想抄底收租 (CSP)", "🔴 有货，想止盈回血 (CC)"),
        captions=["策略: Cash-Secured Put", "策略: Covered Call"]
    )
    
    strat_code = 'CSP' if "CSP" in strategy else 'CC'

    st.divider()
    
    # 标的选择
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

# 主界面逻辑
if strat_code == 'CSP':
    st.title(f"📉 {ticker} 抄底收租 (Put)")
    dist_label = "安全垫 (跌幅保护)"
    dist_help = "股票跌多少以内，你都是赚的"
    color_theme = "inverse" # 进度条颜色逻辑
else:
    st.title(f"📈 {ticker} 持仓回血 (Call)")
    dist_label = "踏空垫 (上涨空间)"
    dist_help = "股票涨多少以内，股票不会被卖飞"
    color_theme = "normal"

with st.spinner(f'正在计算 {ticker} 的最佳 {strat_code} 策略...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte, strat_code)

if error_msg:
    st.error(f"出错啦: {error_msg}")
else:
    st.metric("📊 当前股价", f"${current_price:.2f}")

    # --- 智能推荐卡片 ---
    st.subheader("🤖 智能推荐 (Best Pick)")
    
    # 统一将 distance 转为百分比数值处理
    df_calc = df.copy()
    df_calc['dist_pct_val'] = df_calc['distance_pct'] * 100
    
    if strat_code == 'CSP':
        # Put: 离现价越远越安全 (安全垫大)
        aggressive = df_calc[(df_calc['dist_pct_val'] < 4) & (df_calc['dist_pct_val'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
        balanced = df_calc[(df_calc['dist_pct_val'] >= 4) & (df_calc['dist_pct_val'] < 8)].sort_values('annualized_return', ascending=False).head(1)
        safe = df_calc[df_calc['dist_pct_val'] >= 8].sort_values('annualized_return', ascending=False).head(1)
    else:
        # Call: 离现价越远越不容易卖飞 (上涨空间大)
        # 激进: 行权价就在现价附近，容易卖飞，但权利金高
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
    
    # 准备展示数据
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
                min_value=-0.05, # 允许稍微有点负数（价内）
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
