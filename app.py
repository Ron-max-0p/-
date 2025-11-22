import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# --- 1. 页面配置 ---
st.set_page_config(
    page_title="美股收租", 
    layout="wide", 
    page_icon="💰",
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
    /* 隐藏表格索引列 */
    thead tr th:first-child {display:none}
    tbody th {display:none}
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days):
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

        all_puts = []
        
        for date, days in valid_dates:
            try:
                opt = stock.option_chain(date)
                puts = opt.puts
                
                strike_threshold = current_price * 1.05 
                puts = puts[puts['strike'] < strike_threshold].copy()
                
                puts['days_to_exp'] = days
                puts['expiration_date'] = date
                puts['distance_pct'] = (current_price - puts['strike']) / current_price 
                puts = puts[puts['bid'] > 0.01] 
                
                puts['roi'] = puts['bid'] / puts['strike']
                puts['annualized_return'] = puts['roi'] * (365 / days)
                
                all_puts.append(puts)
            except Exception:
                continue

        if not all_puts:
            return None, current_price, "没有找到符合条件的 Put 合约"

        df = pd.concat(all_puts)
        return df, current_price, None

    except Exception as e:
        return None, 0, f"API 连接错误: {str(e)}"

# --- 4. 界面渲染区 ---

with st.sidebar:
    st.header("🛠️ 策略参数")
    
    preset_tickers = {
        "QQQ (纳指100)": "QQQ",
        "SPY (标普500)": "SPY",
        "NVDA (英伟达)": "NVDA",
        "TSLA (特斯拉)": "TSLA",
        "AAPL (苹果)": "AAPL",
        "MSFT (微软)": "MSFT",
        "AMZN (亚马逊)": "AMZN",
        "GOOGL (谷歌)": "GOOGL",
        "META (脸书)": "META",
        "MARA (比特币矿股)": "MARA",
        "自定义...": "CUSTOM"
    }
    
    selected_label = st.selectbox("选择热门标的", list(preset_tickers.keys()))
    
    if selected_label == "自定义...":
        ticker = st.text_input("输入股票代码", value="IWM").upper()
    else:
        ticker = preset_tickers[selected_label]
    
    st.caption(f"当前选中: {ticker}")
    st.divider()
    col_d1, col_d2 = st.columns(2)
    min_dte = col_d1.number_input("最近天数", value=14, step=1)
    max_dte = col_d2.number_input("最远天数", value=45, step=1)
    
    st.divider()
    if st.button("🔄 刷新数据", use_container_width=True, type="primary"):
        st.cache_data.clear()

# 主界面
st.title(f"💸 {ticker} 收租雷达")
st.markdown("通过 **Cash-Secured Put** 策略，寻找高性价比的权利金收入。")

with st.spinner(f'正在获取 {ticker} 实时数据...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte)

if error_msg:
    st.error(f"出错啦: {error_msg}")
else:
    st.metric("当前股价", f"${current_price:.2f}")

    # --- 智能推荐 ---
    st.subheader("最佳收租点位推荐")
    
    # 转换为百分比数值用于筛选
    df_calc = df.copy()
    df_calc['dist_pct_val'] = df_calc['distance_pct'] * 100
    
    aggressive = df_calc[(df_calc['dist_pct_val'] < 4) & (df_calc['dist_pct_val'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
    balanced = df_calc[(df_calc['dist_pct_val'] >= 4) & (df_calc['dist_pct_val'] < 8)].sort_values('annualized_return', ascending=False).head(1)
    safe = df_calc[df_calc['dist_pct_val'] >= 8].sort_values('annualized_return', ascending=False).head(1)

    tab1, tab2, tab3 = st.tabs(["激进型", "稳健型", "保守型"])

    def render_card(data):
        if data.empty:
            st.warning("暂无符合该策略的期权。")
            return
        row = data.iloc[0]
        with st.container():
            c1, c2 = st.columns([2, 1])
            with c1:
                st.markdown(f"**行权价**: :orange[${row['strike']}]")
                st.markdown(f"**到期日**: {row['expiration_date']} ({row['days_to_exp']}天)")
                st.markdown(f"**安全垫**: 下跌 {row['distance_pct']:.2%} 内不亏")
            with c2:
                # 修复颜色显示，确保高亮
                st.metric("年化收益率", f"{row['annualized_return']:.2%}")
            st.info(f"先拿权利金: **${row['bid']*100:.0f}**")

    with tab1: render_card(aggressive)
    with tab2: render_card(balanced)
    with tab3: render_card(safe)

    # --- 数据透视 (核心升级点) ---
    st.divider()
    with st.expander("🔎 查看所有机会 (已自动格式化)", expanded=True):
        
        # 准备数据
        display_df = df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']].copy()
        
        # 使用 column_config 强制定义列的属性
        # 这样用户不需要手动去点 Format，也就不太需要用那个英文菜单了
        st.dataframe(
            display_df,
            column_order=("expiration_date", "strike", "bid", "distance_pct", "annualized_return"),
            column_config={
                "expiration_date": st.column_config.DateColumn("到期日"),
                "strike": st.column_config.NumberColumn(
                    "行权价 (Strike)",
                    format="$%.1f", # 强制显示美元
                ),
                "bid": st.column_config.NumberColumn(
                    "权利金 (Bid)",
                    format="$%.2f", # 强制显示美元
                ),
                "distance_pct": st.column_config.ProgressColumn(
                    "安全垫 (跌幅保护)",
                    format="%.2f%%", # 强制显示百分比
                    min_value=0,
                    max_value=0.15, # 进度条最大值设为15%
                ),
                "annualized_return": st.column_config.NumberColumn(
                    "年化收益率 (ARP)",
                    format="%.2f%%", # 强制显示百分比
                ),
            },
            hide_index=True, # 隐藏讨厌的 0,1,2,3 索引列
            use_container_width=True,
            height=500
        )
