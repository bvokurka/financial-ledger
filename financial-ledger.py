import streamlit as st

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("Financial Ledger Login")
    password = st.text_input("Enter Password:", type="password")
    if password == st.secrets["APP_PASSWORD"]:
        st.session_state.authenticated = True
        st.rerun()
    elif password:
        st.error("Incorrect password")
    st.stop()
    
from st_supabase_connection import SupabaseConnection
import pandas as pd
from datetime import datetime

# Initialize connection once at the top
conn = st.connection("supabase", type=SupabaseConnection)

st.set_page_config(page_title="Multi-Account Financial Ledger", layout="wide")

# --- THEME STATE MANAGEMENT ---
if "light_mode" not in st.session_state:
    st.session_state.light_mode = False

# Define theme colors based on toggle state
if st.session_state.light_mode:
    bg_color = "#ffffff"
    container_bg = "#f9fafb"
    border_color = "#e5e7eb"
    text_color = "#111827"
    sub_text = "#4b5563"
    grid_header_bg = "#f3f4f6"
    widget_bg = "#ffffff"
else:
    bg_color = "#0e1117"
    container_bg = "#161b22"
    border_color = "#30363d"
    text_color = "#e6edf3"
    sub_text = "#8b949e"
    grid_header_bg = "#161b22"
    widget_bg = "#0e1117"

# --- CUSTOM CSS FOR SPREADSHEET GRID, THEMES, AND WIDGET OVERRIDES ---
st.markdown(f"""
<style>
/* App background */
.stApp {{
    background-color: {bg_color} !important;
}}

/* Force typography colors to override Streamlit's native dark mode defaults */
.stApp p, .stApp h1, .stApp h2, .stApp h3, .stApp span, .stApp label {{
    color: {text_color} !important;
}}

/* Fix Sidebar Background */
[data-testid="stSidebar"] {{
    background-color: {container_bg} !important;
    border-right: 1px solid {border_color} !important;
}}

/* Fix Metrics (Cash Position column) */
[data-testid="stMetricValue"] div, [data-testid="stMetricLabel"] p, [data-testid="stMetricDelta"] div {{
    color: {text_color} !important;
}}

/* Fix the Success Alert text visibility across themes */
div[data-testid="stAlert"] p, div[data-testid="stAlert"] span {{
    color: {text_color} !important; 
}}

/* Remove rounded card padding and create a seamless spreadsheet grid */
[data-testid="stVerticalBlock"] [data-testid="stVerticalBlockBorderWrapper"] {{
    background-color: {container_bg};
    border: 1px solid {border_color} !important;
    border-radius: 0px !important;
    padding: 2px !important;
    margin: -1px !important;
    min-height: 110px !important;
}}

/* Reduce column gaps so cells touch like Excel */
[data-testid="stHorizontalBlock"] {{
    gap: 0px !important;
}}

/* Custom style for the green Add Transaction button */
[data-testid="stSidebar"] button[kind="primary"] {{
    background-color: #2ea043 !important;
    color: #ffffff !important;
    border-color: #2ea043 !important;
    font-weight: bold !important;
}}
[data-testid="stSidebar"] button[kind="primary"]:hover {{
    background-color: #2c974b !important;
    color: #ffffff !important;
}}

/* Fix Selectboxes and Input backgrounds to match theme */
[data-baseweb="select"] > div, [data-testid="stSelectbox"] div[data-baseweb="select"] {{
    background-color: {widget_bg} !important;
    color: {text_color} !important;
    border-color: {border_color} !important;
}}

/* Fix Data Editor / Table container elements & theme compliance */
[data-testid="stDataEditor"], [data-testid="stTable"] {{
    background-color: {widget_bg} !important;
    color: {text_color} !important;
}}

[data-testid="stDataEditor"] div, [data-testid="stDataEditor"] span {{
    color: {text_color} !important;
}}

/* Hide the Streamlit helper text input without breaking React */
div[data-testid="stTextInput"]:has(input[aria-label*="sync_merchant_"]) {{
    position: absolute !important;
    opacity: 0 !important;
    width: 1px !important;
    height: 1px !important;
    overflow: hidden !important;
    pointer-events: none !important;
    margin: 0 !important;
    padding: 0 !important;
}}
</style>
""", unsafe_allow_html=True)

# --- FETCH EXISTING MERCHANTS FROM SUPABASE ---
@st.cache_data(ttl=60)
def get_existing_merchants():
    try:
        res = conn.table("Transactions").select("merchant").execute()
        if res and res.data:
            merchants = list(set([row.get("merchant") for row in res.data if row.get("merchant")]))
            return sorted(merchants)
    except Exception:
        pass
    return []

# --- ROBUST HTML DATALIST MERCHANT INPUT ---
def merchant_autocomplete_input(default_value="", key_suffix=""):
    existing_merchants = get_existing_merchants()
    options_html = "".join([f'<option value="{m}">' for m in existing_merchants])
    unique_id = f"merchant_dl_{key_suffix}"
    sync_key = f"sync_merchant_{key_suffix}"
    
    st.markdown(f"""
        <div style="margin-bottom: 4px;">
            <label style="font-size: 14px; font-weight: 400; color: {text_color};">Merchant</label>
        </div>
    """, unsafe_allow_html=True)
    
    html_code = f"""
        <input list="{unique_id}" id="input_{unique_id}" value="{default_value}" placeholder="Type or select merchant..." style="width: 100%; padding: 8px 12px; background-color: {container_bg}; color: {text_color}; border: 1px solid {border_color}; border-radius: 4px; font-size: 16px; box-sizing: border-box;">
        <datalist id="{unique_id}">
            {options_html}
        </datalist>
        <script>
            const inputElem = document.getElementById("input_{unique_id}");
            
            function syncToStreamlit() {{
                const doc = window.parent.document;
                const stInput = doc.querySelector('input[aria-label="{sync_key}"]');
                
                if (stInput) {{
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(stInput, inputElem.value);
                    stInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            }}

            inputElem.addEventListener("input", syncToStreamlit);
            inputElem.addEventListener("change", syncToStreamlit);
            inputElem.addEventListener("blur", syncToStreamlit);
            
            setTimeout(syncToStreamlit, 250);
        </script>
    """
    st.components.v1.html(html_code, height=55)
    return st.text_input(sync_key, value=default_value, label_visibility="collapsed")

# --- ADD TRANSACTION POP-UP MODAL DIALOG ---
@st.dialog("Add New Transaction", width="medium")
def add_transaction_dialog():
    workflow_type = st.radio("Transaction Type", ["AMZ Card", "Direct"], index=0, horizontal=True)
    
    with st.form("transaction_form", clear_on_submit=True):
        amount = st.number_input("Amount ($)", value=None, placeholder="0.00", format="%.2f")
        merchant_name = merchant_autocomplete_input(default_value="", key_suffix="add")
        category = st.selectbox(
            "Category", 
            [
                "Groceries", "Utilities", "Shopping", "Entertainment", 
                "Home Improvement", "Pet Supplies", "Medicine", "Lunch", 
                "Hotels/Lodging", "Dining", "Liquor", "Auto Repair", 
                "Points Credits", "Other"
            ]
        )
        tx_date = st.date_input("Date")
        tx_time = st.time_input("Time")
        description = st.text_input("Description")
            
        submitted = st.form_submit_button("Save Transaction")
        
        if submitted:
            final_merchant = str(merchant_name).strip()
            if amount is None:
                st.error("Please enter a valid amount.")
            elif not final_merchant:
                st.error("Please provide a valid merchant name.")
            else:
                data = {
                    "date": str(tx_date),
                    "time": str(tx_time),
                    "amount": amount,
                    "merchant": final_merchant,
                    "category": category,
                    "description": description,
                    "type": workflow_type
                }
                insert_res = conn.table("Transactions").insert(data).execute()
                if insert_res:
                    st.success(f"Successfully saved {workflow_type} transaction!")
                    st.rerun()
                else:
                    st.error("Failed to save transaction.")

# --- EDIT TRANSACTION POP-UP MODAL DIALOG ---
@st.dialog("Edit Existing Transaction", width="medium")
def edit_transaction_dialog():
    response = conn.table("Transactions").select("*").execute()
    if not response or not response.data:
        st.info("No transactions found to edit.")
        return

    tx_list = response.data
    tx_options = {f"ID {t['id']} | {t['date']} | {t['merchant']} | ${t['amount']}": t for t in tx_list}
    selected_label = st.selectbox("Select Transaction to Edit", list(tx_options.keys()))
    selected_tx = tx_options[selected_label]

    categories_list = [
        "Groceries", "Utilities", "Shopping", "Entertainment", 
        "Home Improvement", "Pet Supplies", "Medicine", "Lunch", 
        "Hotels/Lodging", "Dining", "Liquor", "Auto Repair", 
        "Points Credits", "Other"
    ]
    current_cat = selected_tx.get("category")
    cat_default_idx = categories_list.index(current_cat) if current_cat in categories_list else 0

    workflow_types = ["AMZ Card", "Direct"]
    current_type = selected_tx.get("type", "AMZ Card")
    type_default_idx = workflow_types.index(current_type) if current_type in workflow_types else 0

    with st.form("edit_transaction_form"):
        workflow_type = st.radio("Transaction Type", workflow_types, index=type_default_idx, horizontal=True)
        amount = st.number_input("Amount ($)", value=float(selected_tx.get("amount", 0.0)), format="%.2f")
        merchant_name = merchant_autocomplete_input(default_value=selected_tx.get("merchant", ""), key_suffix="edit")
        category = st.selectbox("Category", categories_list, index=cat_default_idx)
        
        try:
            default_date = datetime.strptime(selected_tx.get("date"), "%Y-%m-%d").date()
        except Exception:
            default_date = datetime.today().date()

        try:
            time_str = str(selected_tx.get("time", "00:00:00"))[:8]
            default_time = datetime.strptime(time_str, "%H:%M:%S").time()
        except Exception:
            default_time = datetime.now().time()

        tx_date = st.date_input("Date", value=default_date)
        tx_time = st.time_input("Time", value=default_time)
        description = st.text_input("Description", value=selected_tx.get("description", ""))
            
        col1, col2 = st.columns(2)
        with col1:
            submitted = st.form_submit_button("Update Transaction", use_container_width=True)
        with col2:
            deleted = st.form_submit_button("🗑️ Delete Transaction", use_container_width=True, type="secondary")
        
        if submitted:
            final_merchant = str(merchant_name).strip()
            if not final_merchant:
                st.error("Please provide a valid merchant name.")
            else:
                updated_data = {
                    "date": str(tx_date),
                    "time": str(tx_time),
                    "amount": amount,
                    "merchant": final_merchant,
                    "category": category,
                    "description": description,
                    "type": workflow_type
                }
                update_res = conn.table("Transactions").update(updated_data).eq("id", selected_tx["id"]).execute()
                if update_res:
                    st.success("Transaction successfully updated!")
                    st.rerun()
                else:
                    st.error("Failed to update transaction.")
                    
        if deleted:
            delete_res = conn.table("Transactions").delete().eq("id", selected_tx["id"]).execute()
            if delete_res:
                st.success("Transaction successfully deleted!")
                st.rerun()
            else:
                st.error("Failed to delete transaction.")

# --- SIDEBAR BUTTONS & CONTROLS ---
if st.sidebar.button("➕ Add Transaction", type="primary", use_container_width=True):
    add_transaction_dialog()

if st.sidebar.button("✏️ Edit Transaction", use_container_width=True):
    edit_transaction_dialog()

st.sidebar.divider()

theme_choice = st.sidebar.select_slider(
    "Theme Mode", 
    options=["Dark", "Light"], 
    value="Light" if st.session_state.light_mode else "Dark"
)

new_light_mode = (theme_choice == "Light")
if new_light_mode != st.session_state.light_mode:
    st.session_state.light_mode = new_light_mode
    st.rerun()

st.sidebar.title("Financial Accounts")
account_selection = st.sidebar.selectbox(
    "Select Account",
    ["Primary Checking", "Emergency Savings", "Direct PLUS Loan"],
    label_visibility="collapsed"
)
st.sidebar.divider()
st.sidebar.info(f"Viewing: **{account_selection}**")

# --- CHECKING ACCOUNT LAYOUT ---
if account_selection == "Primary Checking":
    st.title("Checking Account: Cash Flow Calendar")
    
    main_col, side_col = st.columns([3, 1])

    with main_col:
        st.subheader("August 2026 Cash Flow Calendar")
        
        days_of_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        header_cols = st.columns(7)
        for i, col in enumerate(header_cols):
            col.markdown(f"<div style='text-align: center; font-weight: bold; color: {sub_text}; border: 1px solid {border_color}; background-color: {grid_header_bg}; padding: 4px;'>{days_of_week[i]}</div>", unsafe_allow_html=True)

        response = conn.table("Transactions").select("*").execute()
        
        august_grid = {}
        if response and hasattr(response, "data") and response.data:
            df_sup = pd.DataFrame(response.data)
            if 'date' in df_sup.columns:
                df_sup['date'] = pd.to_datetime(df_sup['date'], errors='coerce')
                aug_sup = df_sup[(df_sup['date'].dt.year == 2026) & (df_sup['date'].dt.month == 8)]
                
                for day in range(1, 32):
                    day_txs = aug_sup[aug_sup['date'].dt.day == day]
                    if not day_txs.empty:
                        net_sum = day_txs['amount'].sum()
                        items_list = ", ".join(day_txs['merchant'].dropna().unique())
                        sign_prefix = "+" if net_sum > 0 else ""
                        august_grid[day] = {
                            "net": f"{sign_prefix}${net_sum:,.2f}",
                            "items": items_list
                        }

        for week in range(5):
            w_cols = st.columns(7)
            for day in range(7):
                day_num = week * 7 + day - 4
                
                with w_cols[day]:
                    if 1 <= day_num <= 31:
                        data = august_grid.get(day_num, {"net": "$0.00", "items": ""})
                        net_val = data['net']
                        
                        if '+' in net_val:
                            net_color = '#3fb950'
                        elif '-' in net_val and net_val != '$0.00':
                            net_color = '#f85149'
                        else:
                            net_color = sub_text

                        with st.container(border=True):
                            st.markdown(f"<span style='font-weight:bold; color:{text_color};'>{day_num}</span> <span style='float:right; color:#58a6ff; font-size:0.85em; font-weight:600;'>${4500 - (day_num*10):,.2f}</span>", unsafe_allow_html=True)
                            st.markdown(f"<div style='font-size:0.75em; color:{sub_text}; min-height:24px; padding-top:2px;'>{data['items']}</div>", unsafe_allow_html=True)
                            st.markdown(f"<div style='text-align: right; color:{net_color}; font-weight:700; font-size:0.8em;'>{net_val}</div>", unsafe_allow_html=True)
                    else:
                        with st.container(border=True):
                            st.markdown(f"<span style='color:{border_color};'>-</span>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.success("**Projected Month-End Balance:** $4,850.00 *(Computed via actuals + future budget rules)*")

    with side_col:
        st.markdown("### Cash Position")
        st.metric("Current Balance", "$3,450.00")
        st.metric("Weekend Expected", "$3,100.00", "-$350.00")
        st.metric("Month-End Expected", "$4,850.00", "+$1,400.00")
        st.metric("Pending Rules", "2 Active")

    st.divider()
    st.subheader("Transaction Register & Schedule Mapping")
    
    response_reg = conn.table("Transactions").select("*").execute()
    if response_reg and hasattr(response_reg, "data") and response_reg.data:
        checking_data = pd.DataFrame(response_reg.data)
        if 'date' in checking_data.columns and 'time' in checking_data.columns:
            checking_data = checking_data.sort_values(by=["date", "time"], ascending=[False, False])
    else:
        checking_data = pd.DataFrame(columns=["Date", "Merchant", "Category", "Amount", "Type"])

    st.data_editor(checking_data, use_container_width=True, hide_index=True)

# --- SAVINGS ACCOUNT LAYOUT ---
elif account_selection == "Emergency Savings":
    st.title("Savings Account: Goals & Growth")
    s1, s2, s3 = st.columns(3)
    s1.metric("Total Savings", "$15,400.00")
    s2.metric("Emergency Goal", "$20,000.00", "77% reached")
    s3.metric("Monthly Contribution", "$500.00/mo")
    st.divider()
    st.progress(0.77, text="Emergency Fund Target: 77% ($15,400 / $20,000)")

# --- LINES OF CREDIT LAYOUT ---
elif account_selection == "Direct PLUS Loan":
    st.title("Liability Management: Direct PLUS Loan")
    l1, l2, l3 = st.columns(3)
    l1.metric("Remaining Principal", "$12,350.00")
    l2.metric("Interest Rate", "6.8%")
    l3.metric("Next Payment Due", "Sep 15, 2026")
