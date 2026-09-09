from uuid import uuid4
from html import escape
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from supabase import create_client
from time import monotonic
from copy import deepcopy


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

NEW_CATEGORY_OPTION = "➕ Add New Category..."


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("financial_ledger")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ============================================================
# AUTHENTICATION — one Supabase client per browser session
# ============================================================

ALLOWED_USER_ID = "5a9e2156-a5cb-4f65-9a7c-db8e0f92bf1d"


def clear_login_state():
    for key in list(st.session_state):
        del st.session_state[key]


def new_supabase_client():
    config = st.secrets.get('connections', {}).get('supabase', {})
    url = config.get('SUPABASE_URL') or config.get('url') or st.secrets.get('SUPABASE_URL')
    key = config.get('SUPABASE_KEY') or config.get('key') or st.secrets.get('SUPABASE_KEY')
    if not url or not key:
        raise ValueError('Supabase connection settings are missing.')
    # Reject administrative credentials: this app must use user-scoped RLS.
    if str(key).startswith('sb_secret_'):
        raise ValueError('Use the anon/public or publishable key, not a secret key.')
    if str(key).startswith('eyJ'):
        # This is only a configuration check, never a JWT authentication check.
        import base64
        import json
        payload = str(key).split('.')[1]
        role = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('role')
        if role != 'anon':
            raise ValueError('Use the anon/public key.')
    return create_client(str(url), str(key))


if 'ledger_client' not in st.session_state:
    st.title('Financial Ledger Login')
    with st.form('ledger_login', clear_on_submit=True):
        email = st.text_input('Email')
        password = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Sign in', type='primary')
    if submitted:
        try:
            client = new_supabase_client()
            result = client.auth.sign_in_with_password({'email': email.strip(), 'password': password})
            if not result.user or str(result.user.id) != ALLOWED_USER_ID:
                try:
                    client.auth.sign_out()
                finally:
                    raise ValueError('Account is not authorized.')
            clear_login_state()
            st.session_state['ledger_client'] = client
            st.session_state['last_activity'] = monotonic()
            st.rerun()
        except Exception:
            # Do not log credentials, authentication responses, or tokens.
            st.error('Sign-in failed. Check your email, password, and app connection settings.')
    st.stop()

conn = st.session_state['ledger_client']
def require_session():
    if monotonic() - st.session_state.get('last_activity', 0) > 1800:
        try:
            conn.auth.sign_out()
        except Exception:
            pass
        clear_login_state()
        st.info('Your session expired. Sign in again.')
        st.stop()
    
    try:
        # get_user validates with the Auth server, rather than trusting local state.
        verified = conn.auth.get_user()
        if not verified.user or str(verified.user.id) != ALLOWED_USER_ID:
            raise ValueError('Unauthorized account.')
    except Exception:
        clear_login_state()
        st.error('Your session could not be verified. Reload and sign in again.')
        st.stop()
    st.session_state['last_activity'] = monotonic()


require_session()


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


def get_transactions_cached() -> list[dict]:
    """Session-local cache; never share financial records between logins."""
    cached = st.session_state.get('ledger_rows')
    if cached and monotonic() - cached['at'] < 10:
        return deepcopy(cached['rows'])
    try:
        rows, offset = [], 0
        while True:
            response = (conn.table("Transactions").select("*").order("id")
                        .range(offset, offset + 499).execute())
            if response is None or getattr(response, "error", None) or response.data is None:
                raise RuntimeError("Supabase did not return transaction data.")
            rows.extend(response.data)
            if len(response.data) < 500:
                break
            offset += 500

        if response is None:
            raise RuntimeError("Supabase returned no response.")

        # Supabase clients normally raise for HTTP/API errors, but keep a defensive
        # check for response objects that expose an explicit error attribute.
        response_error = getattr(response, "error", None)
        if response_error:
            raise RuntimeError(str(response_error))

        st.session_state['ledger_rows'] = {'at': monotonic(), 'rows': deepcopy(rows)}
        return rows

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


def get_existing_merchants() -> list[str]:
    """Return unique merchants from the cached transaction dataset."""
    transactions = get_transactions_cached()

    merchants = {
        str(row.get("merchant")).strip()
        for row in transactions
        if row.get("merchant")
    }

    return sorted(merchants, key=str.lower)


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
    st.session_state.pop('ledger_rows', None)


# ============================================================
# TRANSACTION STATE HELPERS
# ============================================================


def reset_add_transaction_state() -> None:
    """Reset the Add Transaction dialog to a clean state."""
    keys_to_clear = [
        "add_workflow_type",
        "add_direction",
        "add_amount",
        "add_merchant",
        "add_last_merchant_signature",
        "add_category",
        "add_custom_category",
        "add_date",
        "add_time",
        "add_desc",
    ]

    for key in keys_to_clear:
        st.session_state.pop(key, None)

    st.session_state["add_merchant_instance"] = uuid4().hex
    st.session_state["add_workflow_type"] = "AMZ Card"
    st.session_state["add_direction"] = "Expense"
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

    st.session_state["edit_merchant_instance"] = uuid4().hex
    st.session_state["edit_loaded_tx_id"] = tx_id
    st.session_state["edit_workflow_type"] = current_type
    st.session_state["edit_direction"] = selected_tx.get("direction")
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


MERCHANT_HTML = """
<div class="ledger-merchant-autocomplete">
<label for="merchant">Merchant</label>
<input id="merchant" type="text" tabindex="0" autocomplete="off" spellcheck="false"
       aria-autocomplete="inline" aria-describedby="merchant-help"
       placeholder="Start typing a merchant..." />
<small id="merchant-help">Tab accepts the completion. Escape dismisses it.</small>
</div>
"""

MERCHANT_CSS = """
.ledger-merchant-autocomplete label { display: block; margin-bottom: .4rem; font-size: .875rem; }
.ledger-merchant-autocomplete input {
    box-sizing: border-box; width: 100%; padding: .65rem .75rem;
    border: 1px solid var(--st-border-color, #80808066);
    border-radius: .5rem; font: inherit;
    color: var(--st-text-color);
    background: var(--st-secondary-background-color);
}
.ledger-merchant-autocomplete input:focus { outline: 2px solid var(--st-primary-color); outline-offset: -2px; }
.ledger-merchant-autocomplete small { display: block; margin-top: .25rem; opacity: .7; font-size: .75rem; }
"""

MERCHANT_JS = r"""
export default function({ parentElement, data, setStateValue }) {
    const input = parentElement.querySelector('input');
    const merchants = data.merchants;
    // Preserve an uncommitted draft if another widget causes a rerender.
    // Python gives each newly opened/selected transaction a fresh component key.
    if (!input.dataset.initialized) {
        input.value = data.value || '';
        input.dataset.initialized = 'true';
        input.dataset.committed = input.value.trim();
    }

    let suggestionStart = null;
    let composing = false;

    function commit() {
        suggestionStart = null;
        const typed = input.value.trim();
        const exact = merchants.find(
            merchant => merchant.toLowerCase() === typed.toLowerCase()
        );
        const value = exact || typed;
        input.value = value;
        if (value !== input.dataset.committed) {
            input.dataset.committed = value;
            setStateValue('value', value);
        }
    }

    function complete() {
        suggestionStart = null;
        const prefix = input.value;
        // Complete only at the end, so editing the middle remains predictable.
        if (!prefix || input.selectionStart !== prefix.length ||
                input.selectionEnd !== prefix.length) return;
        const match = merchants.find(
            merchant => merchant.toLowerCase().startsWith(prefix.toLowerCase())
        );
        if (match && match.length > prefix.length) {
            // Keep typed casing until acceptance; select only the added suffix.
            input.value = prefix + match.slice(prefix.length);
            suggestionStart = prefix.length;
            input.setSelectionRange(prefix.length, input.value.length);
        }
    }

    input.oninput = event => {
        suggestionStart = null;
        if (composing || event.isComposing) return;
        // Deletion must remove text without immediately putting it back.
        if ((event.inputType || '').startsWith('delete')) return;
        complete();
    };
    input.oncompositionstart = () => { composing = true; };
    input.oncompositionend = () => { composing = false; complete(); };
    input.onkeydown = event => {
        if (composing || event.isComposing) return;
        if (event.key === 'Tab') {
            commit();
            // Allow the browser's normal Tab/Shift+Tab focus navigation.
        } else if (event.key === 'Enter') {
            event.preventDefault();
            commit();
            input.setSelectionRange(input.value.length, input.value.length);
        } else if (event.key === 'Escape' && suggestionStart !== null) {
            event.preventDefault();
            event.stopPropagation();
            input.value = input.value.slice(0, suggestionStart);
            input.setSelectionRange(input.value.length, input.value.length);
            suggestionStart = null;
        } else if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            suggestionStart = null;
        }
    };
    input.onpointerdown = () => { suggestionStart = null; };
    input.onblur = commit;
}
"""


@st.cache_resource
def get_merchant_component():
    """Register once using Streamlit's public component API."""
    try:
        from streamlit.components.v2 import component
    except ImportError:
        st.error("Merchant autocomplete requires Streamlit 1.51 or newer.")
        st.stop()
    return component(
        "ledger_merchant_autocomplete",
        html=MERCHANT_HTML,
        css=MERCHANT_CSS,
        js=MERCHANT_JS,
        # Let the dialog's focus manager discover the merchant input.
        # CSS selectors above are scoped to this component's wrapper.
        isolate_styles=False,
    )


def merchant_selector(prefix: str, current_merchant: str = "") -> str:
    """Inline prefix completion; Tab, Enter, or blur commits the merchant."""
    merchant_key = f"{prefix}_merchant"
    instance_key = f"{prefix}_merchant_instance"
    if instance_key not in st.session_state:
        st.session_state[instance_key] = uuid4().hex

    # Match and deduplicate case-insensitively, with a stable suggestion order.
    by_key = {}
    for merchant in get_existing_merchants():
        if merchant.strip():
            by_key.setdefault(merchant.strip().casefold(), merchant.strip())
    if current_merchant.strip():
        by_key.setdefault(current_merchant.strip().casefold(), current_merchant.strip())
    merchants = sorted(by_key.values(), key=str.casefold)
    initial = str(st.session_state.get(merchant_key, current_merchant) or "")

    result = get_merchant_component()(
        data={"merchants": merchants, "value": initial},
        default={"value": initial},
        key=f"{prefix}_autocomplete_{st.session_state[instance_key]}",
        on_value_change=lambda: None,
    )
    value = str(result.value if result.value is not None else initial).strip()
    value = by_key.get(value.casefold(), value)
    st.session_state[merchant_key] = value
    return value



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

    The dialog synchronizes category state when the accepted merchant changes.
    The user may then override the category normally.
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
    require_session()
    if "add_workflow_type" not in st.session_state:
        reset_add_transaction_state()

    workflow_type = st.radio(
        "Transaction Type",
        ["AMZ Card", "Direct"],
        horizontal=True,
        key="add_workflow_type",
    )

    direction = st.selectbox(
        "Income / Expense", ["Expense", "Income"],
        key="add_direction", placeholder="Classify this transaction",
    )
    st.caption("Enter a positive amount. AMZ Card income represents a refund or card credit.")

    amount = st.number_input(
        "Amount ($)",
        value=None,
        placeholder="0.00",
        format="%.2f",
        key="add_amount",
    )

    merchant_name = merchant_selector("add")

    # Apply category defaults only when the accepted merchant changes.
    merchant_signature = str(merchant_name or "").strip().casefold()
    previous_signature = st.session_state.get("add_last_merchant_signature")

    if previous_signature != merchant_signature:
        latest_category = get_last_category_for_merchant(merchant_name)
        categories = list(get_existing_categories())

        if latest_category and latest_category in categories:
            st.session_state["add_category"] = latest_category
        elif categories:
            st.session_state["add_category"] = categories[0]

        st.session_state["add_custom_category"] = ""
        st.session_state["add_last_merchant_signature"] = merchant_signature

    selected_cat_option, custom_category = category_selector(
        merchant_name=merchant_name,
        prefix="add",
    )

    description = st.text_input(
        "Description",
        key="add_desc",
    )

    tx_date = st.date_input(
        "Date",
        key="add_date",
    )

    tx_time = st.time_input(
        "Time",
        key="add_time",
    )

    if st.button(
        "Save Transaction",
        type="primary",
        use_container_width=True,
        key="save_add_tx",
    ):
        if direction not in ("Expense", "Income") or amount is None or not math.isfinite(amount) or amount < 0:
            st.error("Choose Income or Expense and enter a nonnegative amount.")
            return
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
            "direction": direction,
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
    require_session()
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
    if selected_tx.get('card_budget_week'):
        paid_week = load_card_weeks().get(str(selected_tx['card_budget_week']))
        if paid_week:
            st.info('This card entry has been reconciled. Amount, direction, date, type and deletion are locked; merchant, description and category can still be edited.')

    workflow_types = ["AMZ Card", "Direct"]

    if st.session_state["edit_workflow_type"] not in workflow_types:
        st.session_state["edit_workflow_type"] = workflow_types[0]

    workflow_type = st.radio(
        "Transaction Type",
        workflow_types,
        horizontal=True,
        key="edit_workflow_type",
    )

    direction = st.selectbox(
        "Income / Expense", ["Expense", "Income"],
        key="edit_direction", placeholder="Classify this transaction",
    )
    st.caption("Enter a positive amount. AMZ Card income represents a refund or card credit.")

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

    # Preserve the saved category until the accepted merchant changes.
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

    description = st.text_input(
        "Description",
        key="edit_desc",
    )

    tx_date = st.date_input(
        "Date",
        key="edit_date",
    )

    tx_time = st.time_input(
        "Time",
        key="edit_time",
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
        if direction not in ("Expense", "Income") or amount is None or not math.isfinite(amount) or amount < 0:
            st.error("Choose Income or Expense and enter a nonnegative amount.")
            return
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
            "direction": direction,
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
# EDITABLE CASH FLOW CALENDAR
# ============================================================

from calendar import Calendar, monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Match the supplied September calendar; month navigation remains a later feature.
CALENDAR_YEAR = 2026
CALENDAR_MONTH = 9


def money(value):
    """Use cents throughout balance calculations; reject invalid database values."""
    try:
        result = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Enter a valid dollar amount.') from exc
    if not result.is_finite() or abs(result) > Decimal('999999999.99'):
        raise ValueError('Dollar amount is outside the supported range.')
    return result


def week_ending(day):
    """Sunday through Saturday, including weeks spanning two months."""
    return day + timedelta(days=(5 - day.weekday()) % 7)


def calendar_balances(transactions, settings, first, last, as_of=None, reconciled=None, planned=None):
    """Roll balances forward from the latest explicit opening balance.

    Amounts are nonnegative; direction defines income or expense.
    AMZ Card expenses minus card income/refunds are settled on Saturday.
    Open weeks reserve their budget on Saturday until explicitly reconciled.
    Closed weeks deduct their frozen payment on its actual date and retain surplus.
    """
    reconciled = reconciled or {}
    anchors = [key for key in settings if key.startswith('opening:')
               and date.fromisoformat(key.split(':', 1)[1]) <= first]
    if not anchors:
        raise ValueError('Set the opening checking balance to start this calendar.')
    anchor = max(anchors)
    start = date.fromisoformat(anchor.split(':', 1)[1])
    balance = money(settings[anchor]['amount'])
    direct, card, payments = {}, {}, {}
    for closed in reconciled.values():
        payment_day = date.fromisoformat(closed['payment_date'])
        payments.setdefault(payment_day, []).append(closed)
    for row in transactions:
        day = date.fromisoformat(str(row['date'])[:10])
        amount = money(row.get('amount', 0))
        kind = row.get('type')
        assigned_week = (date.fromisoformat(str(row['card_budget_week']))
                         if row.get('card_budget_week') else week_ending(day))
        if kind == 'AMZ Card' and assigned_week.isoformat() in reconciled:
            continue  # Its immutable payment is counted separately, once.
        effective = assigned_week if kind == 'AMZ Card' else day
        if effective < start or effective > last:
            continue
        direction = row.get('direction')
        if direction not in ('Income', 'Expense') or amount < 0:
            raise ValueError(f"Classify transaction {row.get('id')} as Income or Expense with a nonnegative amount.")
        if kind == 'AMZ Card':
            amount = amount if direction == 'Expense' else -amount
            saturday = assigned_week
            card[saturday] = card.get(saturday, Decimal(0)) + amount
        elif kind == 'Direct':
            amount = amount if direction == 'Income' else -amount
            direct[day] = direct.get(day, Decimal(0)) + amount
        else:
            raise ValueError(f"Transaction {row.get('id')} has an unsupported type.")
    for item in planned or []:
        if not item['enabled'] or item.get('transaction_id') is not None:
            continue
        due = date.fromisoformat(item['due_date'])
        amount = money(item['amount'])
        direct[due] = direct.get(due, Decimal(0)) + (amount if item['direction'] == 'Income' else -amount)
    days = {}
    day = start
    while day <= last:
        net = direct.get(day, Decimal(0)) - sum((money(row['paid_amount']) for row in payments.get(day, [])), Decimal(0))
        spent, remaining, budget = Decimal(0), Decimal(0), Decimal(0)
        surplus = Decimal(0)
        completed = day.isoformat() in reconciled
        if day.weekday() == 5:
            spent = card.get(day, Decimal(0))
            budget_key = 'budget:' + day.isoformat()
            budget = money(settings.get(budget_key, {}).get('amount', 0))
            remaining = max(budget - spent, Decimal(0))
            if completed:
                snapshot = reconciled[day.isoformat()]
                spent = money(snapshot['paid_amount'])
                budget = money(snapshot['budget_amount'])
                remaining = max(budget - spent, Decimal(0))
                surplus = remaining
            else:
                net -= max(spent + remaining, Decimal(0))
        balance += net
        if day >= first:
            days[day] = dict(balance=balance, net=net, spent=spent,
                             remaining=remaining, budget=budget,
                             surplus=surplus, completed=completed, payments=payments.get(day, []))
        day += timedelta(days=1)
    return days, start


def load_calendar_settings():
    rows, offset = [], 0
    while True:
        response = (conn.table('LedgerCalendarSettings').select('*')
                    .order('key').range(offset, offset + 499).execute())
        if getattr(response, 'error', None) or response.data is None:
            raise RuntimeError('Calendar settings could not be loaded.')
        rows.extend(response.data)
        if len(response.data) < 500:
            return {row['key']: row for row in rows}
        offset += 500


def save_calendar_setting(key, amount, previous):
    """Compare revisions to avoid overwriting a change from another open tab."""
    payload = {'key': key, 'amount': str(money(amount)),
               'revision': (previous['revision'] + 1) if previous else 1}
    if key.startswith('budget:'):
        payload['is_override'] = True
    try:
        table = conn.table('LedgerCalendarSettings')
        if previous:
            response = (table.update(payload).eq('key', key)
                        .eq('revision', previous['revision']).execute())
        else:
            response = table.insert(payload).execute()
        if not response.data or getattr(response, 'error', None):
            st.error('The setting was not saved. Reload to check for another edit.')
            return False
        return True
    except Exception:
        logger.exception('Unable to save calendar setting.')
        st.error('The setting could not be saved. Reload and check the connection.')
        return False


def load_card_weeks():
    records, offset = {}, 0
    while True:
        response = conn.table('LedgerCardWeeks').select('*').order('week_ending').range(offset, offset + 499).execute()
        if response.data is None:
            raise RuntimeError('Card reconciliation records could not be loaded.')
        records.update({row['week_ending']: row for row in response.data})
        if len(response.data) < 500:
            return records
        offset += 500


@st.dialog('Reconcile and mark card paid', width='large')
def reconcile_card_dialog():
    require_session()
    try:
        clear_transaction_caches()
        rows = get_transactions_cached()
        settings = load_calendar_settings()
        closed = load_card_weeks()
    except Exception:
        st.error('Could not load card weeks. Apply card_reconciliation.sql and check the connection.')
        return
    candidates = {str(row['card_budget_week']) for row in rows
                  if row.get('type') == 'AMZ Card' and row.get('card_budget_week')}
    candidates.update(key.split(':', 1)[1] for key in settings if key.startswith('budget:'))
    if closed:
        candidates.add((date.fromisoformat(max(closed)) + timedelta(days=7)).isoformat())
    candidates = sorted(candidates - set(closed))
    if not candidates:
        st.info('Save a weekly budget or add a card transaction first.')
        return
    week = st.selectbox('Budget week ending Saturday', candidates)
    included = sorted([row for row in rows if row.get('type') == 'AMZ Card'
                       and str(row.get('card_budget_week')) == week], key=lambda row: int(row['id']))
    st.dataframe(pd.DataFrame(included), use_container_width=True, hide_index=True)
    if any(row.get('direction') not in ('Expense','Income') or money(row['amount']) < 0 for row in included):
        st.error('Classify all included purchases and credits before closing this week.')
        return
    budget_key = 'budget:' + week
    if budget_key not in settings:
        st.info('Set this week’s budget first. This also lets you set a budget outside the displayed month.')
        with st.form('reconcile_new_budget_' + week):
            new_budget = st.number_input('Weekly card budget', min_value=0.0, format='%.2f')
            save_budget = st.form_submit_button('Save weekly budget')
        if save_budget and save_calendar_setting(budget_key, new_budget, None):
            st.rerun()
        return
    budget = money(settings[budget_key]['amount'])
    total = sum((money(row['amount']) * (1 if row['direction']=='Expense' else -1)
                 for row in included), Decimal(0))
    if total < 0:
        st.error('This week has a net card credit; review it before marking a payment.')
        return
    st.write(f'Payment to record: **${total:,.2f}** · Budget surplus: **${max(budget-total, Decimal(0)):,.2f}**')
    st.caption('This records a payment you already made; it does not send money. '
               'The listed amounts, dates and budget assignments will be locked. '
               'New card entries will go to the following budget week, regardless of their date.')
    with st.form('confirm_card_payment_' + week):
        payment_date = st.date_input('Actual payment date', value=datetime.now(LOCAL_TZ).date(),
                                    max_value=datetime.now(LOCAL_TZ).date())
        confirmed = st.checkbox('I reconciled these transactions and paid the amount shown.')
        submitted = st.form_submit_button('Reconcile and mark paid', type='primary')
    if submitted:
        if not confirmed:
            st.error('Confirm the reviewed transactions and payment first.')
            return
        try:
            expected = [{'id': row['id'], 'amount': float(money(row['amount'])),
                         'direction': row['direction']} for row in included]
            response = conn.rpc('ledger_reconcile_card_week', {
                'p_week': week, 'p_payment_date': payment_date.isoformat(),
                'p_expected': expected, 'p_expected_budget': float(budget),
            }).execute()
            if not response.data:
                raise RuntimeError('Payment was not confirmed.')
            clear_transaction_caches()
            st.rerun()
        except Exception as exc:
            # Database errors here are deliberate validation messages, not credentials.
            st.error(f'Could not confirm reconciliation: {getattr(exc, "message", "Reload and review this week again.")}')


def calendar_entry_html(row):
    """Read-only signed amount with an escaped, browser-native hover tooltip."""
    amount = money(row['amount'])
    is_income = row.get('direction') == 'Income'
    displayed = f"{'+' if is_income else '−'}${amount:,.2f}"
    if row.get("planned"):
        displayed += " ◦"
    description = str(row.get('description') or 'Not provided')
    merchant = str(row.get('merchant') or 'Not provided')
    tooltip = f"Amount: {displayed}\nDescription: {description}\nMerchant: {merchant}"
    color = '#238636' if is_income else '#d14343'
    return (
        f'<span tabindex="0" title="{escape(tooltip, quote=True)}" '
        f'aria-label="{escape(tooltip, quote=True)}" '
        f'style="display:block;cursor:help;color:{color};padding:3px 0;'
        f'font-variant-numeric:tabular-nums;">{escape(displayed)}</span>'
    )


def render_editable_calendar():
    first = date(CALENDAR_YEAR, CALENDAR_MONTH, 1)
    last = date(CALENDAR_YEAR, CALENDAR_MONTH, monthrange(CALENDAR_YEAR, CALENDAR_MONTH)[1])
    st.title('Checking Account: Cash Flow Calendar')
    st.subheader(first.strftime('%B %Y'))
    try:
        settings = load_calendar_settings()
        transactions = get_transactions_cached()
        reconciled = load_card_weeks()
    except Exception:
        logger.exception('Unable to load editable calendar.')
        st.error('The calendar could not be loaded. Apply calendar_setup.sql and card_reconciliation.sql in Supabase, '
                 'then check that the app connection can access LedgerCalendarSettings.')
        return

    unclassified = [row for row in transactions if row.get('direction') not in ('Income', 'Expense')]
    if unclassified:
        st.warning(f"{len(unclassified)} existing transactions need Income/Expense classification. Use Edit Transaction in the sidebar to review and save each one.")
        st.dataframe(pd.DataFrame(unclassified), hide_index=True, use_container_width=True)

    opening_key = 'opening:' + first.isoformat()
    anchors = [key for key in settings if key.startswith('opening:')
               and key.split(':', 1)[1] <= first.isoformat()]
    with st.expander('Opening balance', expanded=not anchors):
        st.caption('Enter the checking balance immediately before the first day’s transactions. '
                   'When an earlier month has an opening balance, its ending balance carries '
                   'forward automatically. Saving here creates an override for this month.')
        with st.form('calendar_opening'):
            opening = st.number_input('Opening checking balance', format='%.2f',
                                      value=float(settings[opening_key]['amount'])
                                      if opening_key in settings else None)
            save_opening = st.form_submit_button('Save opening balance')
        if save_opening:
            if opening is None:
                st.error('Enter the opening checking balance.')
            elif save_calendar_setting(opening_key, opening, settings.get(opening_key)):
                st.rerun()
    if not anchors:
        st.info('Enter the opening balance above to enable the calendar.')
        return
    try:
        anchor_date = date.fromisoformat(max(anchors).split(':', 1)[1])
        ensure_budget_months(anchor_date, first)
        settings = load_calendar_settings()
        planned = load_budget_table('LedgerBudgetItems')
        balances, anchor = calendar_balances(transactions, settings, first, last,
                                             reconciled=reconciled, planned=planned)
    except Exception:
        logger.exception('Budget calendar load failed.')
        st.error('The calendar could not calculate. Apply budget_setup.sql and check the connection and transaction classifications.')
        return

    st.caption('Hover over a transaction amount for its description and merchant. '
               'Use the sidebar to add or edit transactions. Card purchases are '
               'entered as positive AMZ Card transactions and assigned to a budget week. '
               'Do not enter the same card payment again as a Direct expense.')
    st.caption('Open card weeks reserve spending plus remaining budget on Saturday. '
               'Use Reconcile card week after paying: the actual payment date then controls '
               'the deduction, and unused budget is retained as surplus.')
    if reconciled:
        next_week = date.fromisoformat(max(reconciled)) + timedelta(days=7)
        st.info(f'New card entries apply to the budget week ending {next_week:%b %d, %Y}, regardless of transaction date.')
    if st.button('Reconcile card week', key='calendar_reconcile'):
        reconcile_card_dialog()
    if anchor < first:
        st.caption(f'Opening balance carried forward from the saved balance on {anchor:%b %d, %Y}.')
    direct = {}
    for row in transactions:
        if row.get('type') == 'Direct':
            direct.setdefault(str(row['date'])[:10], []).append(row)
    for item in planned:
        if item['enabled'] and item.get('transaction_id') is None:
            direct.setdefault(item['due_date'], []).append({
                'id': 'planned-' + str(item['id']), 'amount': item['amount'],
                'direction': item['direction'], 'merchant': item['name'],
                'description': 'Planned: ' + (item.get('description') or item['name']),
                'planned': True,
            })
    st.caption('Balances include planned items. Link a paid bill to its actual Direct transaction on the Budget page to replace the estimate.')
    headers = st.columns(7)
    for col, name in zip(headers, ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']):
        col.markdown(f'**{name}**')
    # Calendar supplies a sixth row when necessary, so August 31 is never omitted.
    for week in Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        cols = st.columns(7)
        for col, day in zip(cols, week):
            with col:
                if day.month != first.month:
                    st.caption(day.strftime('%b %d'))
                    continue
                values = balances[day]
                with st.container(border=True):
                    st.markdown(f"**{day.day}** · **${values['balance']:,.2f}**")
                    for row in sorted(direct.get(day.isoformat(), []), key=lambda row: str(row['id'])):
                        st.markdown(calendar_entry_html(row), unsafe_allow_html=True)
                    for payment in values['payments']:
                        st.markdown(calendar_entry_html({
                            'amount': payment['paid_amount'], 'direction': 'Expense',
                            'merchant': 'Credit card payment',
                            'description': f"Reconciled budget week ending {payment['week_ending']}",
                        }), unsafe_allow_html=True)
                    if day.weekday() == 5:
                        key = 'budget:' + day.isoformat()
                        if values['completed']:
                            if values['surplus'] > 0:
                                st.caption(f"Budget surplus: ${values['surplus']:,.2f}")
                        else:
                            st.markdown(calendar_entry_html({
                                'amount': values['remaining'], 'direction': 'Expense',
                                'merchant': 'Weekly card budget remaining',
                                'description': f"Budget: ${values['budget']:,.2f}; spent: ${values['spent']:,.2f}",
                            }), unsafe_allow_html=True)
                        st.markdown(
                            '<div style="background:#00b4e6;color:#002b36;padding:6px;'
                            'border-radius:3px;font-weight:600">'
                            f"${values['spent']:,.2f}</div>", unsafe_allow_html=True)
                        if values['spent'] > values['budget'] and key in settings:
                            st.caption(f"Over budget: ${values['spent'] - values['budget']:,.2f}")
                    st.caption(f"Day net: ${values['net']:,.2f}")
    st.metric('Projected month-end balance', f"${balances[last]['balance']:,.2f}")
    monthly_surplus = sum((values['surplus'] for values in balances.values()), Decimal(0))
    st.metric('Monthly budget surplus — completed weeks', f"${monthly_surplus:,.2f}")
    st.caption('Includes completed weeks whose Saturday falls in this month. '
               'Over-budget weeks show zero surplus and are flagged above. '
               'Reconciled payments and budget surplus are preserved from the saved reconciliation.')
    st.divider()
    st.subheader('Transaction Register & Schedule Mapping')
    st.dataframe(pd.DataFrame(transactions), use_container_width=True, hide_index=True)


# ============================================================
# BUDGET DEFAULTS AND MONTHLY PLANS
# ============================================================

def load_budget_table(table):
    rows, offset = [], 0
    order = 'month' if table == 'LedgerBudgetMonths' else 'id'
    while True:
        response = conn.table(table).select('*').order(order).range(offset, offset + 499).execute()
        if getattr(response, 'error', None) or response.data is None:
            raise RuntimeError('Budget data could not be loaded.')
        rows.extend(response.data)
        if len(response.data) < 500:
            return rows
        offset += 500


def ensure_budget_months(first, last):
    """Materialize each month once, including intermediate carry-forward months."""
    prepared = {row['month'] for row in load_budget_table('LedgerBudgetMonths')}
    current = first.replace(day=1)
    while current <= last:
        if current.isoformat() not in prepared:
            response = conn.rpc('ledger_prepare_budget_month', {'p_month': current.isoformat()}).execute()
            if response.data is not True:
                raise RuntimeError('The monthly budget was not prepared.')
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)


def save_budget_row(table, payload, previous=None):
    try:
        if previous:
            payload = dict(payload, revision=previous['revision'] + 1)
            response = (conn.table(table).update(payload).eq('id', previous['id'])
                        .eq('revision', previous['revision']).execute())
        else:
            response = conn.table(table).insert(payload).execute()
        if getattr(response, 'error', None) or not response.data:
            st.error('This item changed in another tab. Reload before saving again.')
            return False
        clear_transaction_caches()
        return True
    except Exception:
        logger.exception('Budget save failed.')
        st.error('The item could not be saved. Reload and check that a linked transaction is not already used by another budget item.')
        return False


def budget_editor_changes(edited, originals, month, transaction_labels):
    """Validate all rows before sending an atomic batch; retain original revisions."""
    changes = []
    permanent_weekly = set()
    for row in edited.to_dict('records'):
        row = {k: (None if pd.isna(v) else v) for k,v in row.items()}
        key = row.get('_key')
        previous = originals.get(key)
        if previous is None:
            # Blank appended rows do not create empty budget items.
            if not str(row.get('Item') or '').strip():
                continue
            if str(row.get('Item')).lower() == 'nan':
                continue
        elif all(row.get(field) == previous.get(field) for field in (
            'Item','Amount','Day','Direction','Schedule','Include','Description','Actual transaction','Apply change')):
            continue
        kind = previous['_kind'] if previous else 'bill'
        scope = row.get('Apply change') or 'This month only'
        if scope not in ('This month only','This month and future months'):
            raise ValueError('Choose which months each edit should affect.')
        amount = money(row.get('Amount'))
        if amount < 0:
            raise ValueError('Amounts must be positive or zero.')
        day = int(row.get('Day') or 1)
        if not 1 <= day <= 31 or float(row.get('Day') or 1) != day:
            raise ValueError('Enter a whole calendar day from 1 to 31.')
        actual_label = row.get('Actual transaction') or 'Not linked'
        if actual_label not in transaction_labels:
            raise ValueError('Select an actual transaction from the list.')
        payload = dict(kind=kind, scope=scope, amount=str(amount), day=day,
            name=str(row.get('Item') or '').strip(), description=str(row.get('Description') or ''),
            direction=row.get('Direction') or 'Expense', schedule=row.get('Schedule') or 'Monthly',
            enabled=True if row.get('Include') is None else bool(row['Include']), transaction_id=transaction_labels[actual_label])
        if not payload['name'] or len(payload['name']) > 200:
            raise ValueError('Each item needs a name of at most 200 characters.')
        if payload['direction'] not in ('Income','Expense') or payload['schedule'] not in ('Monthly','As needed','Weekly'):
            raise ValueError('Choose a valid direction and schedule.')
        if kind == 'weekly':
            if previous['_closed']:
                raise ValueError('Reconciled card weeks cannot be edited.')
            for field in ('Item','Day','Direction','Schedule','Include','Description','Actual transaction'):
                if row.get(field) != previous.get(field):
                    raise ValueError('For a weekly card row, edit only Amount and Apply change.')
            group = 'third' if 15 <= day <= 21 else 'regular'
            if scope == 'This month and future months':
                if group in permanent_weekly:
                    raise ValueError('Choose just one permanent change for regular weeks and one for the third week per save.')
                permanent_weekly.add(group)
            payload.update(week=previous['_week'], revision=previous['_revision'], rule_revision=previous['_rule_revision'])
        else:
            if payload['schedule']=='Weekly':
                raise ValueError('Use the existing weekly card rows for weekly budgets.')
            if payload['transaction_id'] is not None and not payload['enabled']:
                raise ValueError('Unlink the actual transaction before making an item inactive.')
            payload.update(id=previous['_id'] if previous else None,
                revision=previous['_revision'] if previous else None,
                rule_revision=previous['_rule_revision'] if previous else None)
        changes.append(payload)
    return changes


def render_budget_page():
    st.title('Budget')
    chosen = st.date_input('Budget month', value=date(CALENDAR_YEAR, CALENDAR_MONTH, 1), key='budget_month_choice')
    month = chosen.replace(day=1)
    snapshot_key = 'budget_grid_snapshot_' + month.isoformat()
    if st.button('Reload budget / discard unsaved changes'):
        st.session_state.pop(snapshot_key, None)
        st.session_state['budget_grid_generation'] = st.session_state.get('budget_grid_generation',0)+1
        st.rerun()
    try:
        if snapshot_key not in st.session_state:
            ensure_budget_months(month, month)
            clear_transaction_caches()
            st.session_state[snapshot_key] = dict(items=load_budget_table('LedgerBudgetItems'),
                rules=load_budget_table('LedgerBudgetRules'), settings=load_calendar_settings(),
                defaults=load_budget_table('LedgerWeeklyDefaults')[0], closed=load_card_weeks(),
                transactions=get_transactions_cached())
        data = st.session_state[snapshot_key]
        rules = {r['id']:r for r in data['rules']}
        labels = {'Not linked': None}
        for tx in data['transactions']:
            if tx.get('type') == 'Direct' and tx.get('direction') in ('Income','Expense'):
                label = f"{tx['date']} · {tx.get('merchant') or 'No merchant'} · {tx['direction']} ${money(tx['amount']):,.2f} · entry {tx['id']}"
                labels[label] = tx['id']
        reverse = {v:k for k,v in labels.items()}
        rows = []
        for item in data['items']:
            if item['month'] != month.isoformat():
                continue
            rule=rules[item['rule_id']]
            rows.append({'_key':'bill:'+str(item['id']), '_kind':'bill','_id':item['id'],
                '_revision':item['revision'],'_rule_revision':rule['revision'],
                'Item':item['name'],'Amount':float(money(item['amount'])),
                'Day':item.get('due_day') or date.fromisoformat(item['due_date']).day,
                'Direction':item['direction'], 'Schedule':'As needed' if item.get('schedule',rule['schedule'])=='as_needed' else 'Monthly',
                'Include':item['enabled'],'Description':item['description'],
                'Actual transaction':reverse.get(item['transaction_id'],'Not linked'),
                'Apply change':'This month only','Status':'Linked' if item['transaction_id'] is not None else 'Planned' if item['enabled'] else 'Inactive'})
        for n in range(1,monthrange(month.year,month.month)[1]+1):
            day=month.replace(day=n)
            if day.weekday()!=5: continue
            key='budget:'+day.isoformat()
            setting=data['settings'].get(key,{'amount':0,'revision':None})
            closed=data['closed'].get(day.isoformat())
            rows.append({'_key':key,'_kind':'weekly','_week':day.isoformat(),
                '_revision':setting['revision'],'_rule_revision':data['defaults']['revision'],'_closed':bool(closed),
                'Item':'Weekly card budget','Amount':float(money(closed['budget_amount'] if closed else setting['amount'])),
                'Day':n,'Direction':'Expense','Schedule':'Weekly','Include':True,
                'Description':'Third Saturday' if 15<=n<=21 else 'Regular week',
                'Actual transaction':'Not linked','Apply change':'This month only',
                'Status':'Reconciled — locked' if closed else 'Open card week'})
        rows.sort(key=lambda r:(r['Day'],r['Item']))
    except Exception:
        logger.exception('Unable to load budget editor.')
        st.error('Could not load the budget. Install budget_editor_update.sql after the previous budget setup, then reload.')
        return
    st.subheader(month.strftime('%B %Y'))
    st.caption('Edit directly in the rows, choose Apply change for each changed row, then Save changes. '
               'Add a bill or income item using the blank row at the bottom. To remove an expense from the plan, clear Include.')
    st.caption('Permanent changes begin in this month. Earlier months, linked payments, reconciled card weeks, '
               'and saved month-only exceptions are preserved. For weekly rows, edit only Amount and Apply change; '
               'a permanent regular-week change applies to all regular Saturdays, while the third Saturday stays separate.')
    visible=['Item','Amount','Day','Direction','Schedule','Include','Description','Actual transaction','Apply change','Status']
    frame=pd.DataFrame(rows)
    with st.form('unified_budget_'+month.isoformat()):
        edited=st.data_editor(frame, hide_index=True, use_container_width=True, num_rows='dynamic',
            key='budget_grid_'+month.isoformat()+'_'+str(st.session_state.get('budget_grid_generation',0)),
            column_order=visible, disabled=['Status']+[c for c in frame.columns if c.startswith('_')],
            column_config={
                'Item':st.column_config.TextColumn(required=True,max_chars=200),
                'Amount':st.column_config.NumberColumn(min_value=0,max_value=999999999.99,format='$%.2f',required=True),
                'Day':st.column_config.NumberColumn(min_value=1,max_value=31,step=1,required=True),
                'Direction':st.column_config.SelectboxColumn(options=['Expense','Income'],default='Expense',required=True),
                'Schedule':st.column_config.SelectboxColumn(options=['Monthly','As needed','Weekly'],default='Monthly',required=True),
                'Include':st.column_config.CheckboxColumn(default=True),
                'Actual transaction':st.column_config.SelectboxColumn(options=list(labels),default='Not linked'),
                'Apply change':st.column_config.SelectboxColumn(options=['This month only','This month and future months'],default='This month only',required=True),
            })
        saved=st.form_submit_button('Save changes',type='primary')
    st.caption('Link actual Direct transactions in the table to replace their planned amount and date. '
               'Card payments continue to use Reconcile card week. Days 29–31 use the last day of shorter months.')
    if saved:
        try:
            originals={r['_key']:r for r in rows}
            if set(originals)-set(edited['_key'].dropna()):
                raise ValueError('To remove an item, clear Include instead of deleting its row. This preserves its history.')
            changes=budget_editor_changes(edited,originals,month,labels)
            if not changes:
                st.info('No changes to save.')
                return
            result=conn.rpc('ledger_save_budget_edits',{'p_month':month.isoformat(),'p_changes':changes}).execute()
            if result.data is not True: raise RuntimeError('Save not confirmed.')
            for key in list(st.session_state):
                if key.startswith('budget_grid_snapshot_'): st.session_state.pop(key,None)
            st.session_state['budget_grid_generation']=st.session_state.get('budget_grid_generation',0)+1
            clear_transaction_caches()
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            logger.exception('Atomic budget save failed.')
            st.error('Nothing was saved. Check that linked transactions match the direction and are not used twice. '
                     'If another tab changed the budget, reload before trying again. Confirm budget_editor_update.sql is installed.')


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
    st.session_state.pop("edit_loaded_tx_id", None)
    edit_transaction_dialog()


if st.sidebar.button('Reconcile card week', use_container_width=True):
    reconcile_card_dialog()


st.sidebar.divider()

if st.sidebar.button('Budget', use_container_width=True):
    st.session_state['ledger_view'] = 'Budget'
if st.sidebar.button('View selected account', use_container_width=True):
    st.session_state['ledger_view'] = 'Account'

st.sidebar.title("Financial Accounts")

account_selection = st.sidebar.selectbox(
    "Select Account",
    [
        "Primary Checking",
        "Emergency Savings",
        "Direct PLUS Loan",
    ],
    label_visibility="collapsed",
    key='ledger_account',
    on_change=lambda: st.session_state.update(ledger_view='Account'),
)
if st.session_state.get('ledger_view') == 'Budget':
    account_selection = 'Budget'

st.sidebar.divider()

if st.sidebar.button("Log Out", use_container_width=True):
    try:
        conn.auth.sign_out()
    except Exception:
        st.warning('Server sign-out could not be confirmed. Local session cleared.')
    clear_login_state()
    st.rerun()

st.sidebar.info(f"Viewing: **{account_selection}**")


# ============================================================
# CHECKING ACCOUNT LAYOUT
# ============================================================

if account_selection == "Primary Checking":
    render_editable_calendar()


# ============================================================
# SAVINGS ACCOUNT LAYOUT
# ============================================================

elif account_selection == "Budget":
    render_budget_page()

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
