import inspect
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from st_supabase_connection import SupabaseConnection


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Multi-Account Financial Ledger",
    layout="wide",
)


# ============================================================
# CONSTANTS
# ============================================================

LOCAL_TZ = ZoneInfo("America/New_York")

BASE_CATEGORIES = [
    "Groceries",
    "Utilities",
    "Shopping",
    "Entertainment",
    "Home Improvement",
    "Pet Supplies",
    "Medicine",
    "Lunch",
    "Hotels/Lodging",
    "Dining",
    "Liquor",
    "Auto Repair",
    "Points Credits",
    "Other",
]

NEW_MERCHANT_OPTION = "➕ Add New Merchant..."
NEW_CATEGORY_OPTION = "➕ Add New Category..."


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("financial_ledger")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ============================================================
# SUPABASE CONNECTION
# ============================================================

conn = st.connection("supabase", type=SupabaseConnection)


# ============================================================
# AUTHENTICATION
# ============================================================

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False


if not st.session_state.authenticated:
    st.title("Financial Ledger Login")

    password = st.text_input(
        "Enter Password:",
        type="password",
    )

    if password == st.secrets["APP_PASSWORD"]:
        st.session_state.authenticated = True
        st.rerun()
    elif password:
        st.error("Incorrect password")

    st.stop()


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown(
    """
    <style>

    /* Reduce column gaps so cells touch like Excel */
    [data-testid="stHorizontalBlock"] {
        gap: 0px !important;
    }

    /* Green Add Transaction button */
    [data-testid="stSidebar"] button[kind="primary"] {
        background-color: #2ea043 !important;
        color: #ffffff !important;
        border-color: #2ea043 !important;
        font-weight: bold !important;
    }

    [data-testid="stSidebar"] button[kind="primary"]:hover {
        background-color: #2c974b !important;
        color: #ffffff !important;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# DATABASE / CACHE HELPERS
# ============================================================


def _show_or_log_database_error(message: str, exc: Exception) -> None:
    """Log database errors without silently pretending the database is empty."""
    logger.exception(message, exc_info=exc)
    # User-facing message is intentionally concise. Full details remain in logs.
    st.error(
        "The transaction database could not be read. "
        "Check the Supabase connection and try again."
    )


@st.cache_data(ttl=10, show_spinner=False)
def get_transactions_cached() -> list[dict]:
    """Fetch transactions once and reuse the result across the current cache window."""
    try:
        response = (
            conn
            .table("Transactions")
            .select("*")
            .execute()
        )

        if response is None:
            raise RuntimeError("Supabase returned no response.")

        # Supabase clients normally raise for HTTP/API errors, but keep a defensive
        # check for response objects that expose an explicit error attribute.
        response_error = getattr(response, "error", None)
        if response_error:
            raise RuntimeError(str(response_error))

        return response.data or []

    except Exception as exc:
        logger.exception("Unable to fetch Transactions table.")
        raise RuntimeError(
            "Unable to read the Transactions table. See application logs for details."
        ) from exc


def get_transactions() -> list[dict]:
    """Retrieve cached transactions and surface database failures to the user."""
    try:
        return get_transactions_cached()
    except Exception:
        st.error(
            "The transaction database could not be read. "
            "Check the Supabase connection and try again."
        )
        return []


@st.cache_data(ttl=10, show_spinner=False)
def get_existing_merchants() -> list[str]:
    """Return unique merchants from the cached transaction dataset."""
    transactions = get_transactions_cached()

    merchants = {
        str(row.get("merchant")).strip()
        for row in transactions
        if row.get("merchant")
    }

    return sorted(merchants, key=str.lower)


@st.cache_data(ttl=10, show_spinner=False)
def get_existing_categories() -> list[str]:
    """Return default categories plus categories found in transactions."""
    transactions = get_transactions_cached()

    db_categories = {
        str(row.get("category")).strip()
        for row in transactions
        if row.get("category")
    }

    return sorted(
        set(BASE_CATEGORIES).union(db_categories),
        key=str.lower,
    )


@st.cache_data(ttl=10, show_spinner=False)
def get_merchant_category_map() -> dict[str, str]:
    """
    Return merchant -> most recently used category.

    The source rows are sorted newest-first by date and time, then the first
    occurrence for each merchant is retained. Matching is case-insensitive.
    """
    transactions = get_transactions_cached()

    rows = []
    for row in transactions:
        merchant = row.get("merchant")
        category = row.get("category")

        if not merchant or not category:
            continue

        rows.append(
            {
                "merchant": str(merchant).strip(),
                "category": str(category).strip(),
                "date": str(row.get("date", "")),
                "time": str(row.get("time", "")),
            }
        )

    rows.sort(
        key=lambda item: (item["date"], item["time"]),
        reverse=True,
    )

    mapping: dict[str, str] = {}

    for row in rows:
        merchant_key = row["merchant"].casefold()
        if merchant_key not in mapping:
            mapping[merchant_key] = row["category"]

    return mapping


def get_last_category_for_merchant(merchant_name: str | None) -> str | None:
    """Find the latest category for a merchant, case-insensitively."""
    if not merchant_name:
        return None

    merchant_key = str(merchant_name).strip().casefold()
    return get_merchant_category_map().get(merchant_key)


def clear_transaction_caches() -> None:
    """Invalidate all caches that depend on Transactions."""
    get_transactions_cached.clear()
    get_existing_merchants.clear()
    get_existing_categories.clear()
    get_merchant_category_map.clear()


# ============================================================
# STREAMLIT WIDGET CAPABILITY HELPERS
# ============================================================


def selectbox_supports_new_options() -> bool:
    """Return whether this Streamlit build exposes accept_new_options on selectbox."""
    try:
        return "accept_new_options" in inspect.signature(st.selectbox).parameters
    except (TypeError, ValueError):
        return False


# ============================================================
# MERCHANT / CATEGORY STATE HELPERS
# ============================================================


def _category_key_for_merchant_change(prefix: str) -> str:
    return f"{prefix}_category"


def _custom_category_key(prefix: str) -> str:
    return f"{prefix}_custom_category"


def handle_merchant_change(prefix: str) -> None:
    """When merchant changes, set category to that merchant's latest category."""
    merchant_key = f"{prefix}_merchant"
    merchant = st.session_state.get(merchant_key, "")

    if merchant == NEW_MERCHANT_OPTION:
        merchant = st.session_state.get(f"{prefix}_new_merchant", "")

    merchant = str(merchant or "").strip()

    category_key = _category_key_for_merchant_change(prefix)
    custom_key = _custom_category_key(prefix)

    latest_category = get_last_category_for_merchant(merchant)

    if latest_category:
        st.session_state[category_key] = latest_category
    else:
        categories = list(get_existing_categories())
        if categories:
            st.session_state[category_key] = categories[0]

    # A manual custom-category override from a prior merchant should not silently
    # override the newly selected merchant's default.
    st.session_state[custom_key] = ""


def reset_add_transaction_state() -> None:
    """Reset the Add Transaction dialog to a clean state."""
    keys_to_clear = [
        "add_workflow_type",
        "add_amount",
        "add_merchant",
        "add_new_merchant",
        "add_category",
        "add_custom_category",
        "add_date",
        "add_time",
        "add_desc",
    ]

    for key in keys_to_clear:
        st.session_state.pop(key, None)

    st.session_state["add_workflow_type"] = "AMZ Card"
    st.session_state["add_category"] = list(get_existing_categories())[0]

    now = datetime.now(LOCAL_TZ)
    st.session_state["add_date"] = now.date()
    st.session_state["add_time"] = now.time().replace(microsecond=0)
    st.session_state["add_custom_category"] = ""
    st.session_state["add_desc"] = ""


def initialize_edit_transaction_state(selected_tx: dict) -> None:
    """Load a selected transaction into widget state when the selection changes."""
    tx_id = selected_tx.get("id")
    previous_id = st.session_state.get("edit_loaded_tx_id")

    if previous_id == tx_id:
        return

    current_type = selected_tx.get("type", "AMZ Card")
    merchant = str(selected_tx.get("merchant", "")).strip()
    category = str(selected_tx.get("category", "")).strip()

    try:
        edit_date = datetime.strptime(
            str(selected_tx.get("date")),
            "%Y-%m-%d",
        ).date()
    except Exception:
        edit_date = datetime.now(LOCAL_TZ).date()

    try:
        edit_time = datetime.strptime(
            str(selected_tx.get("time", "00:00:00"))[:8],
            "%H:%M:%S",
        ).time()
    except Exception:
        edit_time = datetime.now(LOCAL_TZ).time().replace(microsecond=0)

    st.session_state["edit_loaded_tx_id"] = tx_id
    st.session_state["edit_workflow_type"] = current_type
    st.session_state["edit_amount"] = float(selected_tx.get("amount", 0.0) or 0.0)
    st.session_state["edit_merchant"] = merchant
    st.session_state["edit_category"] = category or list(get_existing_categories())[0]
    st.session_state["edit_custom_category"] = ""
    st.session_state["edit_date"] = edit_date
    st.session_state["edit_time"] = edit_time
    st.session_state["edit_desc"] = selected_tx.get("description", "") or ""
    # Mark the newly loaded merchant as the baseline so the first render preserves
    # the transaction's saved category. A later merchant change can then override it.
    st.session_state["edit_last_merchant_signature"] = merchant.casefold()


# ============================================================
# MERCHANT SELECTOR
# ============================================================


def merchant_selector(
    prefix: str,
    current_merchant: str = "",
) -> str:
    """
    Native Streamlit merchant selector.

    On newer Streamlit versions, accept_new_options=True provides the desired
    "search existing or type a new merchant" experience.

    On older versions, the selector falls back to an explicit New Merchant field.
    No JavaScript or DOM/React manipulation is used.
    """
    existing_merchants = list(get_existing_merchants())

    if current_merchant:
        normalized = current_merchant.strip()
        existing_by_key = {merchant.casefold(): merchant for merchant in existing_merchants}
        if normalized and normalized.casefold() not in existing_by_key:
            existing_merchants.append(normalized)
            existing_merchants.sort(key=str.lower)

    if selectbox_supports_new_options():
        kwargs = {
            "label": "Merchant",
            "options": existing_merchants,
            "key": f"{prefix}_merchant",
            "accept_new_options": True,
        }

        if current_merchant:
            current_key = current_merchant.casefold()
            matching_index = next(
                (i for i, merchant in enumerate(existing_merchants)
                 if merchant.casefold() == current_key),
                None,
            )
            kwargs["index"] = matching_index
        elif "placeholder" in inspect.signature(st.selectbox).parameters:
            kwargs["index"] = None
            kwargs["placeholder"] = "Search existing merchant or type a new one..."
        else:
            kwargs["index"] = 0 if existing_merchants else None

        selected = st.selectbox(**kwargs)
        return str(selected or "").strip()

    # Compatibility fallback for older Streamlit versions.
    options = [NEW_MERCHANT_OPTION] + existing_merchants

    if current_merchant and current_merchant in options:
        default_index = options.index(current_merchant)
    else:
        default_index = 0 if not current_merchant else 0

    selected = st.selectbox(
        "Merchant",
        options,
        index=default_index,
        key=f"{prefix}_merchant",
        on_change=handle_merchant_change,
        args=(prefix,),
    )

    if selected == NEW_MERCHANT_OPTION:
        new_merchant = st.text_input(
            "New Merchant Name",
            placeholder="Enter merchant name...",
            key=f"{prefix}_new_merchant",
            on_change=handle_merchant_change,
            args=(prefix,),
        )
        return new_merchant.strip()

    return selected.strip()


# ============================================================
# CATEGORY SELECTOR
# ============================================================


def category_selector(
    merchant_name: str,
    prefix: str,
    current_category: str = "",
) -> tuple[str, str]:
    """
    Render Category and the optional New Category field.

    The merchant selector's callback changes the category widget state when the
    merchant changes. The user may then override the category normally.
    """
    categories = list(get_existing_categories())

    if current_category and current_category not in categories:
        categories.append(current_category)

    categories = sorted(set(categories), key=str.lower)
    cat_options = categories + [NEW_CATEGORY_OPTION]

    category_key = f"{prefix}_category"
    custom_key = f"{prefix}_custom_category"

    if category_key not in st.session_state:
        latest_category = get_last_category_for_merchant(merchant_name)
        if latest_category and latest_category in categories:
            st.session_state[category_key] = latest_category
        elif current_category and current_category in categories:
            st.session_state[category_key] = current_category
        elif categories:
            st.session_state[category_key] = categories[0]

    selected_category = st.selectbox(
        "Category",
        cat_options,
        key=category_key,
    )

    custom_category = st.text_input(
        "New Category Name",
        placeholder="Type here to override dropdown...",
        key=custom_key,
    )

    return selected_category, custom_category


# ============================================================
# TRANSACTION WRITE HELPERS
# ============================================================


def transaction_write_succeeded(response, operation: str) -> bool:
    """
    Validate a Supabase mutation response.

    Supabase normally raises an exception on API/HTTP errors. We also inspect a
    possible response.error attribute and require returned row data when the
    client supplies it. This avoids treating a generic response object as proof
    that a financial transaction was actually changed.
    """
    if response is None:
        st.error(f"The database did not return a response for the {operation}.")
        return False

    response_error = getattr(response, "error", None)
    if response_error:
        st.error(f"The database rejected the {operation}: {response_error}")
        return False

    data = getattr(response, "data", None)

    # For mutation calls we expect changed row data from PostgREST when using the
    # standard Supabase table API. Empty/None data is treated as a failed/no-op write.
    if not data:
        st.error(f"The {operation} did not report a changed transaction.")
        return False

    return True


def build_time_string(tx_date, tx_time) -> str:
    """Create the existing date/time representation with the local timezone offset."""
    naive_dt = datetime.combine(tx_date, tx_time)
    localized_dt = naive_dt.replace(tzinfo=LOCAL_TZ)

    tz_offset = localized_dt.strftime("%z")
    formatted_offset = (
        f"{tz_offset[:3]}:{tz_offset[3:]}"
        if tz_offset
        else ""
    )

    return f"{tx_time.strftime('%H:%M:%S')}{formatted_offset}"


# ============================================================
# ADD TRANSACTION DIALOG
# ============================================================


@st.dialog("Add New Transaction", width="medium")
def add_transaction_dialog():
    if "add_workflow_type" not in st.session_state:
        reset_add_transaction_state()

    workflow_type = st.radio(
        "Transaction Type",
        ["AMZ Card", "Direct"],
        horizontal=True,
        key="add_workflow_type",
    )

    amount = st.number_input(
        "Amount ($)",
        value=None,
        placeholder="0.00",
        format="%.2f",
        key="add_amount",
    )

    # The current code uses the native selector. In the accept_new_options case,
    # the user can type a new merchant directly; changing the merchant itself will
    # trigger a Streamlit rerun, and the category state is synchronized below.
    merchant_name = merchant_selector("add")

    # Detect a merchant change for the native accept_new_options selector.
    merchant_signature = str(merchant_name or "").strip().casefold()
    previous_signature = st.session_state.get("add_last_merchant_signature")

    if previous_signature != merchant_signature:
        latest_category = get_last_category_for_merchant(merchant_name)
        categories = list(get_existing_categories())

        if latest_category and latest_category in categories:
            st.session_state["add_category"] = latest_category
        elif categories and "add_category" not in st.session_state:
            st.session_state["add_category"] = categories[0]

        st.session_state["add_custom_category"] = ""
        st.session_state["add_last_merchant_signature"] = merchant_signature

    selected_cat_option, custom_category = category_selector(
        merchant_name=merchant_name,
        prefix="add",
    )

    tx_date = st.date_input(
        "Date",
        key="add_date",
    )

    tx_time = st.time_input(
        "Time",
        key="add_time",
    )

    description = st.text_input(
        "Description",
        key="add_desc",
    )

    if st.button(
        "Save Transaction",
        type="primary",
        use_container_width=True,
        key="save_add_tx",
    ):
        final_merchant = str(merchant_name or "").strip()
        final_category = (
            custom_category.strip()
            if custom_category.strip()
            else selected_cat_option
        )

        if amount is None:
            st.error("Please enter a valid amount.")
            return

        if not final_merchant:
            st.error("Please provide a valid merchant name.")
            return

        if final_category == NEW_CATEGORY_OPTION:
            st.error("Please type a name for your new category in the text box.")
            return

        data = {
            "date": str(tx_date),
            "time": build_time_string(tx_date, tx_time),
            "amount": amount,
            "merchant": final_merchant,
            "category": final_category,
            "description": description,
            "type": workflow_type,
        }

        try:
            insert_res = (
                conn
                .table("Transactions")
                .insert(data)
                .execute()
            )

            if transaction_write_succeeded(insert_res, "insert"):
                clear_transaction_caches()
                st.success(
                    f"Successfully saved {workflow_type} transaction!"
                )
                st.rerun()

        except Exception as exc:
            _show_or_log_database_error(
                "Unable to insert transaction.",
                exc,
            )


# ============================================================
# EDIT TRANSACTION DIALOG
# ============================================================


@st.dialog("Edit Existing Transaction", width="medium")
def edit_transaction_dialog():
    transactions = get_transactions()

    if not transactions:
        st.info("No transactions found to edit.")
        return

    tx_list = sorted(
        transactions,
        key=lambda row: (
            str(row.get("date", "")),
            str(row.get("time", "")),
            str(row.get("id", "")),
        ),
        reverse=True,
    )

    tx_options = {
        (
            f"ID {t['id']} | "
            f"{t.get('date', '')} | "
            f"{t.get('merchant', '')} | "
            f"${float(t.get('amount', 0) or 0):,.2f}"
        ): t
        for t in tx_list
    }

    selected_label = st.selectbox(
        "Select Transaction to Edit",
        list(tx_options.keys()),
        key="edit_tx_select_dropdown",
    )

    selected_tx = tx_options[selected_label]
    initialize_edit_transaction_state(selected_tx)

    workflow_types = ["AMZ Card", "Direct"]

    if st.session_state["edit_workflow_type"] not in workflow_types:
        st.session_state["edit_workflow_type"] = workflow_types[0]

    workflow_type = st.radio(
        "Transaction Type",
        workflow_types,
        horizontal=True,
        key="edit_workflow_type",
    )

    amount = st.number_input(
        "Amount ($)",
        format="%.2f",
        key="edit_amount",
    )

    original_merchant = str(
        selected_tx.get("merchant", "")
    ).strip()

    merchant_name = merchant_selector(
        prefix="edit",
        current_merchant=original_merchant,
    )

    # Explicit merchant-change detection for the native selector path.
    merchant_signature = str(merchant_name or "").strip().casefold()
    previous_signature = st.session_state.get("edit_last_merchant_signature")

    if previous_signature != merchant_signature:
        # The selected transaction was already loaded above, so any signature change
        # here represents an actual merchant change by the user. Use that merchant's
        # latest category as the new default.
        latest_category = get_last_category_for_merchant(merchant_name)
        categories = list(get_existing_categories())

        if latest_category and latest_category in categories:
            st.session_state["edit_category"] = latest_category
        elif categories:
            st.session_state["edit_category"] = categories[0]

        st.session_state["edit_custom_category"] = ""
        st.session_state["edit_last_merchant_signature"] = merchant_signature

    current_category = str(
        selected_tx.get("category", "")
    ).strip()

    selected_cat_option, custom_category = category_selector(
        merchant_name=merchant_name,
        prefix="edit",
        current_category=current_category,
    )

    tx_date = st.date_input(
        "Date",
        key="edit_date",
    )

    tx_time = st.time_input(
        "Time",
        key="edit_time",
    )

    description = st.text_input(
        "Description",
        key="edit_desc",
    )

    col1, col2 = st.columns(2)

    with col1:
        submitted = st.button(
            "Update Transaction",
            use_container_width=True,
            type="primary",
            key="update_tx_btn",
        )

    with col2:
        deleted = st.button(
            "🗑️ Delete Transaction",
            use_container_width=True,
            type="secondary",
            key="delete_tx_btn",
        )

    # ========================================================
    # UPDATE
    # ========================================================

    if submitted:
        final_merchant = str(merchant_name or "").strip()
        final_category = (
            custom_category.strip()
            if custom_category.strip()
            else selected_cat_option
        )

        if not final_merchant:
            st.error("Please provide a valid merchant name.")
            return

        if final_category == NEW_CATEGORY_OPTION:
            st.error("Please type a name for your new category in the text box.")
            return

        updated_data = {
            "date": str(tx_date),
            "time": build_time_string(tx_date, tx_time),
            "amount": amount,
            "merchant": final_merchant,
            "category": final_category,
            "description": description,
            "type": workflow_type,
        }

        try:
            update_res = (
                conn
                .table("Transactions")
                .update(updated_data)
                .eq("id", selected_tx["id"])
                .execute()
            )

            if transaction_write_succeeded(update_res, "update"):
                clear_transaction_caches()
                st.success("Transaction successfully updated!")
                st.rerun()

        except Exception as exc:
            _show_or_log_database_error(
                "Unable to update transaction.",
                exc,
            )

    # ========================================================
    # DELETE
    # ========================================================

    if deleted:
        try:
            delete_res = (
                conn
                .table("Transactions")
                .delete()
                .eq("id", selected_tx["id"])
                .execute()
            )

            if transaction_write_succeeded(delete_res, "delete"):
                clear_transaction_caches()
                st.success("Transaction successfully deleted!")
                st.rerun()

        except Exception as exc:
            _show_or_log_database_error(
                "Unable to delete transaction.",
                exc,
            )


# ============================================================
# SIDEBAR BUTTONS & ACCOUNT CONTROLS
# ============================================================

if st.sidebar.button(
    "➕ Add Transaction",
    type="primary",
    use_container_width=True,
):
    reset_add_transaction_state()
    add_transaction_dialog()


if st.sidebar.button(
    "✏️ Edit Transaction",
    use_container_width=True,
):
    edit_transaction_dialog()


st.sidebar.divider()

st.sidebar.title("Financial Accounts")

account_selection = st.sidebar.selectbox(
    "Select Account",
    [
        "Primary Checking",
        "Emergency Savings",
        "Direct PLUS Loan",
    ],
    label_visibility="collapsed",
)

st.sidebar.divider()

if st.sidebar.button("Log Out", use_container_width=True):
    st.session_state.authenticated = False
    st.rerun()

st.sidebar.info(f"Viewing: **{account_selection}**")


# ============================================================
# CHECKING ACCOUNT LAYOUT
# ============================================================

if account_selection == "Primary Checking":
    st.title("Checking Account: Cash Flow Calendar")

    main_col, side_col = st.columns([3, 1])

    with main_col:
        # Intentionally left as August 2026 placeholder UI per user's request.
        st.subheader("August 2026 Cash Flow Calendar")

        days_of_week = [
            "Mon",
            "Tue",
            "Wed",
            "Thu",
            "Fri",
            "Sat",
            "Sun",
        ]

        header_cols = st.columns(7)
        for i, col in enumerate(header_cols):
            col.markdown(
                f"""
                <div style='
                    text-align: center;
                    font-weight: bold;
                    border: 1px solid var(--secondary-background-color);
                    background-color: var(--secondary-background-color);
                    padding: 4px;'>
                    {days_of_week[i]}
                </div>
                """,
                unsafe_allow_html=True,
            )

        transactions = get_transactions()

        august_grid = {}

        if transactions:
            df_sup = pd.DataFrame(transactions)

            if "date" in df_sup.columns:
                df_sup["date"] = pd.to_datetime(
                    df_sup["date"],
                    errors="coerce",
                )

                aug_sup = df_sup[
                    (df_sup["date"].dt.year == 2026)
                    & (df_sup["date"].dt.month == 8)
                ]

                for day in range(1, 32):
                    day_txs = aug_sup[
                        aug_sup["date"].dt.day == day
                    ]

                    if not day_txs.empty:
                        net_sum = day_txs["amount"].fillna(0).sum()
                        items = day_txs["merchant"].dropna().unique()
                        items_list = ", ".join(map(str, items))
                        sign_prefix = "+" if net_sum > 0 else ""

                        august_grid[day] = {
                            "net": f"{sign_prefix}${net_sum:,.2f}",
                            "items": items_list,
                        }

        for week in range(5):
            w_cols = st.columns(7)

            for day in range(7):
                day_num = week * 7 + day - 4

                with w_cols[day]:
                    if 1 <= day_num <= 31:
                        data = august_grid.get(
                            day_num,
                            {"net": "$0.00", "items": ""},
                        )

                        net_val = data["net"]

                        if "+" in net_val:
                            net_color = "#3fb950"
                        elif "-" in net_val and net_val != "$0.00":
                            net_color = "#f85149"
                        else:
                            net_color = "gray"

                        with st.container(border=True):
                            st.markdown(
                                f"<span style='font-weight:bold;'>{day_num}</span> "
                                f"<span style='float:right; color:#58a6ff; "
                                f"font-size:0.85em; font-weight:600;'>"
                                f"${4500 - (day_num * 10):,.2f}</span>",
                                unsafe_allow_html=True,
                            )

                            st.markdown(
                                f"<div style='font-size:0.75em; color: gray; "
                                f"min-height:24px; padding-top:2px;'>"
                                f"{data['items']}</div>",
                                unsafe_allow_html=True,
                            )

                            st.markdown(
                                f"<div style='text-align: right; color:{net_color}; "
                                f"font-weight:700; font-size:0.8em;'>"
                                f"{net_val}</div>",
                                unsafe_allow_html=True,
                            )
                    else:
                        with st.container(border=True):
                            st.markdown(
                                "<span style='color: gray;'>-</span>",
                                unsafe_allow_html=True,
                            )

        st.markdown("<br>", unsafe_allow_html=True)
        st.success(
            "**Projected Month-End Balance:** $4,850.00 "
            "*(Computed via actuals + future budget rules)*"
        )

    with side_col:
        st.markdown("### Cash Position")
        st.metric("Current Balance", "$3,450.00")
        st.metric("Weekend Expected", "$3,100.00", "-$350.00")
        st.metric("Month-End Expected", "$4,850.00", "+$1,400.00")
        st.metric("Pending Rules", "2 Active")

    st.divider()
    st.subheader("Transaction Register & Schedule Mapping")

    # Reuse the same cached transaction result rather than querying Supabase again.
    register_data = get_transactions()

    if register_data:
        checking_data = pd.DataFrame(register_data)

        if "date" in checking_data.columns and "time" in checking_data.columns:
            checking_data = checking_data.sort_values(
                by=["date", "time"],
                ascending=[False, False],
            )
    else:
        checking_data = pd.DataFrame(
            columns=[
                "Date",
                "Merchant",
                "Category",
                "Amount",
                "Type",
            ]
        )

    st.dataframe(
        checking_data,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# SAVINGS ACCOUNT LAYOUT
# ============================================================

elif account_selection == "Emergency Savings":
    st.title("Savings Account: Goals & Growth")

    s1, s2, s3 = st.columns(3)

    s1.metric("Total Savings", "$15,400.00")
    s2.metric("Emergency Goal", "$20,000.00", "77% reached")
    s3.metric("Monthly Contribution", "$500.00/mo")

    st.divider()

    st.progress(
        0.77,
        text="Emergency Fund Target: 77% ($15,400 / $20,000)",
    )


# ============================================================
# DIRECT PLUS LOAN LAYOUT
# ============================================================

elif account_selection == "Direct PLUS Loan":
    st.title("Liability Management: Direct PLUS Loan")

    l1, l2, l3 = st.columns(3)

    l1.metric("Remaining Principal", "$12,350.00")
    l2.metric("Interest Rate", "6.8%")
    l3.metric("Next Payment Due", "Sep 15, 2026")


