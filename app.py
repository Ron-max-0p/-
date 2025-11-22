import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import numpy as np

# --- 1. 页面配置 (必须是第一行) ---
st.set_page_config(
    page_title="QQQ 收租雷达", 
    layout="wide", 
    page_icon="💸",
    initial_sidebar_state="expanded"
)

# --- 2. 自定义 CSS (让界面看起来更像 App 而不是学术论文) ---
st.markdown("""
<style>
    .metric-card {
        background-color: #1E1E1E;
        border: 1px solid #333;
        padding: 20px;
        border-radius: 10px;
        margin-bottom: 10px;
    }
    .stProgress > div > div > div > div {
        background-color: #00CC96;
    }
    /* 手机端优化字体 */
    h1 { font-size: 1.8rem !important; }
    h2 { font-size: 1.5rem !important; }
    h3 { font-size: 1.2rem !important; }
</style>
""", unsafe_allow_html=True)

# --- 3. 核心逻辑区 ---

@st.cache_data(ttl=300) # 关键升级：数据缓存5分钟，避免频繁请求导致卡顿
def fetch_market_data(ticker, min_days, max_days):
    """
    获取市场数据并进行清洗，带有缓存机制。
    """
    try:
        stock = yf.Ticker(ticker)
        # 获取实时价格 (尝试多种字段防止报错)
        history = stock.history(period="1d")
        if history.empty:
            return None, 0, "无法获取股价数据"
        current_price = history['Close'].iloc[-1]
        
        # 获取期权链日期
        expirations = stock.options
        if not expirations:
            return None, current_price, "该标的没有期权链数据"

        valid_dates = []
        today = datetime.now().date()
        
        # 筛选日期
        for date_str in expirations:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_to_exp = (exp_date - today).days
            if min_days <= days_to_exp <= max_days:
                valid_dates.append((date_str, days_to_exp))
        
        if not valid_dates:
            return None, current_price, "选定范围内无到期日"

        all_puts = []
        
        # 只需要抓取 OTM (价外) Put，为了速度
        for date, days in valid_dates:
            try:
                # 优化：只获取 put 数据
                opt = stock.option_chain(date)
                puts = opt.puts
                
                # 核心筛选逻辑
                strike_threshold = current_price * 1.05 # 稍微放宽一点范围
                puts = puts[puts['strike'] < strike_threshold].copy()
                
                # 计算字段
                puts['days_to_exp'] = days
                puts['expiration_date'] = date
                puts['distance_pct'] = (current_price - puts['strike']) / current_price * 100
                
                # 排除极度深虚值（保护计算不出错）
                puts = puts[puts['bid'] > 0.01] 
                
                # ROI 和 年化
                puts['roi'] = puts['bid'] / puts['strike']
                puts['annualized_return'] = puts['roi'] * (365 / days) * 100
                
                all_puts.append(puts)
            except Exception:
                continue # 跳过某个坏数据的日期，不中断程序

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
    ticker = st.text_input("标的代码", value="QQQ").upper()
    col_d1, col_d2 = st.columns(2)
    min_dte = col_d1.number_input("最近天数", value=14, step=1)
    max_dte = col_d2.number_input("最远天数", value=45, step=1)
    st.caption("提示：一般 30-45 天是 Theta 衰减最舒适的区域。")
    
    st.divider()
    if st.button("🔄 刷新数据 (Refresh)", use_container_width=True, type="primary"):
        st.cache_data.clear() # 清除缓存，强制刷新

# 主界面
st.title(f"💸 {ticker} 收租雷达")
st.markdown("通过 **Cash-Secured Put** 策略，寻找高性价比的权利金收入。")

# 加载状态提示
with st.spinner(f'正在分析 {ticker} 的期权链数据...'):
    df, current_price, error_msg = fetch_market_data(ticker, min_dte, max_dte)

if error_msg:
    st.error(f"出错啦: {error_msg}")
else:
    # 顶部关键指标
    st.metric("📊 当前股价", f"${current_price:.2f}")

    # --- 智能推荐卡片 (模拟 App 界面) ---
    st.subheader("🎯 最佳收租点位推荐")
    
    # 算法筛选
    # 激进：缓冲 < 4%
    # 稳健：缓冲 4% - 8%
    # 保守：缓冲 > 8%
    
    aggressive = df[(df['distance_pct'] < 4) & (df['distance_pct'] > 0.5)].sort_values('annualized_return', ascending=False).head(1)
    balanced = df[(df['distance_pct'] >= 4) & (df['distance_pct'] < 8)].sort_values('annualized_return', ascending=False).head(1)
    safe = df[df['distance_pct'] >= 8].sort_values('annualized_return', ascending=False).head(1)

    tab1, tab2, tab3 = st.tabs(["🔥 激进型 (高收益)", "⚖️ 稳健型 (推荐)", "🛡️ 保守型 (安全)"])

    def render_card(data, tag):
        if data.empty:
            st.warning("暂无符合该策略的期权。")
            return
        
        row = data.iloc[0]
        # 使用容器美化
        with st.container():
            c1, c2 = st.columns([2, 1])
            with c1:
                st.markdown(f"**行权价 Strike**: :orange[${row['strike']}]")
                st.markdown(f"**到期日**: {row['expiration_date']} ({row['days_to_exp']}天)")
                st.markdown(f"**安全垫**: 下跌 {row['distance_pct']:.1f}% 内不亏")
            with c2:
                st.metric("年化收益率", f"{row['annualized_return']:.1f}%", delta="预估")
            
            st.info(f"💰 每卖一张合约，先拿 **${row['bid']*100:.0f}** 权利金。")

    with tab1:
        render_card(aggressive, "激进")
    with tab2:
        render_card(balanced, "稳健")
    with tab3:
        render_card(safe, "保守")

    # --- 数据透视 ---
    st.divider()
    with st.expander("🔎 查看所有机会 (完整列表)"):
        st.dataframe(
            df[['expiration_date', 'strike', 'bid', 'distance_pct', 'annualized_return']]
            .sort_values('annualized_return', ascending=False)
            .style.format({
                'bid': '${:.2f}',
                'distance_pct': '{:.2f}%',
                'annualized_return': '{:.2f}%'
            }),
            use_container_width=True
        )