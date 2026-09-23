from uuid import uuid4
import json
from hashlib import sha256
from streamlit.components.v2 import component
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


def get_all_transactions_cached() -> list[dict]:
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
    st.session_state.pop('savings_snapshot', None)


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
        "add_desc", "add_transfer_direction", "add_savings_bucket",
    ]

    for key in keys_to_clear:
        st.session_state.pop(key, None)

    st.session_state["add_merchant_instance"] = uuid4().hex
    st.session_state["add_workflow_type"] = "AMZ Card" if active_cash_id() == 1 else "Direct"
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
    st.session_state["edit_original_row"] = deepcopy(selected_tx)
    st.session_state["edit_workflow_type"] = current_type
    st.session_state["edit_direction"] = selected_tx.get("direction")
    st.session_state["edit_amount"] = float(selected_tx.get("amount", 0.0) or 0.0)
    st.session_state["edit_merchant"] = merchant
    st.session_state["edit_category"] = category or list(get_existing_categories())[0]
    st.session_state["edit_custom_category"] = ""
    st.session_state["edit_date"] = edit_date
    st.session_state["edit_time"] = edit_time
    st.session_state["edit_desc"] = selected_tx.get("description", "") or ""
    st.session_state["edit_transfer_direction"] = selected_tx.get("transfer_direction") or "To savings"
    st.session_state["edit_savings_bucket"] = selected_tx.get("savings_bucket_id")
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
        isolate_styles=False,  # Mount in the dialog DOM so its focus manager can reach the input.
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
    selected_savings = st.session_state.get('ledger_account') in {
        row['name'] for row in savings_accounts().values()}
    if selected_savings:
        st.session_state['add_workflow_type'] = 'Transfer'
        st.caption('Recording a transfer for ' + savings_accounts()[active_savings_id()]['name'])
    else:
        st.caption("Recording in " + cash_accounts()[active_cash_id()]["name"])
    workflow_type = st.radio(
        "Transaction Type",
        (['Transfer'] if selected_savings else
         (["AMZ Card"] if active_cash_id() == 1 else []) + ["Direct", "Check", "Transfer"]),
        horizontal=True,
        key="add_workflow_type",
    )

    if workflow_type == "Transfer":
        transfer_form()
        return

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
            "cash_account_id": active_cash_id(),
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
    if st.session_state.get('ledger_account') in {row['name'] for row in savings_accounts().values()}:
        bucket_ids = {b['id'] for b in load_budget_table('LedgerSavingsBuckets')
                      if int(b['savings_account_id']) == active_savings_id()}
        transfers = [t for t in load_budget_table('LedgerTransfers')
                     if t.get('source_bucket') in bucket_ids or t.get('destination_bucket') in bucket_ids]
        if not transfers:
            st.info('No linked transfers for this savings account. Individual budget expenses can be edited on View / Edit Budget.')
            return
        selected = st.selectbox('Select savings transfer', transfers,
            format_func=lambda t: str(t['date']) + ' · $' + str(t['amount']) + ' · ' + str(t['description']))
        transfer_form(selected)
        return
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

    requested_id = st.session_state.pop('calendar_edit_id', None)
    if requested_id is not None:
        match = next((label for label, row in tx_options.items() if str(row['id']) == str(requested_id)), None)
        if match is None:
            st.error('This transaction is no longer available. Reload the calendar.')
            return
        st.session_state['edit_tx_select_dropdown'] = match
        st.session_state.pop('edit_loaded_tx_id', None)
    selected_label = st.selectbox(
        "Select Transaction to Edit",
        list(tx_options.keys()),
        key="edit_tx_select_dropdown",
    )

    selected_tx = tx_options[selected_label]
    if selected_tx.get('transfer_id') is not None:
        transfer = next((t for t in load_budget_table('LedgerTransfers') if t['id'] == selected_tx['transfer_id']), None)
        if transfer is None: st.error('Transfer changed. Reload the page.'); return
        transfer_form(transfer)
        return
    initialize_edit_transaction_state(selected_tx)
    selected_tx = deepcopy(st.session_state["edit_original_row"])
    render_check_clearance(selected_tx)
    budget_generated = selected_tx.get('unified_occurrence_id') is not None
    if budget_generated:
        st.caption('Amount and date edits apply to this occurrence only. The recurring budget item stays unchanged.')
    paid_week = None
    if selected_tx.get('card_budget_week'):
        paid_week = load_card_weeks().get(str(selected_tx['card_budget_week']))
        if paid_week:
            st.info('This card entry has been reconciled. Amount, direction, date, type and deletion are locked; merchant, description and category can still be edited.')
            st.session_state['edit_workflow_type'] = selected_tx['type']
            st.session_state['edit_direction'] = selected_tx['direction']
            st.session_state['edit_amount'] = float(money(selected_tx['amount']))
            st.session_state['edit_date'] = date.fromisoformat(str(selected_tx['date'])[:10])
            st.session_state['edit_time'] = datetime.strptime(str(selected_tx.get('time') or '00:00:00')[:8], '%H:%M:%S').time()

    workflow_types = (["AMZ Card"] if active_cash_id() == 1 else []) + ["Direct", "Check"]
    if selected_tx.get('type') == 'Savings Transfer': workflow_types.append('Savings Transfer')
    if budget_generated: workflow_types = [selected_tx['type']]

    if st.session_state["edit_workflow_type"] not in workflow_types:
        st.session_state["edit_workflow_type"] = workflow_types[0]

    workflow_type = st.radio(
        "Transaction Type",
        workflow_types,
        format_func=lambda v: "Transfer" if v == "Savings Transfer" else v,
        horizontal=True,
        key="edit_workflow_type", disabled=bool(paid_week or budget_generated),
    )

    if workflow_type == "Savings Transfer":
        savings_transfer_form("edit", selected_tx)
        return

    direction = st.selectbox(
        "Income / Expense", ["Expense", "Income"],
        key="edit_direction", placeholder="Classify this transaction", disabled=bool(paid_week or budget_generated),
    )
    st.caption("Enter a positive amount. AMZ Card income represents a refund or card credit.")

    amount = st.number_input(
        "Amount ($)",
        format="%.2f",
        key="edit_amount", disabled=bool(paid_week),
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
        key="edit_date", disabled=bool(paid_week),
    )

    tx_time = st.time_input(
        "Time",
        key="edit_time", disabled=bool(paid_week),
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
            key="delete_tx_btn", disabled=bool(paid_week),
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
            "cash_account_id": active_cash_id(),
        }

        if paid_week:
            updated_data = {k: updated_data[k] for k in ("merchant", "category", "description")}
        try:
            update_res = (
                conn
                .table("Transactions")
                .update(updated_data)
                .eq("id", selected_tx["id"])
                .eq("check_revision", selected_tx["check_revision"])
                .execute()
            )

            if transaction_write_succeeded(update_res, "update"):
                invalidate_edited_transaction(selected_tx)
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
        if paid_week:
            st.error("Reconciled transactions cannot be deleted.")
            return
        try:
            delete_res = (
                conn
                .table("Transactions")
                .delete()
                .eq("id", selected_tx["id"])
                .eq("check_revision", selected_tx["check_revision"])
                .execute()
            )

            if transaction_write_succeeded(delete_res, "delete"):
                invalidate_edited_transaction(selected_tx)
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
    direct, card, payments, generated_card = {}, {}, {}, {}
    for closed in reconciled.values():
        payment_day = date.fromisoformat(closed['payment_date'])
        payments.setdefault(payment_day, []).append(closed)
    for row in transactions:
        day = date.fromisoformat(str(row['date'])[:10])
        amount = money(row.get('amount', 0))
        kind = row.get('type')
        assigned_week = (date.fromisoformat(str(row['card_budget_week']))
                         if row.get('card_budget_week') else week_ending(day))
        if kind == 'AMZ Card' and row.get('unified_occurrence_id') is not None:
            signed = amount if row.get('direction') == 'Expense' else -amount
            generated_card[assigned_week.isoformat()] = generated_card.get(
                assigned_week.isoformat(), Decimal(0)) + signed
            if start <= day <= last:
                direct[day] = direct.get(day, Decimal(0)) - signed
            continue
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
        elif kind in ('Direct', 'Check', 'Savings Transfer'):
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
        net = direct.get(day, Decimal(0)) - sum((
            money(row['paid_amount']) - generated_card.get(str(row['week_ending']), Decimal(0))
            for row in payments.get(day, [])), Decimal(0))
        spent, remaining, budget = Decimal(0), Decimal(0), Decimal(0)
        surplus = Decimal(0)
        completed = day.isoformat() in reconciled
        if day.weekday() == 5:
            manual_card = card.get(day, Decimal(0))
            spent = manual_card + generated_card.get(day.isoformat(), Decimal(0))
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
                net -= max(manual_card + remaining, Decimal(0))
        balance += net
        if day >= first:
            days[day] = dict(balance=balance, net=net, spent=spent,
                             remaining=remaining, budget=budget,
                             surplus=surplus, completed=completed, payments=payments.get(day, []))
        day += timedelta(days=1)
    return days, start


def get_transactions_cached():
    return [r for r in get_all_transactions_cached() if int(r.get('cash_account_id', 1)) == active_cash_id()]


def load_calendar_settings():
    if active_cash_id() != 1:
        return {r['key']: r for r in load_budget_table('LedgerCashSettings') if int(r['account_id']) == active_cash_id()}

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
    if active_cash_id() != 1:
        account_action('ledger_save_cash_opening', dict(p_account=active_cash_id(), p_key=key,
            p_amount=str(money(amount)), p_revision=previous['revision'] if previous else None))
        return False
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
    if active_cash_id() != 1: return {}
    records, offset = {}, 0
    while True:
        response = conn.table('LedgerCardWeeks').select('*').order('week_ending').range(offset, offset + 499).execute()
        if response.data is None:
            raise RuntimeError('Card reconciliation records could not be loaded.')
        records.update({row['week_ending']: row for row in response.data})
        if len(response.data) < 500:
            return records
        offset += 500



# Direct HTML in a supported v2 component enables events without Markdown parsing.
LEDGER_INTERACTION_JS = r"""
export default function({parentElement, data, setTriggerValue}) {
    const root = parentElement.querySelector('.ledger-interactive-root');
    root.innerHTML = data.html;
    root.onclick = event => {
        const button = event.target.closest('button[data-transaction],button[data-planned],button[data-payday],button[data-card-amount],button[data-review],button[data-savings]');
        if (!button || !root.contains(button)) return;
        if (button.dataset.savings) {
            setTriggerValue('action', {savings: button.dataset.savings});
        } else if (button.dataset.cardAmount) {
            setTriggerValue('action', {card_amount: button.dataset.cardAmount});
        } else if (button.dataset.transaction) {
            setTriggerValue('action', {id: button.dataset.transaction});
        } else if (button.dataset.payday) {
            setTriggerValue('action', {payday: button.dataset.payday});
        } else if (button.dataset.planned) {
            setTriggerValue('action', {planned: button.dataset.planned});
        } else {
            setTriggerValue('action', {review: button.dataset.review});
        }
    };
}
"""
ledger_interaction = component('ledger_interaction',
    html='<div class="ledger-interactive-root"></div>', js=LEDGER_INTERACTION_JS)


@st.dialog('Edit planned amount for this month', width='medium')
def planned_occurrence_dialog(item_id):
    require_session()
    key = 'occurrence_edit_' + str(item_id)
    if key not in st.session_state:
        try:
            item = next(r for r in load_budget_table('LedgerBudgetItems') if r['id'] == item_id)
            rule = next(r for r in load_budget_table('LedgerBudgetRules') if r['id'] == item['rule_id'])
            st.session_state[key] = (item, rule)
        except Exception:
            st.error('The planned item could not be loaded. Reload the calendar.')
            return
    item, rule = st.session_state[key]
    if not item['enabled'] or item.get('transaction_id') is not None:
        st.error('This estimate is inactive or linked. Reload the calendar to open the actual transaction.')
        return
    st.write(item['name'])
    st.caption('Only ' + item['month'][:7] + ' changes. Recurring defaults and other months stay unchanged.')
    with st.form(key + '_form'):
        amount = st.number_input('Amount ($)', min_value=0.0, max_value=999999999.99,
                                 value=float(money(item['amount'])), format='%.2f')
        save = st.form_submit_button('Save this month')
    if save:
        try:
            payload = occurrence_change(item, rule, amount)
            response = conn.rpc('ledger_save_budget_edits', {'p_month': item['month'], 'p_changes': [payload]}).execute()
            if response.data is not True:
                raise RuntimeError('Save not confirmed')
            invalidate_budget_views()
            st.session_state.pop(key, None)
            st.rerun()
        except Exception:
            st.error('Nothing was saved. The item may have changed. Close and reopen it before trying again.')


def occurrence_change(item, rule, amount):
    return dict(kind='bill', scope='This month only', id=item['id'], revision=item['revision'],
                rule_revision=rule['revision'], name=item['name'], description=item.get('description') or '',
                amount=str(money(amount)), day=item.get('due_day') or date.fromisoformat(item['due_date']).day,
                direction=item['direction'], schedule='As needed' if item.get('schedule') == 'as_needed' else 'Monthly',
                enabled=item['enabled'], transaction_id=item.get('transaction_id'))


def invalidate_budget_views():
    clear_transaction_caches()
    for key in list(st.session_state):
        if key.startswith(('budget_grid_snapshot_', 'payday_snapshot_')):
            st.session_state.pop(key, None)
    st.session_state['budget_grid_generation'] = st.session_state.get('budget_grid_generation', 0) + 1


def render_check_clearance(row):
    if row.get('type') != 'Check':
        return
    st.caption('Check status: ' + ('Cleared' if row.get('check_cleared_at') else 'Uncleared'))
    st.caption('Clearance actions apply to the saved check shown below, not unsaved edits. '
               'Save changes first if you are correcting this check.')
    cleared = bool(row.get('check_cleared_at'))
    if st.button('Mark uncleared' if cleared else 'Mark cleared', key='clear_saved_check_' + str(row['id'])):
        require_session()
        try:
            result = conn.rpc('ledger_unclear_check' if cleared else 'ledger_clear_check', {
                'p_id': int(row['id']), 'p_revision': int(row['check_revision'])}).execute()
            if result.data is not True:
                raise RuntimeError('Clearance not confirmed.')
            clear_transaction_caches()
            st.session_state.pop('edit_loaded_tx_id', None)
            st.rerun()
        except Exception:
            clear_transaction_caches()
            st.error('Clearance could not be confirmed. Close and reopen this transaction to review the latest saved version.')


def render_check_calendar(first, balances, direct, settings):
    markup = calendar_grid_html(first, balances, direct, settings, show_cards=active_cash_id()==1)
    st.caption('Click an actual transaction to edit it. Checks: yellow = uncleared; green = cleared. '
               'Use Mark cleared or Mark uncleared in the edit window. Clearance changes status only; '
               'checks affect projections once on their transaction date. Click a planned estimate to change its amount for that month only. Card summaries are read-only.')
    actual_ids = {str(r['id']) for rows in direct.values() for r in rows
                  if not r.get('planned') and r.get('type') in ('Direct', 'Check', 'AMZ Card', 'Savings Transfer')}
    planned_ids = {str(r['budget_item_id']) for rows in direct.values() for r in rows if r.get('planned') and r.get('budget_item_id') is not None}
    payday_ids = {str(r['payday_id']) for rows in direct.values() for r in rows if r.get('payday_id') is not None}
    if not actual_ids and not planned_ids and not payday_ids:
        st.html(markup)
        return
    event = ledger_interaction(data={'html': markup}, key='check_calendar',
                               on_action_change=lambda: None).action
    if event and str(event.get('id')) in actual_ids:
        require_session()
        clear_transaction_caches()
        st.session_state['calendar_edit_id'] = str(event['id'])
        edit_transaction_dialog()
    elif event and str(event.get('planned')) in planned_ids:
        require_session()
        st.session_state.pop('occurrence_edit_' + str(event['planned']), None)
        planned_occurrence_dialog(int(event['planned']))
    elif event and str(event.get('payday')) in payday_ids:
        require_session()
        st.session_state.pop('payday_edit_' + str(event['payday']), None)
        payday_occurrence_dialog(int(event['payday']))


def review_signature(row):
    # A changed row loses its marker; row positions never identify a transaction.
    return sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def render_review_rows(included, week):
    key = 'reviewed_card_rows_' + week
    valid = {review_signature(row) for row in included}
    reviewed = set(st.session_state.get(key, [])) & valid
    st.session_state[key] = sorted(reviewed)
    st.caption('Click a row to toggle its green review marker. Markers last for this signed-in session; '
               'changed rows need review again. These markers do not close the week or make a payment.')
    parts = ['<div style="display:grid;gap:4px">']
    for row in included:
        signature = review_signature(row)
        marked = signature in reviewed
        text = ' · '.join(str(row.get(field) or '') for field in ('date','merchant','category','description','direction'))
        text += f" · ${money(row['amount']):,.2f} · entry {row['id']}"
        parts.append(f'<button type="button" data-review="{signature}" aria-pressed="{str(marked).lower()}" '
                     f'style="text-align:left;white-space:normal;padding:10px;border:1px solid #84948b;'
                     f'border-radius:4px;font:inherit;background:{"#b9e5c3" if marked else "#f4f6f5"};color:#17221a">'
                     f'{"✓ Reviewed · " if marked else ""}{escape(text)}</button>')
    parts.append('</div>')
    if not included:
        st.info('No transactions in this week.')
        return
    event = ledger_interaction(data={'html': ''.join(parts)}, key='review_rows_' + week,
                               on_action_change=lambda: None).action
    if event and event.get('review') in valid:
        reviewed.symmetric_difference_update({event['review']})
        st.session_state[key] = sorted(reviewed)
        st.rerun(scope='fragment')
    st.caption(f'{len(reviewed)} of {len(included)} transactions marked reviewed.')


def card_month_weeks(month):
    return [month.replace(day=n).isoformat() for n in range(1, monthrange(month.year, month.month)[1]+1)
            if month.replace(day=n).weekday() == 5]


def month_review_html(rows, closed, month, reviewed):
    parts = ['<style>.card-month-scroll{overflow-x:auto}.card-month-tables{display:flex;gap:12px;align-items:flex-start}'
             '.card-month-tables section{flex:1 0 245px;min-width:245px}.card-month-tables table{border-collapse:collapse;width:100%;font-size:.85rem}'
             '.card-month-tables th,.card-month-tables td{border:1px solid #85958e;padding:5px;text-align:left;overflow-wrap:anywhere}'
             '.card-month-tables button{font:inherit;color:inherit;background:transparent;border:0;padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:3px;width:100%;text-align:left}'
             '.card-month-tables button:focus-visible{outline:2px solid #287cdb;outline-offset:2px}'
             '.card-month-tables header{padding:8px;border:1px solid #85958e;font-weight:600}</style>'
             '<div class="card-month-scroll"><div class="card-month-tables">']
    for number, week in enumerate(card_month_weeks(month), 1):
        included = sorted([r for r in rows if r.get('type') == 'AMZ Card' and str(r.get('card_budget_week')) == week],
                          key=lambda r: (str(r['date']), int(r['id'])))
        valid = all(r.get('direction') in ('Income', 'Expense') and money(r['amount']) >= 0 for r in included)
        total = sum((money(r['amount']) * (-1 if r.get('direction') == 'Income' else 1) for r in included), Decimal(0))
        total_text = f'${total:,.2f}' if valid else 'Needs classification'
        status = 'Reconciled' if week in closed else 'Open'
        parts.append(f'<section><header>Week {number} · {escape(total_text)}<br>'
                     f'<small>Ending {escape(week)} · {status}</small></header><table>'
                     '<thead><tr><th>Date</th><th>Amount</th><th>Merchant</th></tr></thead><tbody>')
        for row in included:
            signature = review_signature(row)
            marked = signature in reviewed.get(week, set())
            amount = money(row['amount']) * (-1 if row.get('direction') == 'Income' else 1)
            mark = 'Reviewed' if marked else 'Not reviewed'
            values = [str(row['date']), f'${amount:,.2f}', str(row.get('merchant') or 'Not provided')]
            label = escape(mark + ': ' + ' · '.join(values), quote=True)
            date_cell = (f'<button type="button" data-review="{signature}" aria-pressed="{str(marked).lower()}" '
                         f'aria-label="Toggle review: {label}" title="Toggle green review marker">{escape(values[0])}</button>')
            amount_cell = (f'<button type="button" data-card-amount="{signature}" '
                           f'aria-label="Edit transaction: {label}" title="Open full transaction editor{" — financial fields locked" if week in closed else ""}">{escape(values[1])}</button>')
            parts.append(f'<tr style="background:{"#b9e5c3" if marked else "transparent"};'
                         f'color:{"#17221a" if marked else "inherit"}">'
                         f'<td>{date_cell}</td><td>{amount_cell}</td><td>{escape(values[2])}</td></tr>')
        if not included:
            parts.append('<tr><td colspan="3">No transactions</td></tr>')
        parts.append('</tbody></table></section>')
    return ''.join(parts) + '</div></div>'


def render_month_review(rows, closed, month):
    reviewed, signature_weeks = {}, {}
    for week in card_month_weeks(month):
        valid = {review_signature(r) for r in rows if r.get('type') == 'AMZ Card' and str(r.get('card_budget_week')) == week}
        key = 'reviewed_card_rows_' + week
        reviewed[week] = set(st.session_state.get(key, [])) & valid
        st.session_state[key] = sorted(reviewed[week])
        signature_weeks.update({sig: week for sig in valid})
    st.caption('Click Date to toggle the green reviewed marker; click Amount to open the full transaction editor. '
               'Merchant is plain text. Reconciled amounts are locked. Tab to a Date or Amount button and press Enter or Space. '
               'All weeks ending in this month are shown. Credits are negative. Markers last for this signed-in session; changed entries need review again.')
    event = ledger_interaction(data={'html': month_review_html(rows, closed, month, reviewed)},
                               key='month_card_review', on_action_change=lambda: None).action
    if event and event.get('review') in signature_weeks:
        signature = event['review']
        week = signature_weeks[signature]
        reviewed[week].symmetric_difference_update({signature})
        st.session_state['reviewed_card_rows_' + week] = sorted(reviewed[week])
        st.rerun()
    elif event and event.get('card_amount'):
        selected = next((r for r in rows if r.get('type') == 'AMZ Card'
                         and review_signature(r) == event['card_amount']
                         and str(r.get('card_budget_week')) in card_month_weeks(month)), None)
        if selected is None:
            st.error('This transaction changed. Reload the review page before editing its amount.')
        else:
            require_session()
            clear_transaction_caches()
            st.session_state['calendar_edit_id'] = str(selected['id'])
            edit_transaction_dialog()


def invalidate_edited_transaction(original):
    """All full-editor routes refresh review state only after a confirmed write."""
    week = str(original.get('card_budget_week') or '')
    if week:
        key = 'reviewed_card_rows_' + week
        reviewed = set(st.session_state.get(key, []))
        reviewed.discard(review_signature(original))
        st.session_state[key] = sorted(reviewed)
        st.session_state.pop('card_payment_snapshot_' + week, None)
    clear_transaction_caches()


def payday_dates(first, last):
    """Calendar dates, with no holiday adjustment; spouse cadence is continuous."""
    result = []
    day = first
    while day <= last:
        if day.weekday() == 4:
            result.append(('Bill', day))
        if (day - date(2026, 1, 14)).days % 14 == 0:
            result.append(('Spouse', day))
        day += timedelta(days=1)
    return result


def ensure_paydays(first, last):
    result = conn.rpc('ledger_prepare_paydays', {'p_first': first.isoformat(), 'p_last': last.isoformat()}).execute()
    if result.data is not True:
        raise RuntimeError('Payday preparation failed. Install payday_review_update.sql.')


def payday_plans(rows):
    # Unset is not zero. A linked deposit is counted by the actual transaction.
    return [dict(id='payday-' + str(r['id']), payday_id=r['id'], name=r['stream'] + ' payday',
                 due_date=r['due_date'], amount=r['amount'], enabled=r['enabled'],
                 transaction_id=r.get('transaction_id'), direction='Income', description=r.get('description') or '')
            for r in rows if r['amount'] is not None]


def payday_changes(edited, originals, labels):
    changes = []
    seen = set()
    for raw in edited.to_dict('records'):
        row = {k: None if pd.isna(v) else v for k, v in raw.items()}
        key = int(row['_id'])
        if key in seen or key not in originals:
            raise ValueError('Payday rows changed. Reload the income tables.')
        seen.add(key)
        old = originals[key]
        amount = None if row['Amount'] is None else money(row['Amount'])
        if amount is not None and (amount < 0 or amount >= Decimal('1000000000')):
            raise ValueError('Enter a nonnegative amount or leave it unset.')
        link = row.get('Actual transaction') or 'Not linked'
        if link not in labels:
            raise ValueError('Choose an actual income transaction from the list.')
        enabled = bool(row['Include'])
        if labels[link] is not None and not enabled:
            raise ValueError('Unlink the deposit before clearing Include.')
        description = str(row.get('Description') or '')
        original_amount = None if old['amount'] is None else money(old['amount'])
        if (amount, enabled, description, labels[link]) == (original_amount, old['enabled'], old.get('description') or '', old.get('transaction_id')):
            continue
        changes.append(dict(id=key, revision=old['revision'], amount=None if amount is None else str(amount),
                            enabled=enabled, description=description, transaction_id=labels[link]))
    if seen != set(originals):
        raise ValueError('Payday rows cannot be added or deleted. Reload the income tables.')
    return changes


def render_payday_tables(month):
    st.subheader('Payday income')
    st.caption('Bill: every Friday. Spouse: alternate Wednesdays, anchored to January 14, 2026. '
               'Amounts are independent for each payday; blank means unset and is excluded from projections. '
               'Link each actual deposit to replace its estimate. Dates do not shift for holidays.')
    snapshot_key = 'payday_snapshot_' + month.isoformat()
    if st.button('Reload payday income / discard unsaved changes'):
        st.session_state.pop(snapshot_key, None)
        st.session_state['payday_generation'] = st.session_state.get('payday_generation', 0) + 1
        st.rerun()
    try:
        if snapshot_key not in st.session_state:
            last = month.replace(day=monthrange(month.year, month.month)[1])
            ensure_paydays(month, last)
            clear_transaction_caches()
            st.session_state[snapshot_key] = dict(rows=[r for r in load_budget_table('LedgerPaydays')
                if month.isoformat() <= r['due_date'] <= last.isoformat()], transactions=get_transactions_cached())
        snapshot = st.session_state[snapshot_key]
        labels = {'Not linked': None}
        for tx in snapshot['transactions']:
            if tx.get('type') in ('Direct', 'Check') and not tx.get('transfer_id') and tx.get('direction') == 'Income':
                labels[f"{tx['date']} · {tx.get('merchant') or 'No merchant'} · ${money(tx['amount']):,.2f} · entry {tx['id']}"] = tx['id']
        reverse = {v: k for k, v in labels.items()}
        originals = {r['id']: r for r in snapshot['rows']}
        display = []
        for r in snapshot['rows']:
            display.append({'_id': r['id'], 'Item': r['stream'], 'Date': date.fromisoformat(r['due_date']),
                'Amount': None if r['amount'] is None else float(money(r['amount'])),
                'Direction': 'Income', 'Schedule': 'Weekly' if r['stream'] == 'Bill' else 'Every 14 days',
                'Include': r['enabled'], 'Description': r.get('description') or '',
                'Actual transaction': reverse.get(r.get('transaction_id'), 'Not linked'),
                'Status': 'Linked' if r.get('transaction_id') is not None else 'Inactive' if not r['enabled'] else 'Unset' if r['amount'] is None else 'Planned'})
        frame = pd.DataFrame(display).sort_values(['Date', 'Item'])
        frame['Amount'] = pd.to_numeric(frame['Amount'], errors='coerce')
    except Exception:
        st.error('Payday income could not be loaded. Install payday_review_update.sql and reload.')
        return
    with st.form('payday_income_' + month.isoformat()):
        edits = []
        for stream in ('Bill', 'Spouse'):
            st.subheader(stream)
            edits.append(st.data_editor(frame[frame['Item'] == stream].copy(), hide_index=True,
                use_container_width=True, num_rows='fixed',
                key='payday_' + stream + month.isoformat() + '_' + str(st.session_state.get('payday_generation', 0)),
                disabled=['_id', 'Item', 'Date', 'Direction', 'Schedule', 'Status'],
                column_order=['Item', 'Amount', 'Date', 'Direction', 'Schedule', 'Include', 'Description', 'Actual transaction', 'Status'],
                column_config={'_id': None, 'Amount': st.column_config.NumberColumn(min_value=0.0,max_value=999999999.99,format='$%.2f'),
                    'Date': st.column_config.DateColumn(format='MMM D, YYYY'),
                    'Actual transaction': st.column_config.SelectboxColumn(options=list(labels), required=True)}))
        saved = st.form_submit_button('Save payday income', type='primary')
    if saved:
        try:
            changes = payday_changes(pd.concat(edits, ignore_index=True), originals, labels)
            if not changes:
                st.info('No payday changes to save.')
                return
            require_session()
            result = conn.rpc('ledger_save_paydays', {'p_changes': changes}).execute()
            if result.data is not True:
                raise RuntimeError('Save not confirmed')
            invalidate_budget_views()
            st.session_state['payday_generation'] = st.session_state.get('payday_generation', 0) + 1
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error('No payday changes were saved. Reload to check for stale edits or a deposit already linked to another plan.')


@st.dialog('Edit this payday amount', width='medium')
def payday_occurrence_dialog(payday_id):
    require_session()
    key = 'payday_edit_' + str(payday_id)
    try:
        if key not in st.session_state:
            st.session_state[key] = next(r for r in load_budget_table('LedgerPaydays') if r['id'] == payday_id)
        row = st.session_state[key]
    except Exception:
        st.error('Payday could not be loaded. Reload the calendar.')
        return
    if not row['enabled'] or row.get('transaction_id') is not None:
        st.error('This estimate is inactive or linked. Reload and open the actual deposit instead.')
        return
    st.write(row['stream'] + ' · ' + row['due_date'])
    st.caption('Only this payday changes. No amount is copied to other paydays.')
    with st.form(key + '_form'):
        amount = st.number_input('Amount ($)', value=None if row['amount'] is None else float(money(row['amount'])),
                                 min_value=0.0, max_value=999999999.99, format='%.2f')
        saved = st.form_submit_button('Save this payday')
    if saved:
        try:
            result = conn.rpc('ledger_save_paydays', {'p_changes': [dict(id=row['id'], revision=row['revision'],
                amount=None if amount is None else str(money(amount)), enabled=row['enabled'],
                description=row.get('description') or '', transaction_id=row.get('transaction_id'))]}).execute()
            if result.data is not True:
                raise RuntimeError('Save not confirmed')
            invalidate_budget_views()
            st.session_state.pop(key, None)
            st.rerun()
        except Exception:
            st.error('Payday was not saved. Close and reopen it to check for another edit.')


def reconcile_card_dialog():
    require_session()
    st.title('AMZ Card Ledger')
    month = st.date_input('Review month', value=date(CALENDAR_YEAR, CALENDAR_MONTH, 1), key='card_review_month').replace(day=1)
    try:
        ensure_unified_budget_months(month - timedelta(days=7), month)
        clear_transaction_caches()
        rows = get_transactions_cached()
        settings = load_calendar_settings()
        closed = load_card_weeks()
    except Exception:
        st.error('Could not load card weeks. Apply card_reconciliation.sql and check the connection.')
        return
    render_month_review(rows, closed, month)
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
    st.caption('The selected week below is the one whose payment will be recorded. Review markers above never record a payment.')
    if week not in card_month_weeks(month):
        st.info('This week is outside the review month. Change Review month to include its ending Saturday before closing it.')
        return
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
    confirmation_key = 'card_payment_snapshot_' + week
    with st.form('confirm_card_payment_' + week):
        payment_date = st.date_input('Actual payment date', value=datetime.now(LOCAL_TZ).date(),
                                    max_value=datetime.now(LOCAL_TZ).date())
        confirmed = st.checkbox('I reconciled these transactions and paid the amount shown.')
        submitted = st.form_submit_button('Reconcile and mark paid', type='primary')
    if not submitted:
        st.session_state[confirmation_key] = {'rows': deepcopy(included), 'budget': str(budget)}
    if submitted:
        if not confirmed:
            st.error('Confirm the reviewed transactions and payment first.')
            return
        try:
            snapshot = st.session_state.get(confirmation_key)
            if not snapshot:
                raise ValueError('Reload and review the selected week before closing.')
            expected = [{'id': row['id'], 'amount': float(money(row['amount'])),
                         'direction': row['direction']} for row in snapshot['rows']]
            response = conn.rpc('ledger_reconcile_card_week', {
                'p_week': week, 'p_payment_date': payment_date.isoformat(),
                'p_expected': expected, 'p_expected_budget': str(money(snapshot['budget'])),
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
    if row.get('planned') and row.get('payday_id') is not None:
        return (f'<button type="button" data-payday="{int(row["payday_id"])}" '
                f'title="{escape(tooltip, quote=True)}" aria-label="{escape("Edit payday: " + tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:0;background:transparent; '
                f'color:#238636;padding:3px 0;font:inherit;cursor:pointer">{escape(displayed)}</button>')
    if row.get('planned') and row.get('budget_item_id') is not None:
        return (f'<button type="button" data-planned="{int(row["budget_item_id"])}" '
                f'title="{escape(tooltip, quote=True)}" aria-label="{escape("Edit this month: " + tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:0;background:transparent; '
                f'color:{"#238636" if is_income else "#d14343"};padding:3px 0;font:inherit;cursor:pointer">'
                f'{escape(displayed)}</button>')
    if row.get('type') == 'Check':
        cleared = bool(row.get('check_cleared_at'))
        status = 'Cleared' if cleared else 'Uncleared'
        tooltip += '\nCheck: ' + status
        background = '#b9e5c3' if cleared else '#ffe28a'
        action = f' data-transaction="{int(row["id"])}"'
        return (f'<button type="button"{action} title="{escape(tooltip, quote=True)}" '
                f'aria-label="{escape(tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:1px solid #6b7280;'
                f'border-radius:3px;padding:6px;margin:3px 0;background:{background};color:#17221a;'
                f'font:inherit;cursor:pointer;">'
                f'{escape(displayed)} {"✓" if cleared else ""}</button>')
    if not row.get('planned') and row.get('type') in ('Direct', 'AMZ Card', 'Savings Transfer'):
        return (f'<button type="button" data-transaction="{int(row["id"])}" '
                f'title="{escape(tooltip, quote=True)}" aria-label="{escape("Edit transaction: " + tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:0;background:transparent;'
                f'color:{"#238636" if is_income else "#d14343"};padding:3px 0;font:inherit;cursor:pointer;">'
                f'{escape(displayed)}</button>')
    color = '#238636' if is_income else '#d14343' 
    return (
        f'<span tabindex="0" title="{escape(tooltip, quote=True)}" '
        f'aria-label="{escape(tooltip, quote=True)}" '
        f'style="display:block;cursor:help;color:{color};padding:3px 0;'
        f'font-variant-numeric:tabular-nums;">{escape(displayed)}</span>'
    )


def calendar_grid_html(first, balances, direct, settings, show_cards=True):
    """One CSS grid gives every day the height required by the busiest day."""
    cells = []
    for week in Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        for day in week:
            if day.month != first.month:
                cells.append('<div class="ledger-day ledger-outside"><header><span class="ledger-date">'
                             + escape(day.strftime('%b %d')) + '</span></header><div class="ledger-body"></div><footer>&nbsp;</footer></div>')
                continue
            values = balances[day]
            content = [calendar_entry_html(row) for row in sorted(direct.get(day.isoformat(), []), key=lambda row: str(row['id']))]
            for payment in values['payments']:
                content.append(calendar_entry_html({
                    'amount': payment['paid_amount'], 'direction': 'Expense',
                    'merchant': 'Credit card payment',
                    'description': f"Reconciled budget week ending {payment['week_ending']}",
                }))
            if show_cards and day.weekday() == 5:
                if values['completed']:
                    if values['surplus'] > 0:
                        content.append(f'<div class="ledger-note">Budget surplus: ${values["surplus"]:,.2f}</div>')
                else:
                    content.append(calendar_entry_html({
                        'amount': values['remaining'], 'direction': 'Expense',
                        'merchant': 'Weekly card budget remaining',
                        'description': f"Budget: ${values['budget']:,.2f}; spent: ${values['spent']:,.2f}",
                    }))
                content.append(f'<div class="ledger-card-spent" title="Card spent">${values["spent"]:,.2f}</div>')
                if values['spent'] > values['budget'] and 'budget:' + day.isoformat() in settings:
                    content.append(f'<div class="ledger-note">Over budget: ${values["spent"]-values["budget"]:,.2f}</div>')
            cells.append(
                '<div class="ledger-day"><header>'
                f'<span class="ledger-date">{day.day}</span><span class="ledger-balance">${values["balance"]:,.2f}</span>'
                '</header><div class="ledger-body">' + ''.join(content) + '</div>'
                f'<footer>Day net: ${values["net"]:,.2f}</footer></div>')
    style = '''<style>
    .ledger-calendar-scroll {overflow-x:auto;padding-bottom:8px;}
    .ledger-calendar {min-width:840px;color:inherit;}
    .ledger-weekdays,.ledger-days {display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;}
    .ledger-weekdays {font-weight:600;margin-bottom:6px;text-align:center;}
    .ledger-days {grid-auto-rows:1fr;}
    .ledger-day {display:flex;flex-direction:column;min-height:240px;min-width:0;border:1px solid #8a96a5;border-radius:6px;overflow:hidden;}
    .ledger-day header {display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:7px;border-bottom:1px solid #8a96a5;background:rgba(127,150,180,.12);}
    .ledger-date {display:inline-flex;align-items:center;justify-content:center;min-width:30px;min-height:30px;padding:2px 5px;box-sizing:border-box;border:1px solid #8a96a5;border-radius:3px;background:rgba(127,150,180,.2);font-weight:700;}
    .ledger-balance {font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;}
    .ledger-body {padding:8px;flex:1;overflow-wrap:anywhere;}
    .ledger-day footer {padding:7px;border-top:1px solid #8a96a5;background:rgba(127,150,180,.12);font-size:.85rem;font-variant-numeric:tabular-nums;}
    .ledger-card-spent {background:#00b4e6;color:#002b36;padding:6px;border-radius:3px;font-weight:600;margin-top:6px;}
    .ledger-note {font-size:.85rem;padding:4px 0;}
    .ledger-outside {opacity:.55;}
    </style>'''
    names = ''.join('<div>'+name+'</div>' for name in ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'])
    return style + '<div class="ledger-calendar-scroll"><div class="ledger-calendar"><div class="ledger-weekdays">' + names + '</div><div class="ledger-days">' + ''.join(cells) + '</div></div></div>'


def render_editable_calendar():
    first = st.date_input('Calendar month', value=date(CALENDAR_YEAR, CALENDAR_MONTH, 1), key='calendar_month_choice').replace(day=1)
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    heading, summary = st.columns([3, 2], gap='large')
    heading.title(cash_accounts()[active_cash_id()]['name'] + ': Cash Flow Calendar')
    summary_slot = summary.empty()
    st.subheader(first.strftime('%B %Y'))
    try:
        settings = load_calendar_settings()
        opening_dates = [date.fromisoformat(key.split(':', 1)[1]) for key in settings
                         if key.startswith('opening:') and key.split(':', 1)[1] <= first.isoformat()]
        if opening_dates:
            ensure_unified_budget_months(max(opening_dates), first)
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
        cutover = load_budget_table('LedgerUnifiedConfig')[0]['cutover_date']
        planned = [r for r in load_budget_table('LedgerBudgetItems')
                   if int(r['cash_account_id']) == active_cash_id() and r['due_date'] < cutover]
        if active_cash_id() == 1: ensure_paydays(anchor_date, last)
        paydays = load_budget_table('LedgerPaydays') if active_cash_id() == 1 else []
        planned += payday_plans(paydays)
        balances, anchor = calendar_balances(transactions, settings, first, last,
                                             reconciled=reconciled, planned=planned)
    except Exception:
        logger.exception('Budget calendar load failed.')
        st.error('The calendar could not calculate. Apply budget_setup.sql and check the connection and transaction classifications.')
        return

    if active_cash_id() == 1:
        st.caption('Hover over a transaction amount for its description and merchant. '
                   'Use the sidebar to add transactions or click an actual calendar entry to edit. Card purchases are '
                   'entered as positive AMZ Card transactions and assigned to a budget week. '
                   'Do not enter the same card payment again as a Direct expense.')
        st.caption('Open card weeks reserve spending plus remaining budget on Saturday. '
                   'Use AMZ Card Ledger after paying: the actual payment date then controls '
                   'the deduction, and unused budget is retained as surplus.')
    if reconciled:
        next_week = date.fromisoformat(max(reconciled)) + timedelta(days=7)
        st.info(f'New card entries apply to the budget week ending {next_week:%b %d, %Y}, regardless of transaction date.')
    if anchor < first:
        st.caption(f'Opening balance carried forward from the saved balance on {anchor:%b %d, %Y}.')
    direct = {}
    for row in transactions:
        if row.get('type') in ('Direct', 'Check', 'Savings Transfer') or (
            row.get('type') == 'AMZ Card' and row.get('unified_occurrence_id') is not None):
            direct.setdefault(str(row['date'])[:10], []).append(row)
    for item in planned:
        if item['enabled'] and item.get('transaction_id') is None:
            direct.setdefault(item['due_date'], []).append({
                'id': 'planned-' + str(item['id']), 'amount': item['amount'],
                'direction': item['direction'], 'merchant': item['name'],
                'description': 'Planned: ' + (item.get('description') or item['name']),
                'planned': True, 'budget_item_id': None if item.get('payday_id') or item.get('transfer_schedule_id') else item['id'],
                'payday_id': item.get('payday_id'),
            })
    unset = sum(1 for r in paydays if r['enabled'] and r['amount'] is None and r.get('transaction_id') is None
                and anchor.isoformat() <= r['due_date'] <= last.isoformat())
    if unset:
        st.warning(f'{unset} payday amounts are unset in the balance period and excluded from projections. Enter them in Budget → Payday income.')
    st.caption('Active shared-budget items are recorded on their scheduled dates. Older planned items before the budget changeover and unset payday amounts remain estimates.')
    # Render as HTML directly: Markdown interprets dollar amounts as math and
    # can break markup around multiline tooltip attributes.
    render_check_calendar(first, balances, direct, settings)
    with summary_slot.container():
        st.metric('Projected month-end balance', f"${balances[last]['balance']:,.2f}")
        monthly_surplus = sum((values['surplus'] for values in balances.values()), Decimal(0))
        if active_cash_id() == 1: st.metric('Monthly budget surplus — completed weeks', f"${monthly_surplus:,.2f}")
    if active_cash_id() == 1:
        st.caption('Includes completed weeks whose Saturday falls in this month. '
                   'Over-budget weeks show zero surplus and are flagged above. '
                   'Reconciled payments and budget surplus are preserved from the saved reconciliation.')
    st.divider()
    st.subheader('Transaction Register & Schedule Mapping')
    st.dataframe(pd.DataFrame([{'Date':t['date'],'Type':'Transfer' if t.get('transfer_id') or t['type']=='Savings Transfer' else t['type'],
        'Direction':t['direction'],'Amount':float(money(t['amount'])),'Merchant':t.get('merchant',''),
        'Category':'Transfer' if t.get('transfer_id') or t['type']=='Savings Transfer' else t.get('category',''),
        'Description':t.get('description','')} for t in transactions]), use_container_width=True, hide_index=True)


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


def ensure_unified_budget_months(first, last):
    current = first.replace(day=1)
    final = last.replace(day=1)
    while current <= final:
        response = conn.rpc('ledger_prepare_unified_budget_month',
            {'p_month': current.isoformat()}).execute()
        if response.data is not True:
            raise RuntimeError('The shared budget month was not prepared.')
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    clear_transaction_caches()


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
        definition_fields = [f for f in ('Item','Amount','Day','Direction','Schedule','Description')
                             if previous is None or row.get(f) != previous.get(f)]
        scope = ('This month and future months' if definition_fields else 'This month only') if kind == 'bill' else (row.get('Apply change') or 'This month only')
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
        if kind == 'bill':
            payload['definition_fields'] = definition_fields
        changes.append(payload)
    return changes


def unified_account_options():
    checking = {'c:' + str(i): row['name'] for i, row in cash_accounts().items()}
    savings = {'s:' + str(i): row['name'] for i, row in savings_accounts().items()}
    return checking | savings


def save_unified_budget_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise RuntimeError('Save was not confirmed.')
        clear_transaction_caches()
        st.session_state['unified_budget_generation'] = st.session_state.get('unified_budget_generation', 0) + 1
        st.rerun()
    except Exception as exc:
        st.error('Nothing was saved: ' + str(getattr(exc, 'message', None) or exc))


@st.dialog('Budget item', width='large')
def unified_budget_item_dialog(rule, selected_month):
    require_session()
    accounts = unified_account_options()
    payees = load_budget_table('LedgerUnifiedPayees')
    generation = str(st.session_state.get('unified_budget_generation', 0))
    identity = str(rule['id']) if rule else 'new'
    types = ['AMZ Card', 'Direct', 'Check', 'Transfer']
    current_type = rule['transaction_type'] if rule else 'Direct'
    transaction_type = st.selectbox('Transaction type', types,
        index=types.index(current_type), key='budget_type_' + identity + '_' + generation)
    source_default = rule['paid_from'] if rule else 'c:1'
    if transaction_type == 'AMZ Card':
        paid_from = 'c:1'
        st.text_input('Paid from', value=accounts['c:1'], disabled=True)
    else:
        available_sources = list(accounts)
        paid_from = st.selectbox('Paid from', available_sources,
            index=available_sources.index(source_default) if source_default in available_sources else 0,
            format_func=accounts.get, key='budget_source_' + identity + '_' + generation)
    schedules = ['Monthly', 'Weekly', 'As needed']
    current_schedule = rule['schedule'] if rule else 'Monthly'
    schedule = st.selectbox('Schedule', schedules, index=schedules.index(current_schedule),
        key='budget_schedule_' + identity + '_' + generation)
    if transaction_type == 'Transfer':
        destinations = [key for key in accounts if key != paid_from]
        if not destinations:
            st.error('Create another account before adding a transfer.')
            return
        current_target = rule['paid_to'] if rule else destinations[0]
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0,
            format_func=accounts.get)
    else:
        destinations = [row['label'] for row in payees if row['transaction_type'] == transaction_type]
        current_target = rule['paid_to'] if rule else 'Merchant'
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0)
    with st.form('budget_item_form_' + identity + '_' + generation):
        name = st.text_input('Item', value=rule['name'] if rule else '')
        amount = st.number_input('Amount', min_value=0.0, max_value=999999999.99,
            value=float(money(rule['amount'])) if rule else None, format='%.2f')
        if schedule == 'Weekly':
            weekday = st.selectbox('Day of week', list(range(7)),
                index=int(rule['weekday']) if rule and rule['weekday'] is not None else 4,
                format_func=lambda day: ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][day])
            day_of_month = None
        else:
            day_of_month = st.number_input('Day of month', min_value=1, max_value=31,
                value=int(rule['day_of_month']) if rule and rule['day_of_month'] is not None else 1)
            weekday = None
        description = st.text_input('Description', value=rule['description'] if rule else '')
        enabled = st.checkbox('Include', value=bool(rule['enabled']) if rule else True)
        if rule:
            st.caption('Changes to this recurring item begin with future occurrences. Earlier entries stay as recorded.')
        save = st.form_submit_button('Save budget item', type='primary')
    if save:
        if amount is None:
            st.error('Enter an amount.')
            return
        effective = max(selected_month, datetime.now(LOCAL_TZ).date() + timedelta(days=1)) if rule else selected_month
        details = dict(name=name.strip(),amount=str(money(amount)),description=description,
            transaction_type=transaction_type,paid_from=paid_from,paid_to=paid_to,
            schedule=schedule,day_of_month=day_of_month,weekday=weekday,enabled=enabled)
        save_unified_budget_action('ledger_save_unified_budget_rule', dict(
            p_id=rule['id'] if rule else None,
            p_revision=rule['revision'] if rule else None,
            p_effective_from=effective.isoformat(),p_data=details))


def unified_account_options():
    checking = {'c:' + str(i): row['name'] for i, row in cash_accounts().items()}
    savings = {'s:' + str(i): row['name'] for i, row in savings_accounts().items()}
    return checking | savings


def save_unified_budget_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise RuntimeError('Save was not confirmed.')
        clear_transaction_caches()
        st.session_state['unified_budget_generation'] = st.session_state.get('unified_budget_generation', 0) + 1
        st.rerun()
    except Exception as exc:
        st.error('Nothing was saved: ' + str(getattr(exc, 'message', None) or exc))


@st.dialog('Budget item', width='large')
def unified_budget_item_dialog(rule, selected_month):
    require_session()
    accounts = unified_account_options()
    payees = load_budget_table('LedgerUnifiedPayees')
    generation = str(st.session_state.get('unified_budget_generation', 0))
    identity = str(rule['id']) if rule else 'new'
    types = ['AMZ Card', 'Direct', 'Check', 'Transfer']
    current_type = rule['transaction_type'] if rule else 'Direct'
    transaction_type = st.selectbox('Transaction type', types,
        index=types.index(current_type), key='budget_type_' + identity + '_' + generation)
    source_default = rule['paid_from'] if rule else 'c:1'
    if transaction_type == 'AMZ Card':
        paid_from = 'c:1'
        st.text_input('Paid from', value=accounts['c:1'], disabled=True)
    else:
        available_sources = list(accounts)
        paid_from = st.selectbox('Paid from', available_sources,
            index=available_sources.index(source_default) if source_default in available_sources else 0,
            format_func=accounts.get, key='budget_source_' + identity + '_' + generation)
    schedules = ['Monthly', 'Weekly', 'As needed']
    current_schedule = rule['schedule'] if rule else 'Monthly'
    schedule = st.selectbox('Schedule', schedules, index=schedules.index(current_schedule),
        key='budget_schedule_' + identity + '_' + generation)
    if transaction_type == 'Transfer':
        destinations = [key for key in accounts if key != paid_from]
        if not destinations:
            st.error('Create another account before adding a transfer.')
            return
        current_target = rule['paid_to'] if rule else destinations[0]
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0,
            format_func=accounts.get)
    else:
        destinations = [row['label'] for row in payees if row['transaction_type'] == transaction_type]
        current_target = rule['paid_to'] if rule else 'Merchant'
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0)
    with st.form('budget_item_form_' + identity + '_' + generation):
        name = st.text_input('Item', value=rule['name'] if rule else '')
        amount = st.number_input('Amount', min_value=0.0, max_value=999999999.99,
            value=float(money(rule['amount'])) if rule else None, format='%.2f')
        if schedule == 'Weekly':
            weekday = st.selectbox('Day of week', list(range(7)),
                index=int(rule['weekday']) if rule and rule['weekday'] is not None else 4,
                format_func=lambda day: ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][day])
            day_of_month = None
        else:
            day_of_month = st.number_input('Day of month', min_value=1, max_value=31,
                value=int(rule['day_of_month']) if rule and rule['day_of_month'] is not None else 1)
            weekday = None
        description = st.text_input('Description', value=rule['description'] if rule else '')
        enabled = st.checkbox('Include', value=bool(rule['enabled']) if rule else True)
        if rule:
            st.caption('Changes to this recurring item begin with future occurrences. Earlier entries stay as recorded.')
        save = st.form_submit_button('Save budget item', type='primary')
    if save:
        if amount is None:
            st.error('Enter an amount.')
            return
        effective = max(selected_month, datetime.now(LOCAL_TZ).date() + timedelta(days=1)) if rule else selected_month
        details = dict(name=name.strip(),amount=str(money(amount)),description=description,
            transaction_type=transaction_type,paid_from=paid_from,paid_to=paid_to,
            schedule=schedule,day_of_month=day_of_month,weekday=weekday,enabled=enabled)
        save_unified_budget_action('ledger_save_unified_budget_rule', dict(
            p_id=rule['id'] if rule else None,
            p_revision=rule['revision'] if rule else None,
            p_effective_from=effective.isoformat(),p_data=details))


def unified_account_options():
    checking = {'c:' + str(i): row['name'] for i, row in cash_accounts().items()}
    savings = {'s:' + str(i): row['name'] for i, row in savings_accounts().items()}
    return checking | savings


def save_unified_budget_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise RuntimeError('Save was not confirmed.')
        clear_transaction_caches()
        st.session_state['unified_budget_generation'] = st.session_state.get('unified_budget_generation', 0) + 1
        st.rerun()
    except Exception as exc:
        st.error('Nothing was saved: ' + str(getattr(exc, 'message', None) or exc))


@st.dialog('Budget item', width='large')
def unified_budget_item_dialog(rule, selected_month):
    require_session()
    accounts = unified_account_options()
    payees = load_budget_table('LedgerUnifiedPayees')
    generation = str(st.session_state.get('unified_budget_generation', 0))
    identity = str(rule['id']) if rule else 'new'
    types = ['AMZ Card', 'Direct', 'Check', 'Transfer']
    current_type = rule['transaction_type'] if rule else 'Direct'
    transaction_type = st.selectbox('Transaction type', types,
        index=types.index(current_type), key='budget_type_' + identity + '_' + generation)
    source_default = rule['paid_from'] if rule else 'c:1'
    if transaction_type == 'AMZ Card':
        paid_from = 'c:1'
        st.text_input('Paid from', value=accounts['c:1'], disabled=True)
    else:
        available_sources = list(accounts)
        paid_from = st.selectbox('Paid from', available_sources,
            index=available_sources.index(source_default) if source_default in available_sources else 0,
            format_func=accounts.get, key='budget_source_' + identity + '_' + generation)
    schedules = ['Monthly', 'Weekly', 'As needed']
    current_schedule = rule['schedule'] if rule else 'Monthly'
    schedule = st.selectbox('Schedule', schedules, index=schedules.index(current_schedule),
        key='budget_schedule_' + identity + '_' + generation)
    if transaction_type == 'Transfer':
        destinations = [key for key in accounts if key != paid_from]
        if not destinations:
            st.error('Create another account before adding a transfer.')
            return
        current_target = rule['paid_to'] if rule else destinations[0]
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0,
            format_func=accounts.get)
    else:
        destinations = [row['label'] for row in payees if row['transaction_type'] == transaction_type]
        current_target = rule['paid_to'] if rule else 'Merchant'
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0)
    with st.form('budget_item_form_' + identity + '_' + generation):
        name = st.text_input('Item', value=rule['name'] if rule else '')
        amount = st.number_input('Amount', min_value=0.0, max_value=999999999.99,
            value=float(money(rule['amount'])) if rule else None, format='%.2f')
        if schedule == 'Weekly':
            weekday = st.selectbox('Day of week', list(range(7)),
                index=int(rule['weekday']) if rule and rule['weekday'] is not None else 4,
                format_func=lambda day: ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][day])
            day_of_month = None
        else:
            day_of_month = st.number_input('Day of month', min_value=1, max_value=31,
                value=int(rule['day_of_month']) if rule and rule['day_of_month'] is not None else 1)
            weekday = None
        description = st.text_input('Description', value=rule['description'] if rule else '')
        enabled = st.checkbox('Include', value=bool(rule['enabled']) if rule else True)
        if rule:
            st.caption('Changes to this recurring item begin with future occurrences. Earlier entries stay as recorded.')
        save = st.form_submit_button('Save budget item', type='primary')
    if save:
        if amount is None:
            st.error('Enter an amount.')
            return
        today = datetime.now(LOCAL_TZ).date()
        effective = (max(selected_month, today + timedelta(days=1 if rule['enabled'] else 0))
            if rule else max(selected_month, today))
        details = dict(name=name.strip(),amount=str(money(amount)),description=description,
            transaction_type=transaction_type,paid_from=paid_from,paid_to=paid_to,
            schedule=schedule,day_of_month=day_of_month,weekday=weekday,enabled=enabled)
        save_unified_budget_action('ledger_save_unified_budget_rule', dict(
            p_id=rule['id'] if rule else None,
            p_revision=rule['revision'] if rule else None,
            p_effective_from=effective.isoformat(),p_data=details))


def unified_account_options():
    checking = {'c:' + str(i): row['name'] for i, row in cash_accounts().items()}
    savings = {'s:' + str(i): row['name'] for i, row in savings_accounts().items()}
    return checking | savings


def save_unified_budget_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise RuntimeError('Save was not confirmed.')
        clear_transaction_caches()
        st.session_state['unified_budget_generation'] = st.session_state.get('unified_budget_generation', 0) + 1
        st.rerun()
    except Exception as exc:
        st.error('Nothing was saved: ' + str(getattr(exc, 'message', None) or exc))


@st.dialog('Budget item', width='large')
def unified_budget_item_dialog(rule, selected_month):
    require_session()
    accounts = unified_account_options()
    payees = load_budget_table('LedgerUnifiedPayees')
    generation = str(st.session_state.get('unified_budget_generation', 0))
    identity = str(rule['id']) if rule else 'new'
    types = ['AMZ Card', 'Direct', 'Check', 'Transfer']
    current_type = rule['transaction_type'] if rule else 'Direct'
    transaction_type = st.selectbox('Transaction type', types,
        index=types.index(current_type), key='budget_type_' + identity + '_' + generation)
    source_default = rule['paid_from'] if rule else 'c:1'
    if transaction_type == 'AMZ Card':
        paid_from = 'c:1'
        st.text_input('Paid from', value=accounts['c:1'], disabled=True)
    else:
        available_sources = list(accounts)
        paid_from = st.selectbox('Paid from', available_sources,
            index=available_sources.index(source_default) if source_default in available_sources else 0,
            format_func=accounts.get, key='budget_source_' + identity + '_' + generation)
    schedules = ['Monthly', 'Weekly', 'As needed']
    current_schedule = rule['schedule'] if rule else 'Monthly'
    schedule = st.selectbox('Schedule', schedules, index=schedules.index(current_schedule),
        key='budget_schedule_' + identity + '_' + generation)
    if transaction_type == 'Transfer':
        destinations = [key for key in accounts if key != paid_from]
        if not destinations:
            st.error('Create another account before adding a transfer.')
            return
        current_target = rule['paid_to'] if rule else destinations[0]
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0,
            format_func=accounts.get)
    else:
        destinations = [row['label'] for row in payees if row['transaction_type'] == transaction_type]
        current_target = rule['paid_to'] if rule else 'Merchant'
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0)
    with st.form('budget_item_form_' + identity + '_' + generation):
        name = st.text_input('Item', value=rule['name'] if rule else '')
        amount = st.number_input('Amount', min_value=0.0, max_value=999999999.99,
            value=float(money(rule['amount'])) if rule else None, format='%.2f')
        if schedule == 'Weekly':
            weekday = st.selectbox('Day of week', list(range(7)),
                index=int(rule['weekday']) if rule and rule['weekday'] is not None else 4,
                format_func=lambda day: ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][day])
            day_of_month = None
        else:
            day_of_month = st.number_input('Day of month', min_value=1, max_value=31,
                value=int(rule['day_of_month']) if rule and rule['day_of_month'] is not None else 1)
            weekday = None
        description = st.text_input('Description', value=rule['description'] if rule else '')
        enabled = st.checkbox('Include', value=bool(rule['enabled']) if rule else True)
        if rule:
            st.caption('Changes to this recurring item begin with future occurrences. Earlier entries stay as recorded.')
        save = st.form_submit_button('Save budget item', type='primary')
    if save:
        if amount is None:
            st.error('Enter an amount.')
            return
        effective = (max(selected_month, datetime.now(LOCAL_TZ).date() +
            timedelta(days=1 if rule['enabled'] else 0)) if rule else selected_month)
        details = dict(name=name.strip(),amount=str(money(amount)),description=description,
            transaction_type=transaction_type,paid_from=paid_from,paid_to=paid_to,
            schedule=schedule,day_of_month=day_of_month,weekday=weekday,enabled=enabled)
        save_unified_budget_action('ledger_save_unified_budget_rule', dict(
            p_id=rule['id'] if rule else None,
            p_revision=rule['revision'] if rule else None,
            p_effective_from=effective.isoformat(),p_data=details))


@st.dialog('Edit one savings entry')
def unified_savings_occurrence_dialog(occurrence):
    st.write(occurrence['name'])
    st.caption('This changes only the selected occurrence and its savings balance.')
    with st.form('savings_occurrence_' + str(occurrence['id'])):
        day = st.date_input('Date', value=date.fromisoformat(occurrence['actual_date']))
        amount = st.number_input('Amount', min_value=0.0, max_value=999999999.99,
            value=float(money(occurrence['amount'])), format='%.2f')
        save = st.form_submit_button('Save this occurrence', type='primary')
        delete = st.form_submit_button('Remove this occurrence')
    if save or delete:
        save_unified_budget_action('ledger_edit_unified_savings_occurrence', dict(
            p_id=occurrence['id'],p_revision=occurrence['revision'],
            p_date=day.isoformat() if save else None,
            p_amount=str(money(amount)) if save else None,p_delete=delete))


def render_budget_page():
    require_session()
    st.title('View / Edit Budget')
    selected_month = st.date_input('Budget month', value=date(CALENDAR_YEAR, CALENDAR_MONTH, 1),
        key='unified_budget_month').replace(day=1)
    try:
        response = conn.rpc('ledger_prepare_unified_budget_month',
            {'p_month': selected_month.isoformat()}).execute()
        if response.data is not True:
            raise RuntimeError('The budget month was not prepared.')
        rules = load_budget_table('LedgerUnifiedBudgetRules')
        occurrences = load_budget_table('LedgerUnifiedBudgetOccurrences')
        accounts = unified_account_options()
    except Exception:
        logger.exception('Unified budget load failed.')
        st.error('The shared budget could not be loaded. Check that unified_budget_update.sql is installed.')
        return
    month_end = selected_month.replace(day=monthrange(selected_month.year, selected_month.month)[1])
    visible = [r for r in rules if r['effective_from'] <= month_end.isoformat()
        and (r['effective_until'] is None or r['effective_until'] >= selected_month.isoformat())]
    visible.sort(key=lambda row: (row['name'].casefold(), int(row['id'])))
    rows = []
    for rule in visible:
        matching = [o for o in occurrences if int(o['rule_id']) == int(rule['id'])
            and o['scheduled_date'][:7] == selected_month.isoformat()[:7]]
        day = (['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][int(rule['weekday'])]
               if rule['schedule'] == 'Weekly' else str(rule['day_of_month']))
        rows.append({'ID#': int(rule['id']), 'Item': rule['name'],
            'Transaction type': rule['transaction_type'],
            'Paid from': accounts.get(rule['paid_from'], 'Unavailable account'),
            'Paid to': accounts.get(rule['paid_to'], rule['paid_to']),
            'Amount': float(money(rule['amount'])), 'Day': day,
            'Schedule': rule['schedule'], 'Include': bool(rule['enabled']),
            'Description': rule['description'],
            'Actual transaction': f'{sum(not o["cancelled"] for o in matching)} entries',
            'Apply change': 'Future occurrences',
            'Status': 'Active' if rule['enabled'] else 'Inactive'})
    st.subheader('Budgeted bills and income')
    st.caption('Each item is an expense. Payday income is managed below. Click an ID# to edit an item.')
    frame = pd.DataFrame(rows, columns=['ID#','Item','Transaction type','Paid from','Paid to',
        'Amount','Day','Schedule','Include','Description','Actual transaction','Apply change','Status'])
    event = st.dataframe(frame, hide_index=True, use_container_width=True,
        key='unified_budget_table_' + str(st.session_state.get('unified_budget_generation', 0)),
        on_select='rerun', selection_mode='single-cell',
        column_config={'Amount': st.column_config.NumberColumn(format='$%.2f')})
    cells = event.selection.cells
    selection_token = (str(selected_month), cells[0][0]) if cells and cells[0][1] == 'ID#' else None
    if selection_token is None:
        st.session_state.pop('unified_budget_handled_selection', None)
    add_item = st.button('Add budget item', type='primary')
    if add_item:
        unified_budget_item_dialog(None, selected_month)
    elif selection_token is not None and 0 <= selection_token[1] < len(visible):
        if st.session_state.get('unified_budget_handled_selection') != selection_token:
            st.session_state['unified_budget_handled_selection'] = selection_token
            unified_budget_item_dialog(visible[selection_token[1]], selected_month)
    with st.expander('Paid to values'):
        st.caption('Merchant is the default value. The Item field holds the merchant name.')
        with st.form('add_unified_payee'):
            payee_type = st.selectbox('Transaction type', ['AMZ Card','Direct','Check'])
            payee_label = st.text_input('New Paid to value')
            add_payee = st.form_submit_button('Add value')
        if add_payee:
            save_unified_budget_action('ledger_add_unified_payee',
                {'p_type':payee_type,'p_label':payee_label})
    savings_entries = [o for o in occurrences if not o['cancelled']
        and o['paid_from'].startswith('s:') and o['transaction_type'] != 'Transfer'
        and o['scheduled_date'][:7] == selected_month.isoformat()[:7]]
    if savings_entries:
        with st.expander('Individual expenses paid from savings'):
            selected = st.selectbox('Savings entry', savings_entries,
                format_func=lambda o: o['actual_date'] + ' · ' + o['name'] + ' · $' + str(o['amount']))
            if st.button('Edit selected savings entry'):
                unified_savings_occurrence_dialog(selected)
    transfer_entries = [o for o in occurrences if not o['cancelled']
        and o['transfer_id'] is not None and o['scheduled_date'][:7] == selected_month.isoformat()[:7]]
    if transfer_entries:
        with st.expander('Individual budget transfers'):
            selected = st.selectbox('Transfer entry', transfer_entries,
                format_func=lambda o: o['actual_date'] + ' · ' + o['name'] + ' · $' + str(o['amount']))
            linked = next((t for t in load_budget_table('LedgerTransfers')
                if int(t['id']) == int(selected['transfer_id'])), None)
            if linked:
                transfer_form(linked)
    st.divider()
    previous_account = active_cash_id()
    st.session_state['cash_account_id'] = 1
    try:
        render_payday_tables(selected_month)
    finally:
        st.session_state['cash_account_id'] = previous_account
def savings_transfer_deltas(original, replacement):
    deltas = {}
    for row, multiplier in ((original, -1), (replacement, 1)):
        if not row or row.get('type') != 'Savings Transfer':
            continue
        if row.get('transfer_direction') not in ('To savings', 'From savings'):
            raise ValueError('Choose a valid transfer direction.')
        bucket = int(row['savings_bucket_id'])
        effect = money(row['amount']) * (1 if row['transfer_direction'] == 'To savings' else -1)
        deltas[bucket] = deltas.get(bucket, Decimal(0)) + multiplier * effect
    return deltas


def validate_savings_transfer(original, replacement, buckets):
    by_id = {b['id']: b for b in buckets}
    for bucket_id, delta in savings_transfer_deltas(original, replacement).items():
        b = by_id.get(bucket_id)
        if b is None or b['balance'] is None:
            raise ValueError('Establish this bucket balance before transferring money.')
        after = money(b['balance']) + delta
        if after < 0:
            raise ValueError('Insufficient savings in ' + b['name'] + '. Reload if its balance recently changed.')
        if after >= Decimal('1000000000'):
            raise ValueError('Savings balance exceeds the supported limit.')
        if not b['active'] and after != 0:
            raise ValueError('Reactivate ' + b['name'] + ' before reversing its history.')
    # The database repeats these checks against locked, current balances.


def savings_transfer_payload(direction, bucket_id, amount, description, tx_date, tx_time, bucket_name):
    if direction not in ('To savings', 'From savings') or bucket_id is None or amount is None:
        raise ValueError('Choose transfer direction, savings bucket and amount.')
    value = money(amount)
    if value < 0 or value >= Decimal('1000000000'):
        raise ValueError('Enter a nonnegative transfer amount below one billion.')
    return dict(type='Savings Transfer', category='Savings Transfer', transfer_direction=direction,
                savings_bucket_id=int(bucket_id), direction='Expense' if direction == 'To savings' else 'Income',
                amount=str(value), description=str(description or ''), date=str(tx_date),
                time=build_time_string(tx_date, tx_time), merchant='Savings Transfer — ' + bucket_name)


def savings_transfer_form(prefix, original=None):
    try:
        buckets = load_budget_table('LedgerSavingsBuckets')
    except Exception:
        st.error('Savings buckets could not be loaded. Install savings_update.sql, then set up Savings Account.')
        return
    available = {r['id']: r for r in buckets if (r['active'] and r['balance'] is not None)
                 or (original and r['id'] == original.get('savings_bucket_id'))}
    if not available:
        st.info('First establish General savings or a named bucket balance on the Savings Account page. Enter zero explicitly if it is empty.')
        return
    bucket_key = prefix + '_savings_bucket'
    if st.session_state.get(bucket_key) not in available:
        st.session_state[bucket_key] = next(iter(available))
    transfer_direction = st.selectbox('Transfer direction', ['To savings', 'From savings'], key=prefix + '_transfer_direction')
    bucket_id = st.selectbox('Savings category', list(available), key=bucket_key,
                            format_func=lambda i: available[i]['name'] + (' (archived)' if not available[i]['active'] else ''))
    st.caption('Category: Savings Transfer (fixed). To savings leaves checking; From savings returns money to checking. '
               'Transfers update the chosen bucket and savings total once. Bucket balances may not become negative.')
    amount = st.number_input('Amount ($)', value=None if original is None else float(money(original['amount'])),
                             min_value=0.0, max_value=999999999.99, format='%.2f', key=prefix + '_amount')
    description = st.text_input('Description', key=prefix + '_desc')
    tx_date = st.date_input('Date', key=prefix + '_date')
    tx_time = st.time_input('Time', key=prefix + '_time')
    save = st.button('Update Transaction' if original else 'Save Transaction', type='primary', key=prefix + '_save_transfer')
    delete = st.button('Delete Transaction', key=prefix + '_delete_transfer') if original else False
    if save or delete:
        require_session()
        try:
            if delete:
                validate_savings_transfer(original, None, buckets)
                response = (conn.table('Transactions').delete().eq('id', original['id'])
                            .eq('check_revision', original['check_revision']).execute())
            else:
                payload = savings_transfer_payload(transfer_direction, bucket_id, amount, description, tx_date, tx_time, available[bucket_id]['name'])
                validate_savings_transfer(original, payload, buckets)
                if original:
                    response = (conn.table('Transactions').update(payload).eq('id', original['id'])
                                .eq('check_revision', original['check_revision']).execute())
                else:
                    response = conn.table('Transactions').insert(payload).execute()
            if not response.data or getattr(response, 'error', None):
                raise ValueError('The transaction changed. Close and reopen it before saving.')
            if original:
                invalidate_edited_transaction(original)
            else:
                clear_transaction_caches()
            st.rerun()
        except Exception as exc:
            st.error('Transfer was not confirmed: ' + str(getattr(exc, 'message', None) or exc))
            st.caption('An edit or deletion must also leave the original savings bucket nonnegative. Reload after an uncertain response before retrying.')


def savings_totals(buckets):
    known = sum((money(b['balance']) for b in buckets if b['balance'] is not None), Decimal(0))
    unset = [b['name'] for b in buckets if b['active'] and b['balance'] is None]
    return known, unset


def savings_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise RuntimeError('Save not confirmed.')
        st.session_state.pop('savings_snapshot', None)
        st.session_state['savings_generation'] = st.session_state.get('savings_generation', 0) + 1
        clear_transaction_caches()
        st.rerun()
    except Exception as exc:
        st.error('Savings change was not confirmed: ' + str(getattr(exc, 'message', None) or exc))
        st.caption('Reload savings to check for another edit or an uncertain response before trying again.')


def render_savings_page():
    require_session()
    st.title(savings_accounts()[active_savings_id()]['name'])
    st.caption('General savings is unearmarked money; named categories are portions of the same account. '
               'Balances include all recorded savings transfers, including future-dated entries. This is a ledger, not live bank synchronization.')
    if st.button('Reload savings / discard unsaved changes'):
        st.session_state.pop('savings_snapshot', None)
        st.session_state['savings_generation'] = st.session_state.get('savings_generation', 0) + 1
        st.rerun()
    try:
        if 'savings_snapshot' not in st.session_state:
            st.session_state['savings_snapshot'] = [b for b in load_budget_table('LedgerSavingsBuckets') if int(b['savings_account_id']) == active_savings_id()]
        buckets = st.session_state['savings_snapshot']
        audit = [r for r in load_budget_table('LedgerSavingsAudit') if r['bucket_id'] in {b['id'] for b in st.session_state['savings_snapshot']}]
    except Exception:
        st.error('Savings could not be loaded. Install savings_update.sql and reload.')
        return
    by_id = {b['id']: b for b in buckets}
    total, unset = savings_totals(buckets)
    total_column, general_column = st.columns(2)
    total_column.metric('Total savings account balance' if not unset else 'Established balances subtotal', f'${total:,.2f}')
    general = next((b for b in buckets if b['is_general']), None)
    general_column.metric('General savings', 'Not set' if general is None or general['balance'] is None else f"${money(general['balance']):,.2f}")
    if unset:
        st.warning('Total savings is not yet established. Set balances for: ' + ', '.join(unset))
    st.caption('Click a category name to open its transactions and balance history.')
    event = ledger_interaction(data={'html': savings_table_html(buckets)}, key='savings_categories_' + str(active_savings_id()), on_action_change=lambda: None)
    details_id = st.session_state.pop('savings_details_id', None)
    if event.action and isinstance(event.action, dict):
        candidate = str(event.action.get('savings', ''))
        details_id = next((identity for identity in by_id if str(identity) == candidate), None)
    if details_id in by_id:
        savings_category_dialog(by_id[details_id], audit)
    generation = str(st.session_state.get('savings_generation', 0))
    with st.expander('Create or manage savings categories'):
        with st.form('savings_create_' + generation):
            name = st.text_input('New category name')
            create = st.form_submit_button('Create category')
        if create:
            savings_action('ledger_save_savings_bucket',dict(p_id=None,p_revision=None,p_name=name,p_active=True,p_account=active_savings_id()))
        selected = st.selectbox('Category to manage',list(by_id),format_func=lambda i:by_id[i]['name'],key='manage_savings_bucket')
        bucket = by_id[selected]
        with st.form('savings_manage_' + str(selected) + '_' + generation):
            name = st.text_input('Category name',value=bucket['name'],disabled=bucket['is_general'])
            active = st.checkbox('Active',value=bucket['active'],disabled=bucket['is_general'])
            manage = st.form_submit_button('Save category')
        st.caption('Archived categories retain their history. Move their balance to another bucket before archiving. General savings always remains active.')
        if manage:
            savings_action('ledger_save_savings_bucket',dict(p_id=selected,p_revision=bucket['revision'],p_name=name,p_active=active,p_account=active_savings_id()))
    with st.expander('Establish or correct a saved balance'):
        st.caption('Use this for an opening amount or a documented correction. It changes the savings total but never checking. '
                   'Do not use it to record a new checking transfer or to move money between categories. '
                   'When setting up existing savings, enter named portions separately and only the unearmarked remainder in General savings.')
        active_ids = [b['id'] for b in buckets if b['active']]
        selected = st.selectbox('Bucket balance to set',active_ids,format_func=lambda i:by_id[i]['name'],key='set_savings_bucket')
        bucket = by_id[selected]
        with st.form('savings_set_' + str(selected) + '_' + generation):
            balance = st.number_input('Saved amount for this bucket',value=None if bucket['balance'] is None else float(money(bucket['balance'])),
                                      min_value=0.0,max_value=999999999.99,format='%.2f')
            reason = st.text_input('Reason / bank balance reference')
            confirm = st.checkbox('This is an opening balance or correction, not a new checking transfer or internal allocation.')
            save_balance = st.form_submit_button('Record balance adjustment')
        if save_balance:
            if not confirm or balance is None or not reason.strip():
                st.error('Enter the balance and reason, and confirm the adjustment.')
            else:
                savings_action('ledger_set_savings_balance',dict(p_id=selected,p_revision=bucket['revision'],p_balance=str(money(balance)),p_note=reason))
    with st.expander('Allocate money between savings categories'):
        st.caption('Move existing savings between General savings and named categories, or between two named categories. Checking and total savings stay unchanged.')
        initialized = [b['id'] for b in buckets if b['active'] and b['balance'] is not None]
        if len(initialized)<2:
            st.info('Establish balances for at least two active buckets, including an explicit zero for an empty destination.')
        else:
            with st.form('savings_allocate_' + generation):
                source = st.selectbox('From bucket',initialized,format_func=lambda i:by_id[i]['name'])
                target = st.selectbox('To bucket',initialized,index=1,format_func=lambda i:by_id[i]['name'])
                amount = st.number_input('Amount to allocate',value=None,min_value=0.01,max_value=999999999.99,format='%.2f')
                reason = st.text_input('Allocation reason')
                allocate = st.form_submit_button('Move within savings')
            if allocate:
                if source==target or amount is None or not reason.strip():
                    st.error('Choose two different buckets, an amount and a reason.')
                else:
                    savings_action('ledger_allocate_savings',dict(p_from=source,p_to=target,p_from_revision=by_id[source]['revision'],
                        p_to_revision=by_id[target]['revision'],p_amount=str(money(amount)),p_note=reason))
    st.subheader('Savings history')
    if audit:
        st.dataframe(pd.DataFrame([{'Recorded':r['recorded_at'],'Category':by_id.get(r['bucket_id'],{}).get('name',str(r['bucket_id'])),
            'Action':r['kind'],'Change':r['delta'],'Before':r['before_balance'],'After':r['after_balance'],
            'Transaction':r['transaction_id'],'Reason':r['note'],'Operation':r['operation_id'],'Details':json.dumps(r['details'],ensure_ascii=False)}
            for r in reversed(audit)]),hide_index=True,use_container_width=True)
    else:
        st.info('No savings history recorded yet.')


# ============================================================
# LINES OF CREDIT — monthly worksheet, separate from checking
# ============================================================

CREDIT_FIELDS = {
    'credit_limit': 'Credit limit', 'statement_balance': 'Statement balance',
    'apr': 'APR %', 'monthly_interest': 'Monthly interest',
    'minimum_payment': 'Pay at least',
    'charge_1': 'New charge 1', 'charge_2': 'New charge 2', 'charge_3': 'New charge 3',
    'payment_1': 'Payment 1', 'payment_2': 'Payment 2',
    'planned_payment': 'Planned next payment',
}
CREDIT_CALCULATED = ['Month activity', 'Current balance', 'Current usage %',
                     'Future balance', 'Remaining credit', 'Target use', 'Payment to target use']


def credit_optional(value):
    if value is None or pd.isna(value):
        return None
    return money(value)


def credit_calculations(row, target_percent):
    """Posted payments count once. APR estimate uses the current balance before a future payment."""
    values = {key: credit_optional(row.get(key)) for key in CREDIT_FIELDS}
    charges = sum((values[k] or Decimal(0) for k in ('charge_1', 'charge_2', 'charge_3')), Decimal(0))
    payments = sum((abs(values[k] or Decimal(0)) for k in ('payment_1', 'payment_2')), Decimal(0))
    activity = charges - payments
    opening, limit, apr = values['statement_balance'], values['credit_limit'], values['apr']
    balance = None if opening is None else opening + activity
    usage = None if balance is None or not limit else (balance / limit * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    target = None if limit is None else (limit * money(target_percent) / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    interest = None if balance is None or apr is None else (max(balance, Decimal(0)) * apr / 1200).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    future = None if interest is None else balance - abs(values['planned_payment'] or Decimal(0)) + interest
    return {'Month activity': activity, 'Current balance': balance, 'Current usage %': usage,
            'Future balance': future, 'Remaining credit': None if balance is None or limit is None else limit - balance,
            'Target use': target, 'Payment to target use': None if balance is None or target is None else balance - target}


def credit_frame(rows, accounts, target):
    names = {a['id']: a for a in accounts}
    result = []
    for row in rows:
        account = names[row['account_id']]
        entry = {'_id': row['id'], 'Account': account['name'], 'Type': account['account_type']}
        entry.update({label: None if row.get(key) is None else float(money(row[key])) for key, label in CREDIT_FIELDS.items()})
        entry.update({key: None if value is None else float(value) for key, value in credit_calculations(row, target).items()})
        result.append(entry)
    order = ['_id', 'Account', 'Type', 'Credit limit', 'Statement balance', 'APR %', 'Monthly interest', 'Pay at least',
             'New charge 1', 'New charge 2', 'New charge 3', 'Payment 1', 'Payment 2', 'Month activity',
             'Current balance', 'Current usage %', 'Future balance', 'Remaining credit', 'Target use',
             'Payment to target use', 'Planned next payment']
    return pd.DataFrame(result, columns=order).sort_values(
        'Account', key=lambda values: values.str.casefold(), kind='stable'
    ).reset_index(drop=True)


def credit_edits(edited, originals):
    """Return only changed rows with the revision shown to this browser."""
    by_id = {str(r['id']): r for r in originals}
    result = []
    seen = set()
    for _, row in edited.iterrows():
        identity = str(row['_id'])
        if identity not in by_id or identity in seen:
            raise ValueError('The worksheet changed. Reload it before saving.')
        seen.add(identity)
        previous = by_id[identity]
        values = {}
        changed = False
        for key, label in CREDIT_FIELDS.items():
            amount = credit_optional(row[label])
            if amount is not None and key not in ('statement_balance', 'charge_1', 'charge_2', 'charge_3', 'payment_1', 'payment_2') and amount < 0:
                raise ValueError(label + ' cannot be negative.')
            if key == 'apr' and amount is not None and amount > 100:
                raise ValueError('APR must be between 0 and 100 percent.')
            if key in ('payment_1', 'payment_2') and amount is not None:
                amount = -abs(amount)
            old = credit_optional(previous.get(key))
            values[key] = None if amount is None else str(amount)
            changed = changed or amount != old
        if changed:
            result.append(dict(id=previous['id'], revision=previous['revision'], **values))
    if seen != set(by_id):
        raise ValueError('Rows cannot be removed from this worksheet. Reload it.')
    return result


def credit_action(name, payload):
    require_session()
    try:
        result = conn.rpc(name, payload).execute()
        if result.data is not True:
            raise RuntimeError('Save not confirmed.')
    except Exception as exc:
        st.error('Credit change was not confirmed: ' + str(getattr(exc, 'message', None) or exc))
        st.caption('Reload to check the saved values before retrying an uncertain response.')
        return
    st.session_state.pop('credit_snapshot', None)
    st.session_state['credit_generation'] = st.session_state.get('credit_generation', 0) + 1
    st.rerun()


def credit_style(frame):
    def cell_styles(row):
        styles = []
        for column in frame.columns:
            bg = '#ffffff'
            if column == 'Current usage %' and not pd.isna(row[column]):
                if row[column] > row.get('_target_percent', 29):
                    bg = '#ffd6d6'
            styles.append('background-color:' + bg + ';color:#17202a')
        return styles
    return frame.style.apply(cell_styles, axis=1)


def credit_history_rows(history, accounts):
    names = {str(a['id']): a['name'] for a in accounts}
    labels = dict(CREDIT_FIELDS, name='Account name', account_type='Account type', active='Include in new months', target_percent='Target use %')
    result = []
    for record in reversed(history):
        before, after = record.get('before_data') or {}, record.get('after_data') or {}
        stamp = datetime.fromisoformat(record['recorded_at'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
        for key, label in labels.items():
            if before.get(key) == after.get(key):
                continue
            def display(value):
                if value is None:
                    return 'Not set'
                if isinstance(value, bool):
                    return 'Yes' if value else 'No'
                if key in CREDIT_FIELDS or key == 'target_percent':
                    return f'{money(value):,.2f}%' if key in ('apr', 'target_percent') else f'${money(value):,.2f}'
                return str(value)
            result.append({'Recorded': stamp.strftime('%Y-%m-%d %H:%M'), 'Account': names.get(str(record.get('account_id')), 'All accounts'),
                           'Action': record['kind'], 'Field': label, 'Before': display(before.get(key)), 'After': display(after.get(key))})
        if record['kind'] == 'Month prepared':
            result.append({'Recorded': stamp.strftime('%Y-%m-%d %H:%M'), 'Account': names.get(str(record.get('account_id')), 'All accounts'),
                           'Action': 'Month prepared', 'Field': 'Worksheet month', 'Before': '', 'After': record['month']})
    return result


def render_credit_page():
    require_session()
    st.title('Lines of Credit')
    st.caption('Enter charges and payments in the monthly worksheet. Saved credit entries track these balances separately from checking.')
    controls = st.columns([1, 1, 1])
    with controls[0]:
        chosen = st.date_input('Worksheet month', value=date(2026, 9, 1), key='credit_month_picker')
    month = chosen.replace(day=1).isoformat()
    # Match the space occupied by the date input's label above adjacent controls.
    for control in controls[1:]:
        control.markdown('<div aria-hidden="true" style="height:28px"></div>', unsafe_allow_html=True)
    if controls[2].button('Reload / discard unsaved changes'):
        st.session_state.pop('credit_snapshot', None)
        st.session_state['credit_generation'] = st.session_state.get('credit_generation', 0) + 1
        st.rerun()
    try:
        snapshot = st.session_state.get('credit_snapshot')
        if not snapshot or snapshot['month'] != month:
            accounts = load_budget_table('LedgerCreditAccounts')
            months = load_budget_table('LedgerCreditMonths')
            settings = load_budget_table('LedgerCreditSettings')
            if len(settings) != 1:
                raise RuntimeError('Credit settings are missing.')
            snapshot = dict(month=month, accounts=accounts, all_months=months, settings=settings[0])
            st.session_state['credit_snapshot'] = snapshot
        accounts, all_months, setting = snapshot['accounts'], snapshot['all_months'], snapshot['settings']
    except Exception:
        st.error('Lines of Credit could not be loaded. Install lines_of_credit_update.sql, then reload.')
        return
    generation = str(st.session_state.get('credit_generation', 0))
    rows = [r for r in all_months if r['month'] == month]
    target = money(setting['target_percent'])
    with controls[1]:
        with st.expander('Target use settings'):
            with st.form('credit_target_' + generation):
                percent = st.number_input('Target credit use (%)', min_value=0.0, max_value=100.0, value=float(target), step=1.0, format='%.2f')
                save_target = st.form_submit_button('Save target percentage')
    if save_target:
        credit_action('ledger_save_credit_target', dict(p_revision=setting['revision'], p_target=str(money(percent))))
    with st.expander('Add or manage a credit account', expanded=not accounts):
        with st.form('credit_add_' + generation):
            name = st.text_input('Account name')
            kind = st.selectbox('Account type', ['Credit Card', 'Line of Credit', 'Lender', 'Loan'])
            add = st.form_submit_button('Add account to this month')
        if add:
            if not name.strip():
                st.error('Enter an account name.')
            else:
                credit_action('ledger_save_credit_account', dict(p_id=None, p_revision=None, p_name=name.strip(), p_type=kind, p_active=True, p_month=month))
        if accounts:
            by_id = {a['id']: a for a in accounts}
            selected = st.selectbox('Account to manage', list(by_id), format_func=lambda i: by_id[i]['name'])
            account = by_id[selected]
            with st.form('credit_manage_' + str(selected) + '_' + generation):
                name = st.text_input('Name', value=account['name'])
                kinds = ['Credit Card', 'Line of Credit', 'Lender', 'Loan']
                kind = st.selectbox('Type', kinds, index=kinds.index(account['account_type']))
                active = st.checkbox('Include in new months', value=account['active'])
                manage = st.form_submit_button('Save account details')
            st.caption('Turning off inclusion keeps past worksheets and history. Existing month entries remain editable.')
            if manage:
                credit_action('ledger_save_credit_account', dict(p_id=selected, p_revision=account['revision'], p_name=name, p_type=kind, p_active=active, p_month=month))
    missing = [a for a in accounts if a['active'] and a['id'] not in {r['account_id'] for r in rows}]
    if missing:
        st.info('Prepare this month to add: ' + ', '.join(a['name'] for a in missing))
        st.caption('The immediately preceding month’s saved current balance becomes the statement balance. Limits and APR carry forward; charges, payments and planned payments start blank. Verify against each statement. Earlier worksheets stay unchanged.')
        if st.button('Prepare this month from the previous month', type='primary'):
            credit_action('ledger_prepare_credit_month', dict(p_month=month))
    if not rows:
        st.info('Add an account or prepare this month, then enter its limit, statement balance and APR. Blank means not entered; enter zero explicitly for a known zero.')
        return
    frame = credit_frame(rows, accounts, target)
    calculations = [credit_calculations(row, target) for row in rows]
    known_balance = sum((r['Current balance'] for r in calculations if r['Current balance'] is not None), Decimal(0))
    known_limit = sum((money(r['credit_limit']) for r in rows if r.get('credit_limit') is not None), Decimal(0))
    unknown = sum(r['Current balance'] is None for r in calculations)
    c1, c2, c3 = st.columns(3)
    c1.metric('Current balances' if not unknown else 'Established balance subtotal', f'${known_balance:,.2f}')
    c2.metric('Entered credit limits', f'${known_limit:,.2f}')
    c3.metric('Target use', f'{target:,.2f}%')
    if unknown:
        st.warning(f'{unknown} account(s) need a statement balance before their balance and usage can be calculated.')
    st.caption('Click an input cell to edit. Payments may be entered with either sign and are saved as negative amounts. Negative charges record credits/refunds. Save the worksheet before changing months, target percentage or account settings. Calculations refresh after saving; scroll horizontally for the remaining columns.')
    config = {'_id': None, 'Account': st.column_config.TextColumn(width='medium'),
              'Type': st.column_config.TextColumn(width='small')}
    for label in list(CREDIT_FIELDS.values()) + CREDIT_CALCULATED:
        header = ('🟨 ' if label.startswith('New charge ') else
                  '🟩 ' if label in ('Payment 1', 'Payment 2') else '') + label
        config[label] = st.column_config.NumberColumn(header, format='%.2f' if '%' in label else '$%.2f', width='small')
    config['APR %'] = st.column_config.NumberColumn('APR %', min_value=0.0, max_value=100.0, format='%.2f')
    config['Planned next payment'] = st.column_config.NumberColumn('Planned next payment', min_value=0.0, format='$%.2f', help='An additional future payment; posted payments are already included in Current balance.')
    # Styler formatting is supported for disabled/calculated columns. Editable cells use standard inputs.
    frame['_target_percent'] = float(target)
    config['_target_percent'] = None
    edited = st.data_editor(credit_style(frame), hide_index=True, num_rows='fixed', use_container_width=True,
                            height=min(1000, 40 + len(rows) * 36),
                            disabled=['_id', '_target_percent', 'Account', 'Type'] + CREDIT_CALCULATED,
                            column_config=config, key='credit_sheet_' + month + '_' + generation)
    if st.button('Save credit worksheet', type='primary'):
        try:
            changes = credit_edits(edited, rows)
        except (ValueError, InvalidOperation) as exc:
            st.error(str(exc))
        else:
            if changes:
                credit_action('ledger_save_credit_months', dict(p_month=month, p_rows=changes))
            else:
                st.info('No worksheet changes to save.')
    totals = {}
    for column in ['Month activity', 'Current balance', 'Future balance', 'Remaining credit', 'Target use', 'Payment to target use']:
        values = [r[column] for r in calculations]
        totals[column] = None if any(v is None for v in values) else float(sum(values, Decimal(0)))
    totals.update({'Account': 'Month totals', 'Credit limit': None if any(r.get('credit_limit') is None for r in rows) else float(known_limit)})
    st.dataframe(pd.DataFrame([totals]), hide_index=True, use_container_width=True,
                 column_config={k: v for k, v in config.items() if k in totals})
    st.caption('Current balance = statement balance + new charges − recorded payments. Monthly interest and Pay at least are statement-reference fields; they are not added to the balance again. Enter a new interest charge in a New charge cell only if it is not included in the statement balance.')
    st.caption('Future balance is an estimate: current balance − planned next payment + one month of interest (positive current balance × APR ÷ 12). It assumes no additional charges and uses the balance before the planned payment for interest. Blank APR leaves the estimate blank. Actual interest may differ because of daily balances, payment dates, fees, promotional rates and grace periods.')
    st.caption('Target use = limit × target percentage. Payment to target use = current balance − target use: positive is the payment needed; negative means already below target. Preparing another month copies saved current balances, not this estimate; later corrections do not rewrite other months.')
    with st.expander('Saved change history'):
        try:
            history = load_budget_table('LedgerCreditAudit')
            selected_history = [h for h in history if h.get('month') == month or h.get('month') is None]
            if selected_history:
                st.dataframe(pd.DataFrame(credit_history_rows(selected_history, accounts)),
                    hide_index=True, use_container_width=True)
            else:
                st.info('No saved changes yet.')
        except Exception:
            st.error('Credit history could not be loaded.')


def savings_category_transactions(bucket_id, transactions):
    rows = []
    for tx in transactions:
        if tx.get('type') != 'Savings Transfer' or str(tx.get('savings_bucket_id')) != str(bucket_id):
            continue
        amount = money(tx['amount']) * (1 if tx.get('transfer_direction') == 'To savings' else -1)
        rows.append({'Date': tx['date'], 'Amount': float(amount), 'Direction': tx['transfer_direction'],
                     'Description': tx.get('description') or '', 'Transaction': tx['id']})
    return sorted(rows, key=lambda r: (r['Date'], str(r['Transaction'])), reverse=True)


@st.dialog('Savings category transactions', width='large')
def savings_category_dialog(bucket, audit):
    require_session()
    st.subheader(bucket['name'])
    st.metric('Current saved amount', 'Not set' if bucket['balance'] is None else f"${money(bucket['balance']):,.2f}")
    tabs = st.tabs(['Transactions', 'Balance history'])
    with tabs[0]:
        try:
            rows = savings_category_transactions(bucket['id'], get_all_transactions_cached()) + savings_linked_rows(bucket['id'])
        except Exception:
            st.error('Transactions could not be loaded. Close this window and reload savings.')
        else:
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                    column_config={'Amount': st.column_config.NumberColumn(format='$%.2f'), 'Transaction': None})
            else:
                st.info('No checking transfers are recorded for this category.')
        st.caption('Positive amounts add to savings; negative amounts withdraw. Opening balances, corrections, allocations and deleted entries are retained in Balance history.')
    with tabs[1]:
        history = [r for r in audit if str(r['bucket_id']) == str(bucket['id'])]
        if history:
            entries = []
            for row in reversed(history):
                stamp = datetime.fromisoformat(row['recorded_at'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                entries.append({'Recorded': stamp.strftime('%Y-%m-%d %H:%M'), 'Action': row['kind'],
                    'Amount': float(money(row['delta'])), 'Balance after': None if row['after_balance'] is None else float(money(row['after_balance'])),
                    'Description': row['note']})
            st.dataframe(pd.DataFrame(entries), hide_index=True, use_container_width=True,
                column_config={'Amount': st.column_config.NumberColumn(format='$%.2f'), 'Balance after': st.column_config.NumberColumn(format='$%.2f')})
        else:
            st.info('No balance history recorded yet.')
    if st.button('Close category details'):
        st.rerun()


def savings_table_html(buckets):
    parts = ['<style>.savings-category-table{width:100%;border-collapse:collapse;font-size:14px}'
             '.savings-category-table th,.savings-category-table td{border:1px solid #bbb;padding:10px;text-align:left}'
             '.savings-category-table th{background:#edf1f6;color:#17202a}'
             '.savings-category-table button{font:inherit;color:inherit;border:0;background:transparent;padding:0;width:100%;text-align:left;cursor:pointer;text-decoration:underline}'
             '.savings-category-table button:focus-visible{outline:2px solid #2684ff;outline-offset:3px}</style>'
             '<table class="savings-category-table"><thead><tr><th>Category</th><th>Saved amount</th><th>Status</th></tr></thead><tbody>']
    for bucket in buckets:
        amount = 'Not set' if bucket['balance'] is None else f"${money(bucket['balance']):,.2f}"
        parts.append(f'<tr><td><button type="button" data-savings="{int(bucket["id"])}">{escape(bucket["name"])}</button></td>'
                     f'<td>{amount}</td><td>{"Active" if bucket["active"] else "Archived"}</td></tr>')
    parts.append('</tbody></table>')
    return ''.join(parts)


def active_cash_id():
    return int(st.session_state.get('cash_account_id', 1))


def cash_accounts():
    return {int(r['id']): r for r in load_budget_table('LedgerCashAccounts')}


def savings_accounts():
    return {int(r['id']): r for r in load_budget_table('LedgerSavingsAccounts')}


def active_savings_id():
    return int(st.session_state.get('savings_account_id', 1))


def account_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise ValueError('Save was not confirmed.')
        clear_transaction_caches()
        for key in list(st.session_state):
            if key.startswith(('budget_grid_snapshot_', 'payday_snapshot_')):
                st.session_state.pop(key, None)
        st.rerun()
    except Exception as exc:
        st.error('Nothing confirmed: ' + str(getattr(exc, 'message', None) or exc))
        st.caption('Reload and check saved records before retrying an uncertain response.')


def savings_linked_rows(bucket_id):
    rows=[]
    for t in load_budget_table('LedgerTransfers'):
        sign = 1 if t.get('destination_bucket') == bucket_id else -1 if t.get('source_bucket') == bucket_id else 0
        if sign:
            rows.append({'Date':t['date'],'Amount':float(money(t['amount'])*sign),
                'Direction':'To savings' if sign>0 else 'From savings',
                'Description':t['description'],'Transaction':'Transfer '+str(t['id'])})
    return rows


def transfer_form(original=None):
    accounts = cash_accounts()
    savings = savings_accounts()
    buckets = load_budget_table('LedgerSavingsBuckets')
    endpoints = {'c:' + str(i): row['name'] for i, row in accounts.items()}
    endpoints.update({'s:' + str(b['id']): savings[int(b['savings_account_id'])]['name'] + ' / ' + b['name']
        for b in buckets if b['active'] and b['balance'] is not None})
    options = list(endpoints)
    if len(options) < 2:
        st.error('Set up two available accounts before recording a transfer.')
        return
    def endpoint(row, side):
        if row.get(side + '_account') is not None:
            return 'c:' + str(row[side + '_account'])
        return 's:' + str(row[side + '_bucket'])
    if original:
        source = endpoint(original, 'source')
        destination = endpoint(original, 'destination')
    elif st.session_state.get('ledger_account') in {r['name'] for r in savings.values()}:
        source = next(('s:' + str(b['id']) for b in buckets
            if int(b['savings_account_id']) == active_savings_id() and b['is_general']), options[0])
        destination = next((key for key in options if key != source), options[0])
    else:
        source = 'c:' + str(active_cash_id())
        destination = next((key for key in options if key != source), options[0])
    if source not in options or destination not in options:
        st.error('A transfer account is unavailable. Restore its balance and active status before editing.')
        return
    with st.form('transfer_' + str(original['id'] if original else 'new')):
        src = st.selectbox('From account', options, index=options.index(source), format_func=endpoints.get)
        dst = st.selectbox('To account', options, index=options.index(destination), format_func=endpoints.get)
        amount = st.number_input('Transfer amount', min_value=0.01, max_value=999999999.99,
            value=float(money(original['amount'])) if original else None, format='%.2f')
        tx_date = st.date_input('Transfer date',
            value=date.fromisoformat(original['date']) if original else datetime.now(LOCAL_TZ).date())
        description = st.text_input('Description', value=original['description'] if original else '')
        save = st.form_submit_button('Save linked transfer', type='primary')
        delete = st.form_submit_button('Delete linked transfer') if original else False
    if save or delete:
        if not delete and (src == dst or amount is None):
            st.error('Choose different accounts and enter an amount.')
            return
        payload = dict(source_account=int(src[2:]) if src.startswith('c:') else None,
            source_bucket=int(src[2:]) if src.startswith('s:') else None,
            destination_account=int(dst[2:]) if dst.startswith('c:') else None,
            destination_bucket=int(dst[2:]) if dst.startswith('s:') else None,
            amount=str(money(amount or 0)),date=tx_date.isoformat(),description=description,
            schedule_id=None,occurrence_date=None)
        account_action('ledger_save_transfer', dict(p_id=original['id'] if original else None,
            p_revision=original['revision'] if original else None,p_data=payload,p_delete=delete))


def loan_balance(loan, through):
    principal=money(loan['principal']); interest=Decimal(str(loan['accrued_interest']))
    annual=Decimal(str(loan['rate']))/100; basis=Decimal(str(loan.get('day_basis',365.25)))
    day=date.fromisoformat(loan['as_of'])
    if through<day: raise ValueError('Projection date precedes the statement baseline.')
    events=sorted(enumerate(loan.get('events',[])),key=lambda pair:(pair[1]['date'],pair[0]))
    for _,event in events:
        event_day=date.fromisoformat(event['date'])
        if event_day<day: raise ValueError('Event precedes the baseline.')
        if event_day>through: break
        interest+=principal*annual*Decimal((event_day-day).days)/basis; day=event_day
        amount=money(event['amount'])
        if amount<0: raise ValueError('Event amounts must be nonnegative.')
        if event['kind']=='Payment':
            if amount>money(principal+interest): raise ValueError('Payment exceeds estimated total owed on its date.')
            applied=min(interest,amount); interest-=applied
            principal=max(Decimal(0),principal-(amount-applied))
        elif event['kind']=='Capitalization':
            if amount>interest: raise ValueError('Capitalization cannot exceed accrued interest on its date.')
            interest-=amount; principal+=amount
        else: raise ValueError('Unknown loan event.')
    interest+=principal*annual*Decimal((through-day).days)/basis
    return money(principal),money(interest)


def payoff_estimate(principal, accrued, rate, payment, first_date, federal=False, basis=Decimal('365.25')):
    p=money(principal); interest=money(accrued); payment=money(payment); rate=Decimal(str(rate))/100
    if p+interest<=0: return dict(months=0,total=Decimal(0),interest=Decimal(0),status='Paid off')
    if payment<=0: return dict(months=None,status='Enter a positive payment')
    paid=Decimal(0); added=Decimal(0); day=first_date
    for count in range(1,1201):
        y=first_date.year+(first_date.month-1+count)//12; m=(first_date.month-1+count)%12+1
        next_day=date(y,m,min(first_date.day,monthrange(y,m)[1]))
        charge=money(p*rate*Decimal((next_day-day).days)/basis) if federal else money((p+interest)*rate/12)
        if not federal and count==1 and payment<=charge: return dict(months=None,status='Payment does not exceed monthly interest')
        if federal and payment<=p*rate*Decimal(365)/basis/12 and count==1:
            return dict(months=None,status='Payment does not exceed average monthly interest')
        interest+=charge; added+=charge
        actual=min(payment,p+interest); paid+=actual
        toward_interest=min(actual,interest); interest-=toward_interest; p-=actual-toward_interest
        if p+interest<=0: return dict(months=count,total=money(paid),interest=money(added),status='Estimated payoff',date=next_day)
        day=next_day
    return dict(months=None,status='More than 1,200 months at this payment')


def render_debt_planning():
    st.divider(); st.header('Federal student loans')
    loans=load_budget_table('LedgerFederalLoans')
    choices={None:'Add federal consolidation loan',**{r['id']:r['name'] for r in loans}}
    selected=st.selectbox('Student loan',list(choices),format_func=choices.get)
    old=next((r for r in loans if r['id']==selected),None)
    with st.expander('Statement baseline, payments, and capitalization',expanded=old is None):
        st.caption('Baseline balances are after any activity already reflected in the statement. Only enter later payments here. Reconciliation replaces the baseline; retain only events not included in the new statement. Saves preserve the previous baseline and events in history. No checking payment is created.')
        with st.form('federal_loan_'+str(selected)):
            name=st.text_input('Loan name',value=old['name'] if old else 'Federal consolidation loan')
            as_of=st.date_input('Statement balance date',value=date.fromisoformat(old['as_of']) if old else datetime.now(LOCAL_TZ).date())
            principal=st.number_input('Principal balance',min_value=0.0,value=float(old['principal']) if old else None,format='%.2f')
            interest=st.number_input('Unpaid accrued interest',min_value=0.0,value=float(old['accrued_interest']) if old else None,format='%.2f')
            rate=st.number_input('Annual interest rate (%)',min_value=0.0,max_value=100.0,value=float(old['rate']) if old else 8.25,format='%.4f')
            basis=st.selectbox('Servicer day-count basis',[365.25,365.0],index=0 if not old or float(old['day_basis'])==365.25 else 1)
            entries=[{'Date':date.fromisoformat(e['date']),'Kind':e['kind'],'Amount':float(e['amount'])} for e in old.get('events',[])] if old else []
            events=st.data_editor(pd.DataFrame(entries,columns=['Date','Kind','Amount']),num_rows='dynamic',hide_index=True,
                column_config={'Date':st.column_config.DateColumn(required=True),'Kind':st.column_config.SelectboxColumn(options=['Payment','Capitalization'],required=True),'Amount':st.column_config.NumberColumn(min_value=0,required=True,format='$%.2f')})
            save=st.form_submit_button('Save loan baseline and events')
        if save:
            try:
                if principal is None or interest is None: raise ValueError('Enter principal and accrued interest, including explicit zero when applicable.')
                ev=[dict(date=str(e['Date'])[:10],kind=e['Kind'],amount=str(money(e['Amount']))) for e in events.to_dict('records')]
                payload=dict(name=name,principal=str(money(principal)),accrued_interest=str(money(interest)),rate=str(rate),as_of=as_of.isoformat(),day_basis=str(basis),events=ev)
                loan_balance(payload,max([as_of]+[date.fromisoformat(e['date']) for e in ev]))
                account_action('ledger_save_federal_loan',dict(p_id=selected,p_revision=old['revision'] if old else None,p_data=payload))
            except (ValueError,TypeError,InvalidOperation) as exc: st.error(str(exc))
    today=datetime.now(LOCAL_TZ).date()
    if old:
        through=st.date_input('Estimate loan balance through',value=max(today,date.fromisoformat(old['as_of'])))
        try:
            p,i=loan_balance(old,through)
            cols=st.columns(3); cols[0].metric('Principal',f'${p:,.2f}');cols[1].metric('Accrued interest',f'${i:,.2f}');cols[2].metric('Total owed estimate',f'${p+i:,.2f}')
        except ValueError as exc: st.error(str(exc))
        with st.expander('Saved loan history'):
            history=[r for r in load_budget_table('LedgerAccountAudit') if r['kind']=='Federal loan statement and events'
                and (r.get('after_data') or {}).get('id')==old['id']]
            if history:
                st.dataframe(pd.DataFrame([{'Saved':r['recorded_at'],'Statement date':r['after_data']['as_of'],
                    'Principal':float(money(r['after_data']['principal'])),'Unpaid interest':float(money(r['after_data']['accrued_interest'])),
                    'Rate %':float(r['after_data']['rate']),'Events':json.dumps(r['after_data']['events'])} for r in reversed(history)]),hide_index=True)
            else: st.caption('No saved history yet.')
    st.subheader('What-if monthly payoff')
    st.caption('Hypothetical payments do not create transactions. Assumes fixed rates, no new borrowing or fees, and a fixed payment each month. Cards and credit lines use a monthly interest approximation; federal loans use daily simple interest. No forgiveness, subsidy, or automatic capitalization is assumed. Compare estimates with your servicer.')
    credit_accounts={a['id']:a for a in load_budget_table('LedgerCreditAccounts')}
    months=load_budget_table('LedgerCreditMonths')
    month=st.session_state.get('credit_month_picker',date(2026,9,1)).replace(day=1).isoformat()
    start=st.date_input('Payoff estimate starts',value=today)
    for row in [r for r in months if r['month']==month]:
        a=credit_accounts[row['account_id']]; result=credit_calculations(row,29)
        st.write(a['name'])
        payment=st.number_input('What-if monthly payment',min_value=0.0,value=None,format='%.2f',key='whatif_credit_'+str(a['id']))
        if payment is not None:
            if result['Current balance'] is None or row['apr'] is None: st.info('Enter a statement balance and APR first.');continue
            estimate=payoff_estimate(result['Current balance'],0,row['apr'],payment,start)
            st.write(f"{estimate['months']} months" if estimate['months'] is not None else estimate['status'])
    for loan in loans:
        st.write(loan['name']+' — federal student loan')
        payment=st.number_input('What-if monthly payment',min_value=0.0,value=None,format='%.2f',key='whatif_loan_'+str(loan['id']))
        if payment is not None:
            try:
                p,i=loan_balance(loan,start)
                estimate=payoff_estimate(p,i,loan['rate'],payment,start,True,Decimal(str(loan['day_basis'])))
                st.write(f"{estimate['months']} months" if estimate['months'] is not None else estimate['status'])
            except ValueError as exc: st.error(str(exc))


# ============================================================
# SIDEBAR BUTTONS & ACCOUNT CONTROLS
# ============================================================

try:
    checking_accounts = cash_accounts()
    saved_savings_accounts = savings_accounts()
except Exception:
    st.error('Install unified_budget_update.sql after the multi-account update, then reload.')
    st.stop()
account_names = {row['name']: aid for aid, row in checking_accounts.items()}
savings_names = {row['name']: aid for aid, row in saved_savings_accounts.items()}
choices = list(account_names) + list(savings_names)
if st.session_state.get('ledger_account') not in choices:
    st.session_state['ledger_account'] = 'Primary Checking'

def selected_account_changed():
    st.session_state['ledger_view'] = 'Account'
    st.session_state.pop('savings_snapshot', None)

account_selection = st.sidebar.selectbox('Select account', choices, key='ledger_account',
    on_change=selected_account_changed)
if account_selection in account_names:
    st.session_state['cash_account_id'] = account_names[account_selection]
elif account_selection in savings_names:
    st.session_state['savings_account_id'] = savings_names[account_selection]

with st.sidebar.expander('Add new account'):
    with st.form('create_financial_account'):
        account_type = st.selectbox('Account type', ['Checking', 'Savings'])
        name = st.text_input('Account name')
        create = st.form_submit_button('Create account')
    if create:
        if account_type == 'Checking':
            account_action('ledger_save_cash_account', {'p_name': name})
        else:
            account_action('ledger_create_savings_account', {'p_name': name})

if st.sidebar.button('View selected account', use_container_width=True):
    st.session_state['ledger_view'] = 'Account'
st.sidebar.divider()

if st.sidebar.button('➕ Add Transaction', type='primary', use_container_width=True):
    reset_add_transaction_state()
    add_transaction_dialog()
if st.sidebar.button('✏️ Edit Transaction', use_container_width=True):
    st.session_state.pop('edit_loaded_tx_id', None)
    edit_transaction_dialog()
if st.sidebar.button('AMZ Card Ledger', use_container_width=True):
    st.session_state['ledger_view'] = 'Reconcile'

st.sidebar.divider()
if st.sidebar.button('View / Edit Budget', use_container_width=True):
    st.session_state['ledger_view'] = 'Budget'
if st.sidebar.button('Lines of Credit', use_container_width=True):
    if st.session_state.get('ledger_view') != 'Lines of Credit':
        st.session_state['credit_generation'] = st.session_state.get('credit_generation', 0) + 1
    st.session_state['ledger_view'] = 'Lines of Credit'

if st.session_state.get('ledger_view') in ('Budget', 'Reconcile', 'Lines of Credit'):
    account_selection = st.session_state['ledger_view']
st.sidebar.divider()
if st.sidebar.button('Log Out', use_container_width=True):
    try:
        conn.auth.sign_out()
    except Exception:
        st.warning('Server sign-out could not be confirmed. Local session cleared.')
    clear_login_state()
    st.rerun()


# ============================================================
# CHECKING ACCOUNT LAYOUT
# ============================================================

if account_selection in account_names:
    render_editable_calendar()


# ============================================================
# SAVINGS ACCOUNT LAYOUT
# ============================================================

elif account_selection == "Reconcile":
    selected_checking = active_cash_id()
    st.session_state['cash_account_id'] = 1
    try:
        reconcile_card_dialog()
    finally:
        st.session_state['cash_account_id'] = selected_checking

elif account_selection == "Budget":
    render_budget_page()

elif account_selection == "Lines of Credit":
    render_credit_page()
    render_debt_planning()

elif account_selection in savings_names:
    render_savings_page()





