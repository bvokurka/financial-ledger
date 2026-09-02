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
        tx_date = st.date_input("Date", value=datetime.today().date())
        # Explicitly set value to current local time
        tx_time = st.time_input("Time", value=datetime.now().time())
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
