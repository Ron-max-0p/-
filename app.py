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
    /* 调整表格字体大小 */
    .stDataFrame { font-size: 1.1rem; }
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300)
def fetch_market_data(ticker, min_days, max_days):
    try:
        stock = yf.Ticker(ticker)
        history = stock.history(period="1d")
        if history.empty:
            return None, 0, "无法获取股价数据，请检查代码是否正确"
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
                
                # 筛选逻辑
                strike_threshold = current_price * 1.05 
                puts = puts[puts['strike'] < strike_threshold].copy()
                
                # 计算字段
                puts['days_to_exp'] = days
                puts['expiration_date'] = date
                puts['distance_pct'] = (current_price - puts['strike']) / current_price * 100
                puts = puts[puts['bid'] > 0.01] 
                
                puts['roi'] = puts['bid'] / puts['strike']
                puts['annualized_return'] = puts['roi'] * (365 / days) * 100
                
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

# 侧边栏
with st.sidebar:
    st.header("🛠️ 策略参数")
    
    # --- 新增：热门标的下拉菜单 ---
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

with st.spinner(f'正在分析 {ticker} 的期权链数据...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte)

if error_msg:
    st.error(f"出错啦: {error_msg}")
else:
    st.metric("📊 当前股价", f"${current_price:.2f}")

    # --- 智能推荐卡片 ---
    st.subheader("最佳收租点位推荐")
    
    aggressive = df[(df['distance_pct'] < 4) & (df['distance_pct'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
    balanced = df[(df['distance_pct'] >= 4) & (df['distance_pct'] < 8)].sort_values('annualized_return', ascending=False).head(1)
    safe = df[df['distance_pct'] >= 8].sort_values('annualized_return', ascending=False).head(1)

    tab1, tab2, tab3 = st.tabs(["激进", "稳健", "保守"])

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
                st.markdown(f"**安全垫**: 下跌 {row['distance_pct']:.1f}% 内不亏")
            with c2:
                st.metric("年化收益率", f"{row['annualized_return']:.1f}%", delta="预估")
            st.info(f"💰 先拿权利金: **${row['bid']*100:.0f}**")

    with tab1: render_card(aggressive)
    with tab2: render_card(balanced)
    with tab3: render_card(safe)

    # --- 数据透视 (汉化处理) ---
    st.divider()
    with st.expander("🔎 查看所有机会 (汉化完整表)", expanded=True):
        
        # 1. 提取需要的列
        display_df = df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']].copy()
        
        # 2. 改名 (汉化关键步骤)
        display_df.columns = ['到期日', '行权价', '权利金(Bid)', '安全垫(%)', '年化收益率(%)']
        
        # 3. 排序并展示
        st.dataframe(
            display_df.sort_values('年化收益率(%)', ascending=False).style.format({
                '权利金(Bid)': '${:.2f}',
                '安全垫(%)': '{:.2f}%',
                '年化收益率(%)': '{:.2f}%',
                '行权价': '${:.1f}'
            }),
            use_container_width=True,
            height=500 
        )
