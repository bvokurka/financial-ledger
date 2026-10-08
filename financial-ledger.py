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

    /* Selected account buttons stay green. */
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

    /* Only Add Transaction is blue. */
    [data-testid="stSidebar"] .st-key-sidebar_add_transaction button[kind="primary"] {
        background-color: #1769c2 !important;
        border-color: #1769c2 !important;
    }
    [data-testid="stSidebar"] .st-key-sidebar_add_transaction button[kind="primary"]:hover {
        background-color: #11549c !important;
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
    """Return merchants across accounts, including saved budget merchants."""
    transactions = get_all_transactions_cached()

    merchants = {
        str(row.get("merchant")).strip()
        for row in transactions
        if row.get("merchant")
    }

    try:
        merchants.update(
            str(row['paid_to']).strip()
            for row in load_budget_table('LedgerUnifiedBudgetRules')
            if row.get('transaction_type') != 'Transfer' and row.get('paid_to')
            and str(row['paid_to']).strip().casefold() != 'merchant'
        )
    except Exception:
        logger.exception('Saved budget merchants could not be loaded.')
    return sorted({value.casefold(): value for value in merchants if value}.values(), key=str.lower)


def get_existing_categories() -> list[str]:
    """Return active transaction categories across all checking accounts."""
    transactions = get_all_transactions_cached()
    budget_categories = {str(row.get('category')).strip() for row in
                         load_budget_table('LedgerUnifiedBudgetRules')
                         if row.get('transaction_type') != 'Transfer' and row.get('category')}
    retired = {str(row['name']).strip().casefold() for row in
               load_budget_table('LedgerRetiredCategories')}

    db_categories = {
        str(row.get("category")).strip()
        for row in transactions
        if row.get("category")
    }

    return sorted(
        (category for category in set(BASE_CATEGORIES).union(db_categories, budget_categories)
         if category.casefold() not in retired),
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
    const ownerWindow = input.ownerDocument.defaultView;
    // Streamlit can replace the Amount input without rerendering this component.
    // Resolve neighbors at keypress time; capture before the dialog focus trap.
    function neighbors() {
        // The component container may be a fragment; the input is a DOM Element.
        const dialog = input.closest('[role="dialog"]');
        if (!dialog) return {};
        return {
            amount: [...dialog.querySelectorAll('input[type="number"]')]
                .filter(candidate => candidate.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING)
                .at(-1),
            category: [...dialog.querySelectorAll('[data-testid="stSelectbox"] input')]
                .find(candidate => input.compareDocumentPosition(candidate) & Node.DOCUMENT_POSITION_FOLLOWING),
        };
    }
    if (input.ledgerMerchantTabCleanup) input.ledgerMerchantTabCleanup();
    const handleTab = event => {
        if (!input.isConnected) {
            ownerWindow.removeEventListener('keydown', handleTab, true);
            return;
        }
        if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey ||
                !input.getClientRects().length || composing || event.isComposing) return;
        const {amount, category} = neighbors();
        let target = null;
        if (event.target === amount && !event.shiftKey) target = input;
        else if (event.target === category && event.shiftKey) target = input;
        else if (event.target === input) {
            commit();
            target = event.shiftKey ? amount : category;
        }
        if (!target) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        target.focus();
    };
    ownerWindow.addEventListener('keydown', handleTab, true);
    input.ledgerMerchantTabCleanup = () => ownerWindow.removeEventListener('keydown', handleTab, true);
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
            const amountInput = neighbors().amount;
            if (event.shiftKey && amountInput) {
                event.preventDefault();
                amountInput.focus();
            }
            // Forward Tab follows the browser's normal focus navigation.
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
def get_merchant_component(html: str, css: str, js: str):
    """Cache by source content so app updates cannot retain an older component."""
    from hashlib import sha256
    from inspect import signature
    try:
        from streamlit.components.v2 import component
    except ImportError:
        st.error("Merchant autocomplete requires Streamlit 1.51 or newer.")
        st.stop()
    source = f"inline-v2:{len(html)}:{html}{len(css)}:{css}{len(js)}:{js}"
    version = sha256(source.encode("utf-8")).hexdigest()[:16]
    registration_options = {}
    # Newer Streamlit versions moved style isolation from mounting to registration.
    if "isolate_styles" in signature(component).parameters:
        registration_options["isolate_styles"] = False
    return component(
        f"ledger_merchant_autocomplete_{version}",
        html=html,
        css=css,
        js=js,
        **registration_options,
    )


def merchant_selector(prefix: str, current_merchant: str = "") -> str:
    """Inline prefix completion; Tab, Enter, or blur commits the merchant."""
    from inspect import signature
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

    renderer = get_merchant_component(MERCHANT_HTML, MERCHANT_CSS, MERCHANT_JS)
    mount_options = {}
    if "isolate_styles" in signature(renderer).parameters:
        mount_options["isolate_styles"] = False
    result = renderer(
        data={"merchants": merchants, "value": initial},
        default={"value": initial},
        key=f"{prefix}_autocomplete_{st.session_state[instance_key]}",
        on_value_change=lambda: None,
        **mount_options,
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


def add_credit_transaction_form():
    """Record one dated credit charge or payment through the atomic database action."""
    accounts = sorted([a for a in load_budget_table('LedgerCreditAccounts') if a['active']],
        key=lambda a: (a['name'].casefold(), str(a['id'])))
    account_ids = [a['id'] for a in accounts]
    selected = st.selectbox('Line of credit', account_ids + [None],
        index=account_ids.index(st.session_state['new_credit_account_id'])
            if st.session_state.get('new_credit_account_id') in account_ids else 0,
        format_func=lambda i: next((a['name'] for a in accounts if a['id'] == i),
            'Add a new line of credit'))
    if selected is None:
        new_name = st.text_input('New credit account name')
        new_type = st.selectbox('New account type',
            ['Credit Card', 'Line of Credit', 'Lender', 'Loan'])
        if st.button('Create credit account'):
            try:
                response = conn.rpc('ledger_create_credit_account',
                    {'p_name': new_name, 'p_type': new_type}).execute()
                if not response.data:
                    raise RuntimeError('Account creation was not confirmed.')
                st.session_state['new_credit_account_id'] = response.data
                st.session_state.pop('credit_snapshot', None)
                st.rerun()
            except Exception as exc:
                st.error('Credit account was not created: ' + str(getattr(exc, 'message', None) or exc))
        return
    account = next(a for a in accounts if a['id'] == selected)
    if account.get('live_balance') is None:
        st.error('Set this account’s opening balance on Lines of Credit before adding a transaction.')
        return
    side = st.radio('Credit action', ['Charge', 'Payment'], horizontal=True,
        key='new_credit_side')
    st.caption('A charge increases credit owed without reducing checking. A payment appears in checking now and reduces credit owed only when you mark it complete on the calendar.')
    amount = st.number_input('Amount ($)', min_value=0.01, max_value=999999999.99,
        value=None, format='%.2f', key='new_credit_amount')
    merchant = st.selectbox('Merchant', get_existing_merchants(), index=None,
        placeholder='Select or enter a merchant', accept_new_options=True,
        key='new_credit_merchant')
    categories = list(get_existing_categories())
    if 'Credit payment' not in categories:
        categories.append('Credit payment')
    category = st.selectbox('Category', sorted(categories, key=str.casefold),
        index=None, placeholder='Select or enter a category', accept_new_options=True,
        key='new_credit_category')
    description = st.text_input('Description', key='new_credit_description')
    tx_date = st.date_input('Date', value=datetime.now(LOCAL_TZ).date(),
        key='new_credit_date')
    tx_time = st.time_input('Time', value=datetime.now(LOCAL_TZ).time().replace(microsecond=0),
        key='new_credit_time')
    if st.button('Save credit transaction', type='primary', use_container_width=True):
        if amount is None or not str(merchant or '').strip() or not str(category or '').strip():
            st.error('Enter an amount, merchant, and category.')
            return
        payload = dict(credit_account_id=selected,cash_account_id=active_cash_id(),
            credit_action=side,amount=str(money(amount)),date=tx_date.isoformat(),
            time=build_time_string(tx_date,tx_time),merchant=str(merchant).strip(),
            category=str(category).strip(),description=description,
            payment_amount=None, payment_date=None)
        try:
            response = conn.rpc('ledger_save_credit_transaction', {'p_data': payload}).execute()
            if response.data is not True:
                raise RuntimeError('Transaction save was not confirmed.')
            clear_transaction_caches()
            st.session_state.pop('credit_snapshot', None)
            st.rerun()
        except Exception as exc:
            st.error('Credit transaction was not saved: ' + str(getattr(exc, 'message', None) or exc))


def edit_credit_transaction_form(row):
    """Edit a dated credit entry; database triggers keep its balance in sync."""
    accounts = {a['id']: a for a in load_budget_table('LedgerCreditAccounts')}
    account = accounts.get(row.get('credit_account_id'))
    if not account:
        st.error('The credit account could not be found. Reload the page.')
        return
    st.write(account['name'] + ' · ' + str(row.get('credit_action') or 'Credit entry'))
    if row.get('unified_occurrence_id'):
        st.caption('Changes here affect this payment only; the monthly budget rule stays unchanged.')
    monthly_reserve = row.get('credit_budget_month') is not None
    if monthly_reserve:
        spent = credit_month_spent(get_all_transactions_cached(),row['credit_account_id'],row['credit_budget_month'])
        st.caption(f"Monthly budget: \\${money(row['credit_budget_amount']):,.2f} · Charges: \\${spent:,.2f}")
    if row.get('credit_action') == 'Payment':
        if row.get('credit_applied_at'):
            st.success('This payment has been applied to the credit balance.')
        else:
            st.info('This payment is in the checking projection. The credit balance changes when you mark it complete.')
            actual_payment = None
            if monthly_reserve:
                actual_payment = st.number_input('Amount actually paid ($)', min_value=0.01,max_value=999999999.99, value=float(min(max(spent,Decimal('0.01')),money(account['live_balance']))) if money(account['live_balance'])>0 else 0.01,format='%.2f',key='actual_credit_'+str(row['id']))
            if st.button('Mark credit payment complete', type='primary',
                         key='complete_credit_' + str(row['id'])):
                try:
                    payload = {'p_id': row['id'], 'p_revision': row['check_revision']}
                    if monthly_reserve:
                        payload['p_amount'] = str(money(actual_payment))
                    response = conn.rpc('ledger_complete_monthly_credit_payment' if monthly_reserve else 'ledger_complete_credit_payment',payload).execute()
                    if response.data is not True:
                        raise RuntimeError('Completion was not confirmed.')
                    clear_transaction_caches()
                    st.session_state.pop('credit_snapshot', None)
                    st.rerun()
                except Exception as exc:
                    st.error('Payment was not completed: ' + str(getattr(exc, 'message', None) or exc))
    with st.form('credit_edit_' + str(row['id'])):
        amount = st.number_input('Amount ($)', min_value=0.01, max_value=999999999.99,
            value=float(money(row['amount'])), format='%.2f',disabled=monthly_reserve and not row.get('credit_applied_at'))
        merchant = st.text_input('Merchant', value=row.get('merchant') or '')
        category = st.text_input('Category', value=row.get('category') or '')
        description = st.text_input('Description', value=row.get('description') or '')
        day = st.date_input('Date', value=date.fromisoformat(str(row['date'])[:10]))
        tx_time = st.time_input('Time', value=datetime.strptime(
            str(row.get('time') or '12:00:00')[:8], '%H:%M:%S').time())
        save = st.form_submit_button('Save this transaction', type='primary')
        delete = st.form_submit_button('Delete this transaction')
    if save or delete:
        if save and (not merchant.strip() or not category.strip()):
            st.error('Enter a merchant and category.')
            return
        try:
            table = conn.table('Transactions')
            if save:
                payload = dict(amount=str(money(amount)),merchant=merchant.strip(),
                    category=category.strip(),description=description,date=day.isoformat(),
                    time=build_time_string(day,tx_time))
                response = (table.update(payload).eq('id',row['id'])
                    .eq('check_revision',row['check_revision']).execute())
            else:
                response = (table.delete().eq('id',row['id'])
                    .eq('check_revision',row['check_revision']).execute())
            if transaction_write_succeeded(response,'credit transaction change'):
                clear_transaction_caches()
                st.session_state.pop('credit_snapshot', None)
                st.session_state.pop('credit_edit_id', None)
                st.rerun()
        except Exception as exc:
            st.error('Credit transaction was not changed: ' + str(getattr(exc, 'message', None) or exc))


def edit_federal_loan_payment_form(row):
    """A scheduled loan payment affects checking now and the loan when completed."""
    loans = {int(a['id']): a for a in load_budget_table('LedgerFederalLoans')}
    loan_id = int(row['federal_loan_id']) if row.get('federal_loan_id') else None
    loan = loans.get(loan_id)
    if not loan:
        st.error('The student loan could not be found. Reload the page.')
        return
    st.write(loan['name'] + ' · monthly payment')
    st.caption('Changes here affect only this payment, not its recurring budget item.')
    if row.get('federal_loan_applied_at'):
        st.success('This payment has been applied to the loan.')
        st.caption('Use a newer servicer statement to reconcile principal and unpaid interest.')
        return
    st.info('This payment is in the checking projection. The loan changes only when you mark it complete.')
    if st.button('Mark loan payment complete', type='primary',
                 key='complete_loan_' + str(row['id'])):
        try:
            response = conn.rpc('ledger_complete_federal_loan_payment',
                {'p_id': row['id'], 'p_revision': row['check_revision']}).execute()
            if response.data is not True:
                raise RuntimeError('Completion was not confirmed.')
            clear_transaction_caches()
            st.session_state.pop('credit_snapshot', None)
            st.rerun()
        except Exception as exc:
            st.error('Loan payment was not completed: ' + str(getattr(exc, 'message', None) or exc))
    with st.form('loan_payment_edit_' + str(row['id'])):
        amount = st.number_input('Amount ($)', min_value=0.01,
            max_value=999999999.99, value=float(money(row['amount'])), format='%.2f')
        category = st.text_input('Category', value=row.get('category') or 'Loan payment')
        description = st.text_input('Description', value=row.get('description') or '')
        day = st.date_input('Date', value=date.fromisoformat(str(row['date'])[:10]),
            min_value=date.fromisoformat(loan['as_of']))
        save = st.form_submit_button('Save this payment', type='primary')
        delete = st.form_submit_button('Delete this payment')
    if save or delete:
        try:
            table = conn.table('Transactions')
            if save:
                if not category.strip():
                    st.error('Enter a category.')
                    return
                response = (table.update(dict(amount=str(money(amount)),
                    category=category.strip(), description=description,
                    date=day.isoformat())).eq('id',row['id'])
                    .eq('check_revision',row['check_revision']).execute())
            else:
                response = (table.delete().eq('id',row['id'])
                    .eq('check_revision',row['check_revision']).execute())
            if transaction_write_succeeded(response,'loan payment change'):
                clear_transaction_caches()
                st.session_state.pop('credit_snapshot', None)
                st.rerun()
        except Exception as exc:
            st.error('Loan payment was not changed: ' + str(getattr(exc, 'message', None) or exc))


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
         (["AMZ Card"] if active_cash_id() == 1 else []) + ["Direct", "Check", "Transfer", "Line of credit"]),
        horizontal=True,
        key="add_workflow_type",
    )

    if workflow_type == "Transfer":
        transfer_form()
        return
    if workflow_type == "Line of credit":
        add_credit_transaction_form()
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


def linked_current_budget_savings_category(original):
    """Find an unambiguous current transfer version of an older Direct rule."""
    occurrence_id = original.get('unified_occurrence_id')
    if occurrence_id is None:
        return None
    occurrences = load_budget_table('LedgerUnifiedBudgetOccurrences')
    occurrence = next((o for o in occurrences if int(o['id']) == int(occurrence_id)), None)
    if occurrence is None:
        return None
    rules = load_budget_table('LedgerUnifiedBudgetRules')
    old_rule = next((r for r in rules if int(r['id']) == int(occurrence['rule_id'])), None)
    if old_rule is None:
        return None
    matches = [r for r in rules if r['series_id'] == old_rule['series_id']
        and r['transaction_type'] == 'Transfer' and r['effective_until'] is None
        and r['paid_from'] == 'c:' + str(original['cash_account_id'])
        and r.get('destination_savings_bucket_id') is not None]
    return int(matches[0]['destination_savings_bucket_id']) if len(matches) == 1 else None


def edit_pending_budget_transfer_form_by_id(occurrence_id):
    require_session()
    occurrence = next((r for r in load_budget_table('LedgerUnifiedBudgetOccurrences')
        if int(r['id']) == int(occurrence_id)), None)
    if not occurrence or occurrence['cancelled'] or occurrence.get('transfer_id') is not None:
        st.error('This transfer changed. Reload the calendar.')
        return
    accounts = unified_account_options()
    st.write(occurrence['name'])
    st.caption(accounts.get(occurrence['paid_from'], occurrence['paid_from']) + ' → ' +
               accounts.get(occurrence['paid_to'], occurrence['paid_to']))
    st.info('This amount is in the checking projection. Savings will change only when you mark the transfer made.')
    st.caption('Save changes / keep pending changes only this occurrence. The recurring schedule stays unchanged.')
    buckets = load_budget_table('LedgerSavingsBuckets')
    with st.form('pending_savings_transfer_' + str(occurrence_id)):
        transfer_date = st.date_input('Transfer date', value=date.fromisoformat(occurrence['actual_date']))
        amount = st.number_input('Transfer amount ($)', min_value=0.01, max_value=999999999.99,
            value=float(money(occurrence['amount'])), format='%.2f')
        selected = {'source': None, 'destination': None}
        for side, ref in (('source', occurrence['paid_from']), ('destination', occurrence['paid_to'])):
            if not ref.startswith('s:'):
                continue
            choices = {int(b['id']): b for b in buckets
                if int(b['savings_account_id']) == int(ref[2:])
                and b['active'] and b['balance'] is not None}
            if not choices:
                st.error('Establish an active savings category balance before making this transfer.')
                return
            preferred = occurrence.get(side + '_savings_bucket_id')
            general = next((i for i, b in choices.items() if b['is_general']), None)
            preferred = int(preferred) if preferred is not None else general
            ids = list(choices)
            selected[side] = st.selectbox(side.capitalize() + ' savings category', ids,
                index=ids.index(preferred) if preferred in ids else 0,
                format_func=lambda i: choices[i]['name'])
        save_pending = st.form_submit_button('Save changes / keep pending')
        made = st.form_submit_button('Mark transfer made', type='primary')
        confirm_delete = st.checkbox('Confirm delete of this one scheduled transfer')
        deleted = st.form_submit_button('Delete this occurrence')
    if save_pending:
        account_action('ledger_edit_pending_savings_occurrence', dict(
            p_id=occurrence['id'], p_revision=occurrence['revision'],
            p_date=transfer_date.isoformat(), p_amount=str(money(amount)),
            p_source_bucket=selected['source'], p_destination_bucket=selected['destination']))
    if made:
        account_action('ledger_confirm_budget_savings_occurrence', dict(
            p_id=occurrence['id'], p_revision=occurrence['revision'],
            p_date=transfer_date.isoformat(), p_amount=str(money(amount)),
            p_source_bucket=selected['source'], p_destination_bucket=selected['destination']))
    if deleted:
        if not confirm_delete:
            st.error('Confirm deletion of this occurrence first.')
        else:
            account_action('ledger_cancel_pending_savings_occurrence', dict(
                p_id=occurrence['id'], p_revision=occurrence['revision']))

def edit_pending_budget_transfer_form(row):
    edit_pending_budget_transfer_form_by_id(row["id"])


@st.dialog("Edit Existing Transaction", width="medium")
def edit_transaction_dialog():
    require_session()
    transactions = get_all_transactions_cached()
    cash_names = {k:v['name'] for k,v in cash_accounts().items()}
    credit_names = {a['id']:a['name'] for a in load_budget_table('LedgerCreditAccounts')}
    loan_names = {int(a['id']):a['name'] for a in load_budget_table('LedgerFederalLoans')}
    tx_list = sorted(transactions, key=lambda row:int(row['id']), reverse=True)
    tx_options = {}
    for row in tx_list:
        account_name = (credit_names.get(row.get('credit_account_id')) or
            loan_names.get(int(row['federal_loan_id'])) if row.get('federal_loan_id') is not None
            else credit_names.get(row.get('credit_account_id')))
        account_name = account_name or cash_names.get(int(row.get('cash_account_id') or 1),'Checking')
        label = (f"ID {row['id']} | {account_name} | {row.get('type','')} "
            f"{row.get('credit_action') or ''} | {row.get('date','')} | "
            f"{row.get('merchant','')} | ${money(row['amount']):,.2f}")
        tx_options[label] = row
    # Savings-only transfers and pending transfers have their own identifiers,
    # so keep them below the transaction IDs rather than mixing ID sequences.
    represented = {int(r['transfer_id']) for r in transactions if r.get('transfer_id') is not None}
    for transfer in sorted(load_budget_table('LedgerTransfers'),key=lambda r:int(r['id']),reverse=True):
        if int(transfer['id']) not in represented:
            tx_options[f"Savings transfer {transfer['id']} | {transfer['date']} | ${money(transfer['amount']):,.2f} | {transfer.get('description','')}"] = dict(transfer,_editor_kind='transfer')
    for plan in sorted(load_budget_table('LedgerManualSavingsPlans'),key=lambda r:int(r['id']),reverse=True):
        if not plan.get('cancelled') and plan.get('transfer_id') is None:
            tx_options[f"Pending savings transfer {plan['id']} | {plan['date']} | ${money(plan['amount']):,.2f} | {plan.get('description','')}"] = dict(plan,_editor_kind='manual')
    for occurrence in sorted(load_budget_table('LedgerUnifiedBudgetOccurrences'),key=lambda r:int(r['id']),reverse=True):
        if occurrence['transaction_type']=='Transfer' and not occurrence['cancelled'] and occurrence.get('transfer_id') is None:
            tx_options[f"Budget savings transfer {occurrence['id']} | {occurrence['actual_date']} | {occurrence['name']} | ${money(occurrence['amount']):,.2f}"] = dict(occurrence,_editor_kind='budget-transfer')
    if not tx_options:
        st.info('No transactions found to edit.')
        return

    requested_id = st.session_state.pop('calendar_edit_id', None)
    if requested_id is not None:
        match = next((label for label, row in tx_options.items() if not row.get('_editor_kind') and str(row['id']) == str(requested_id)), None)
        if match is None:
            st.error('This transaction is no longer available. Reload the calendar.')
            return
        st.session_state['edit_tx_select_dropdown'] = match
        st.session_state.pop('edit_loaded_tx_id', None)
        st.session_state.pop('edit_auto_transfer_decided', None)
    if st.session_state.get('edit_tx_select_dropdown') not in tx_options:
        st.session_state.pop('edit_tx_select_dropdown', None)
    selected_label = st.selectbox(
        "Select Transaction to Edit",
        list(tx_options.keys()),
        key="edit_tx_select_dropdown",
    )

    selected_tx = tx_options[selected_label]
    extra_kind = selected_tx.get('_editor_kind')
    if extra_kind == 'transfer':
        transfer_form(selected_tx)
        return
    if extra_kind == 'manual':
        transfer_form(selected_tx, pending=True)
        return
    if extra_kind == 'budget-transfer':
        edit_pending_budget_transfer_form(selected_tx)
        return
    st.caption('Editing across all accounts. The saved account remains attached to this entry.')
    if selected_tx.get('transfer_id') is not None:
        transfer = next((t for t in load_budget_table('LedgerTransfers') if t['id'] == selected_tx['transfer_id']), None)
        if transfer is None: st.error('Transfer changed. Reload the page.'); return
        transfer_form(transfer)
        return
    if selected_tx.get('type') == 'Federal student loan':
        edit_federal_loan_payment_form(selected_tx)
        return
    if selected_tx.get('type') == 'Line of credit':
        edit_credit_transaction_form(selected_tx)
        return
    initialize_edit_transaction_state(selected_tx)
    selected_tx = deepcopy(st.session_state["edit_original_row"])
    render_check_clearance(selected_tx)
    budget_generated = selected_tx.get('unified_occurrence_id') is not None
    if budget_generated:
        st.caption('Amount, date, and payment-type edits apply only to this occurrence. The recurring budget item stays unchanged.')
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

    workflow_types = (["AMZ Card"] if int(selected_tx.get('cash_account_id') or 1) == 1 else []) + ["Direct", "Check"]
    if selected_tx.get('type') == 'Savings Transfer': workflow_types.append('Savings Transfer')
    if (selected_tx.get('type') == 'Direct' and selected_tx.get('direction') == 'Expense'
            and selected_tx.get('transfer_id') is None and not paid_week):
        workflow_types.append('Transfer')
        if st.session_state.get('edit_auto_transfer_decided') != selected_tx['id']:
            if linked_current_budget_savings_category(selected_tx) is not None:
                st.session_state['edit_workflow_type'] = 'Transfer'
            st.session_state['edit_auto_transfer_decided'] = selected_tx['id']

    if st.session_state["edit_workflow_type"] not in workflow_types:
        st.session_state["edit_workflow_type"] = workflow_types[0]

    workflow_type = st.radio(
        "Transaction Type",
        workflow_types,
        format_func=lambda v: "Transfer" if v == "Savings Transfer" else v,
        horizontal=True,
        key="edit_workflow_type", disabled=bool(paid_week),
    )

    if workflow_type == 'Transfer':
        convert_direct_to_savings_form(selected_tx)
        return
    render_transaction_completion(selected_tx)
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
            "cash_account_id": int(selected_tx.get('cash_account_id') or 1),
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

def month_year_picker(label, key):
    """Default to the actual local month; preserve a manual choice during this session."""
    today = datetime.now(LOCAL_TZ).date()
    month_col, year_col = st.columns([2, 1])
    with month_col:
        month = st.selectbox(label + ' month', list(range(1, 13)),
            index=today.month - 1,
            format_func=lambda value: date(2000, value, 1).strftime('%B'),
            key=key + '_month')
    with year_col:
        year = st.number_input(label + ' year', min_value=1900, max_value=2100,
            value=today.year, step=1, key=key + '_year')
    return date(int(year), int(month), 1)


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
        if kind == 'Line of credit' and row.get('credit_action') == 'Charge':
            continue  # Credit spending does not reduce checking cash.
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
        elif kind in ('Direct', 'Check', 'Savings Transfer', 'Line of credit', 'Federal student loan'):
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
            money(row['paid_amount']) for row in payments.get(day, [])), Decimal(0))
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
        const button = event.target.closest('button[data-transaction],button[data-pending-transfer],button[data-planned],button[data-payday],button[data-card-budget],button[data-card-amount],button[data-review],button[data-savings],button[data-day]');
        if (!button || !root.contains(button)) return;
        if (button.dataset.day) {
            setTriggerValue('action', {day: button.dataset.day});
        } else if (button.dataset.cardBudget) {
            setTriggerValue('action', {card_budget: button.dataset.cardBudget});
        } else if (button.dataset.savings) {
            setTriggerValue('action', {savings: button.dataset.savings});
        } else if (button.dataset.cardAmount) {
            setTriggerValue('action', {card_amount: button.dataset.cardAmount});
        } else if (button.dataset.transaction) {
            setTriggerValue('action', {id: button.dataset.transaction});
        } else if (button.dataset.pendingTransfer) {
            setTriggerValue('action', {pending_transfer: button.dataset.pendingTransfer});
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


@st.dialog('Edit planned entry for this month', width='medium')
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
        delete = st.form_submit_button('Delete this month’s entry')
    if save or delete:
        try:
            payload = occurrence_change(item, rule, amount, enabled=False if delete else None)
            response = conn.rpc('ledger_save_budget_edits', {'p_month': item['month'], 'p_changes': [payload]}).execute()
            if response.data is not True:
                raise RuntimeError('Save not confirmed')
            invalidate_budget_views()
            st.session_state.pop(key, None)
            st.rerun()
        except Exception:
            st.error('Nothing was saved. The item may have changed. Close and reopen it before trying again.')


def occurrence_change(item, rule, amount, enabled=None):
    return dict(kind='bill', scope='This month only', id=item['id'], revision=item['revision'],
                rule_revision=rule['revision'], name=item['name'], description=item.get('description') or '',
                amount=str(money(amount)), day=item.get('due_day') or date.fromisoformat(item['due_date']).day,
                direction=item['direction'], schedule='As needed' if item.get('schedule') == 'as_needed' else 'Monthly',
                enabled=item['enabled'] if enabled is None else enabled,
                transaction_id=item.get('transaction_id'))


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


def render_transaction_completion(row):
    if row.get('type') not in ('Direct', 'Savings Transfer') or row.get('transfer_id') is not None:
        return
    completed = bool(row.get('completed_at'))
    st.caption('Status: ' + ('Completed' if completed else 'Not marked completed'))
    st.caption('This check mark does not move money. For an older Direct entry that belongs in savings, '
               'choose Transfer under Transaction Type instead. Save edits before changing status.')
    if st.button('Remove completed mark' if completed else 'Mark completed',
                 key='complete_saved_transaction_' + str(row['id'])):
        require_session()
        try:
            result = conn.rpc('ledger_set_transaction_completed', {
                'p_id': int(row['id']), 'p_revision': int(row['check_revision']),
                'p_completed': not completed}).execute()
            if result.data is not True:
                raise RuntimeError('Completion not confirmed')
            clear_transaction_caches()
            st.session_state.pop('edit_loaded_tx_id', None)
            st.rerun()
        except Exception:
            clear_transaction_caches()
            st.error('Completion could not be confirmed. Close and reopen the transaction before retrying.')


def render_check_calendar(first, balances, direct, settings):
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    try:
        response = conn.table('LedgerDayMarkers').select('day,color').eq('account_id', active_cash_id()).gte(
            'day', first.isoformat()).lte('day', last.isoformat()).execute()
        markers = {row['day']: row['color'] for row in response.data or []}
    except Exception:
        logger.exception('Calendar day markers could not be loaded.')
        st.error('Day markers could not be loaded. Apply the follow-up SQL update, then reload.')
        return
    markup = calendar_grid_html(first, balances, direct, settings,
        show_cards=active_cash_id()==1, markers=markers)
    actual_ids = {str(r['id']) for rows in direct.values() for r in rows
                  if not r.get('planned') and r.get('type') in ('Direct', 'Check', 'AMZ Card', 'Savings Transfer', 'Line of credit', 'Federal student loan')}
    planned_ids = {str(r['budget_item_id']) for rows in direct.values() for r in rows if r.get('planned') and r.get('budget_item_id') is not None}
    payday_ids = {str(r['payday_id']) for rows in direct.values() for r in rows if r.get('payday_id') is not None}
    pending_transfer_ids = {str(r['pending_transfer_id']) for rows in direct.values()
        for r in rows if r.get('pending_transfer_id') is not None}
    event = ledger_interaction(data={'html': markup}, key='check_calendar',
                               on_action_change=lambda: None).action
    if event and isinstance(event, dict) and event.get('day'):
        require_session()
        try:
            selected = date.fromisoformat(str(event['day']))
            if selected < first or selected > last:
                raise ValueError('Choose a day in the displayed month.')
            response = conn.rpc('ledger_cycle_day_marker', {
                'p_account': active_cash_id(), 'p_day': selected.isoformat()}).execute()
            if response.data not in ('green', 'yellow', 'clear'):
                raise RuntimeError('Marker save was not confirmed.')
            st.rerun()
        except Exception as exc:
            st.error('Day marker was not saved: ' + str(getattr(exc, 'message', None) or exc))
    elif event and event.get('card_budget') and active_cash_id() == 1:
        try:
            selected = date.fromisoformat(str(event['card_budget']))
            if not first <= selected <= last or selected.weekday() != 5:
                raise ValueError('Choose a Saturday in the displayed month.')
        except ValueError:
            st.error('Choose a card budget in the displayed calendar.')
        else:
            st.session_state.pop('card_budget_edit_' + selected.isoformat(), None)
            calendar_card_budget_dialog(selected.isoformat())
    elif event and str(event.get('id')) in actual_ids:
        require_session()
        clear_transaction_caches()
        st.session_state['calendar_edit_id'] = str(event['id'])
        edit_transaction_dialog()
    elif event and str(event.get('pending_transfer')) in pending_transfer_ids:
        selected_transfer = str(event['pending_transfer'])
        if selected_transfer.startswith('manual-'):
            manual_savings_transfer_dialog(int(selected_transfer[7:]))
        else:
            pending_savings_transfer_dialog(int(selected_transfer))
    elif event and str(event.get('planned')) in planned_ids:
        require_session()
        st.session_state.pop('occurrence_edit_' + str(event['planned']), None)
        planned_occurrence_dialog(int(event['planned']))
    elif event and str(event.get('payday')) in payday_ids:
        require_session()
        st.session_state.pop('payday_edit_' + str(event['payday']), None)
        payday_occurrence_dialog(int(event['payday']))


@st.dialog('Edit weekly card budget', width='medium')
def calendar_card_budget_dialog(week):
    require_session()
    if active_cash_id() != 1:
        st.error('AMZ card budgets belong to Primary Checking.')
        return
    try:
        selected = date.fromisoformat(week)
        if selected.weekday() != 5:
            raise ValueError('Choose a Saturday.')
        if week in load_card_weeks():
            st.info('This week is reconciled. Its budget is locked.')
            return
        snapshot_key = 'card_budget_edit_' + week
        if snapshot_key not in st.session_state:
            st.session_state[snapshot_key] = {'previous': deepcopy(load_calendar_settings().get('budget:' + week))}
        previous = st.session_state[snapshot_key]['previous']
    except Exception:
        st.error('This card budget could not be loaded. Close and reopen it.')
        return
    st.write('Week ending ' + selected.strftime('%B %d, %Y'))
    st.caption('Changes only this week. Other weekly budgets and purchases stay unchanged.')
    with st.form('calendar_card_budget_' + week):
        amount = st.number_input('Weekly card budget ($)', min_value=0.0,
            max_value=999999999.99, value=float(money(previous['amount'])) if previous else 0.0,
            format='%.2f')
        saved = st.form_submit_button('Save weekly budget')
    if saved:
        try:
            if previous:
                response = conn.rpc('ledger_edit_card_week_budget', {
                    'p_week': week, 'p_revision': previous['revision'],
                    'p_amount': str(money(amount))}).execute()
                if response.data is not True:
                    raise RuntimeError('Save was not confirmed.')
            elif not save_calendar_setting('budget:' + week, amount, None):
                return
            clear_transaction_caches()
            st.session_state.pop(snapshot_key, None)
            st.rerun()
        except Exception:
            st.error('The budget was not confirmed. Close and reopen it to check the saved amount before retrying.')


@st.dialog('Budgeted savings transfer', width='medium')
def pending_savings_transfer_dialog(occurrence_id):
    require_session()
    occurrence = next((r for r in load_budget_table('LedgerUnifiedBudgetOccurrences')
        if int(r['id']) == int(occurrence_id)), None)
    if not occurrence or occurrence['cancelled'] or occurrence.get('transfer_id') is not None:
        st.error('This transfer changed. Reload the calendar.')
        return
    accounts = unified_account_options()
    st.write(occurrence['name'])
    st.caption(accounts.get(occurrence['paid_from'], occurrence['paid_from']) + ' → ' +
               accounts.get(occurrence['paid_to'], occurrence['paid_to']))
    st.info('This amount is in the checking projection. Savings will change only when you mark the transfer made.')
    st.caption('Save changes / keep pending changes only this occurrence. The recurring schedule stays unchanged.')
    buckets = load_budget_table('LedgerSavingsBuckets')
    with st.form('pending_savings_transfer_' + str(occurrence_id)):
        transfer_date = st.date_input('Transfer date', value=date.fromisoformat(occurrence['actual_date']))
        amount = st.number_input('Transfer amount ($)', min_value=0.01, max_value=999999999.99,
            value=float(money(occurrence['amount'])), format='%.2f')
        selected = {'source': None, 'destination': None}
        for side, ref in (('source', occurrence['paid_from']), ('destination', occurrence['paid_to'])):
            if not ref.startswith('s:'):
                continue
            choices = {int(b['id']): b for b in buckets
                if int(b['savings_account_id']) == int(ref[2:])
                and b['active'] and b['balance'] is not None}
            if not choices:
                st.error('Establish an active savings category balance before making this transfer.')
                return
            preferred = occurrence.get(side + '_savings_bucket_id')
            general = next((i for i, b in choices.items() if b['is_general']), None)
            preferred = int(preferred) if preferred is not None else general
            ids = list(choices)
            selected[side] = st.selectbox(side.capitalize() + ' savings category', ids,
                index=ids.index(preferred) if preferred in ids else 0,
                format_func=lambda i: choices[i]['name'])
        save_pending = st.form_submit_button('Save changes / keep pending')
        made = st.form_submit_button('Mark transfer made', type='primary')
        confirm_delete = st.checkbox('Confirm delete of this one scheduled transfer')
        deleted = st.form_submit_button('Delete this occurrence')
    if save_pending:
        account_action('ledger_edit_pending_savings_occurrence', dict(
            p_id=occurrence['id'], p_revision=occurrence['revision'],
            p_date=transfer_date.isoformat(), p_amount=str(money(amount)),
            p_source_bucket=selected['source'], p_destination_bucket=selected['destination']))
    if made:
        account_action('ledger_confirm_budget_savings_occurrence', dict(
            p_id=occurrence['id'], p_revision=occurrence['revision'],
            p_date=transfer_date.isoformat(), p_amount=str(money(amount)),
            p_source_bucket=selected['source'], p_destination_bucket=selected['destination']))
    if deleted:
        if not confirm_delete:
            st.error('Confirm deletion of this occurrence first.')
        else:
            account_action('ledger_cancel_pending_savings_occurrence', dict(
                p_id=occurrence['id'], p_revision=occurrence['revision']))


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


def render_payday_tables():
    st.subheader('Payday income')
    st.caption('Bill: every Friday, with a separate amount for each Friday of the month. '
               'Spouse: alternate Wednesdays, anchored to January 14, 2026. '
               'Editing a calendar deposit changes only that date. '
               'Dates do not shift for holidays.')
    try:
        defaults = {r['stream']: r for r in load_budget_table('LedgerPaydayDefaults')}
        weeks = {int(r['week_number']): r for r in load_budget_table('LedgerBillPaydayWeeks')}
        if set(weeks) != set(range(1, 6)):
            raise ValueError('Bill payday weeks are incomplete')
    except Exception:
        st.error('Payday budget could not be loaded. Install the payday pattern SQL update, then reload.')
        return
    st.markdown('**Bill: amount by Friday of the month**')
    weekly_rows = [{'Friday': f'{n}{"st" if n == 1 else "nd" if n == 2 else "rd" if n == 3 else "th"}',
                    'Amount': None if weeks[n]['amount'] is None else float(money(weeks[n]['amount']))}
                   for n in range(1, 6)]
    with st.form('bill_payday_week_amounts'):
        weekly_edits = st.data_editor(pd.DataFrame(weekly_rows), hide_index=True,
            use_container_width=True, num_rows='fixed', disabled=['Friday'],
            column_config={'Amount': st.column_config.NumberColumn(
                min_value=0.0, max_value=999999999.99, format='$%.2f')})
        save_weekly = st.form_submit_button('Save Bill payday amounts')
    if save_weekly:
        try:
            changes = []
            edited_rows = weekly_edits.to_dict('records')
            if len(edited_rows) != 5 or [r['Friday'] for r in edited_rows] != [r['Friday'] for r in weekly_rows]:
                raise ValueError('Payday rows changed')
            for n, row in enumerate(edited_rows, 1):
                amount = None if pd.isna(row['Amount']) else money(row['Amount'])
                old = None if weeks[n]['amount'] is None else money(weeks[n]['amount'])
                if amount != old:
                    changes.append(dict(week_number=n, revision=weeks[n]['revision'],
                        amount=None if amount is None else str(amount)))
            if changes:
                response = conn.rpc('ledger_save_bill_payday_weeks', {'p_changes': changes}).execute()
                if response.data is not True:
                    raise RuntimeError('Save not confirmed')
                invalidate_budget_views()
                st.rerun()
            st.info('No Bill payday amounts to save.')
        except Exception:
            st.error('Bill payday amounts were not saved. Reload and try again.')
    st.markdown('**Payday settings**')
    with st.form('payday_recurring_defaults'):
        bill_include = st.checkbox('Include Bill paydays', value=bool(defaults['Bill']['enabled']))
        bill_description = st.text_input('Bill payday description', value=defaults['Bill'].get('description') or '')
        spouse_amount = st.number_input('Spouse recurring payday amount ($)',
            value=None if defaults['Spouse']['amount'] is None else float(money(defaults['Spouse']['amount'])),
            min_value=0.0, max_value=999999999.99, format='%.2f')
        spouse_include = st.checkbox('Include Spouse paydays', value=bool(defaults['Spouse']['enabled']))
        spouse_description = st.text_input('Spouse payday description', value=defaults['Spouse'].get('description') or '')
        save_defaults = st.form_submit_button('Save payday settings', type='primary')
    if save_defaults:
        try:
            changes = []
            for stream, amount, enabled, description in (
                ('Bill', None if defaults['Bill']['amount'] is None else money(defaults['Bill']['amount']),
                 bill_include, bill_description),
                ('Spouse', None if spouse_amount is None else money(spouse_amount),
                 spouse_include, spouse_description)):
                old = defaults[stream]
                description = str(description or '')
                if (amount, enabled, description) == (
                    None if old['amount'] is None else money(old['amount']),
                    old['enabled'], old.get('description') or ''):
                    continue
                changes.append(dict(stream=stream, revision=old['revision'],
                    amount=None if amount is None else str(amount), enabled=enabled,
                    description=description))
            if changes:
                response = conn.rpc('ledger_save_payday_defaults', {'p_changes': changes}).execute()
                if response.data is not True:
                    raise RuntimeError('Save not confirmed')
                invalidate_budget_views()
                st.rerun()
            st.info('No payday settings to save.')
        except Exception:
            st.error('Payday settings were not saved. Reload the page and try again.')
    st.caption('Future dated deposits use these amounts. Previously recorded and individually edited deposits stay as saved.')
    month = datetime.now(LOCAL_TZ).date().replace(day=1)
    snapshot_key = 'payday_snapshot_all'
    if st.button('Reload payday income / discard unsaved changes'):
        st.session_state.pop(snapshot_key, None)
        st.session_state['payday_generation'] = st.session_state.get('payday_generation', 0) + 1
        st.rerun()
    try:
        if snapshot_key not in st.session_state:
            last = month.replace(day=monthrange(month.year, month.month)[1])
            ensure_paydays(month, last)
            clear_transaction_caches()
            st.session_state[snapshot_key] = dict(rows=load_budget_table('LedgerPaydays'),
                transactions=get_transactions_cached())
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
        frame = pd.DataFrame(display).sort_values(['Date', 'Item'], ascending=[False, True])
        frame['Amount'] = pd.to_numeric(frame['Amount'], errors='coerce')
    except Exception:
        st.error('Payday income could not be loaded. Install payday_review_update.sql and reload.')
        return
    with st.expander('Individual payday deposits and actual links'):
        st.caption('All prepared dates appear here. Calendar edits affect one deposit; linking an actual income transaction replaces its planned deposit.')
        with st.form('payday_income_all'):
            edits = []
            for stream in ('Bill', 'Spouse'):
                st.subheader(stream)
                edits.append(st.data_editor(frame[frame['Item'] == stream].copy(), hide_index=True,
                    use_container_width=True, num_rows='fixed',
                     key='payday_' + stream + '_all_' + str(st.session_state.get('payday_generation', 0)),
                    disabled=['_id', 'Item', 'Amount', 'Date', 'Direction', 'Schedule', 'Include', 'Description', 'Status'],
                    column_order=['Item', 'Amount', 'Date', 'Direction', 'Schedule', 'Include', 'Description', 'Actual transaction', 'Status'],
                    column_config={'_id': None, 'Amount': st.column_config.NumberColumn(min_value=0.0,max_value=999999999.99,format='$%.2f'),
                        'Date': st.column_config.DateColumn(format='MMM D, YYYY'),
                        'Actual transaction': st.column_config.SelectboxColumn(options=list(labels), required=True)}))
            saved = st.form_submit_button('Save actual deposit links')
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
            if amount is None:
                st.error('Enter an amount for this deposit.')
                return
            result = conn.rpc('ledger_edit_payday_occurrence', {
                'p_id': row['id'], 'p_revision': row['revision'],
                'p_amount': str(money(amount))}).execute()
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
    month = month_year_picker('Review', 'card_review')
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
    st.write(rf'Payment to record: **\${total:,.2f}** · Budget surplus: **\${max(budget-total, Decimal(0)):,.2f}**')
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
    if row.get('credit_budget_month'):
        displayed += ' · ' + str(row.get('merchant') or 'Credit payment')
        if not row.get('credit_applied_at'):
            spent = money(row.get('credit_month_spent'))
            remaining = max(money(row.get('credit_budget_amount'))-spent,Decimal(0))
            displayed += f' · Spent ${spent:,.2f} · Remaining ${remaining:,.2f}'
    description = str(row.get('description') or 'Not provided')
    merchant = str(row.get('merchant') or 'Not provided')
    tooltip = f"Amount: {displayed}\nDescription: {description}\nMerchant: {merchant}"
    if row.get('pending_transfer_id') is not None:
        return (f'<button type="button" data-pending-transfer="{escape(str(row["pending_transfer_id"]), quote=True)}" '
                f'title="{escape(tooltip, quote=True)}" aria-label="{escape("Confirm transfer: " + tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:0;background:transparent;'
                f'color:{"#238636" if is_income else "#d14343"};padding:3px 0;font:inherit;cursor:pointer;">'
                f'{escape(displayed)}</button>')
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
    if not row.get('planned') and row.get('type') in ('Direct', 'AMZ Card', 'Savings Transfer', 'Line of credit', 'Federal student loan'):
        marked = ' ✓' if row.get('completed_at') and row.get('type') != 'AMZ Card' else ''
        return (f'<button type="button" data-transaction="{int(row["id"])}" '
                f'title="{escape(tooltip, quote=True)}" aria-label="{escape("Edit transaction: " + tooltip, quote=True)}" '
                f'style="display:block;width:100%;text-align:left;border:0;background:transparent;'
                f'color:{"#238636" if is_income else "#d14343"};padding:3px 0;font:inherit;cursor:pointer;">'
                f'{escape(displayed)}{marked}</button>')
    color = '#238636' if is_income else '#d14343' 
    return (
        f'<span tabindex="0" title="{escape(tooltip, quote=True)}" '
        f'aria-label="{escape(tooltip, quote=True)}" '
        f'style="display:block;cursor:help;color:{color};padding:3px 0;'
        f'font-variant-numeric:tabular-nums;">{escape(displayed)}</span>'
    )


def calendar_grid_html(first, balances, direct, settings, show_cards=True, markers=None):
    """One CSS grid gives every day the height required by the busiest day."""
    markers = markers or {}
    cells = []
    for week in Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        for day in week:
            if day.month != first.month:
                cells.append('<div class="ledger-day ledger-outside"><header><span class="ledger-date">'
                             + escape(day.strftime('%b %d')) + '</span></header><div class="ledger-body"></div><footer>&nbsp;</footer></div>')
                continue
            values = balances[day]
            warning = (' ledger-header-negative' if values['balance'] < 0 else
                       ' ledger-header-low' if values['balance'] < 500 else '')
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
                    content.append(
                        f'<button type="button" data-card-budget="{day.isoformat()}" '
                        f'class="ledger-card-budget" aria-label="Edit card budget for week ending {day.isoformat()}" '
                        f'title="Edit this week’s budget; remaining is included in the checking projection">'
                        f'Budget: ${values["budget"]:,.2f} · Remaining: ${values["remaining"]:,.2f}</button>')
                card_mark = ' ✓' if values['completed'] else ''
                content.append(f'<div class="ledger-card-spent" title="Card spent">${values["spent"]:,.2f}{card_mark}</div>')
                if values['spent'] > values['budget'] and 'budget:' + day.isoformat() in settings:
                    content.append(f'<div class="ledger-note">Over budget: ${values["spent"]-values["budget"]:,.2f}</div>')
            cells.append(
                f'<div class="ledger-day"><header class="ledger-header{warning}">'
                f'<button type="button" data-day="{day.isoformat()}" '
                f'class="ledger-date ledger-date-{markers.get(day.isoformat(), "clear")}" '
                f'aria-label="Mark {day.isoformat()}: {markers.get(day.isoformat(), "clear")}">{day.day}</button>'
                f'<span class="ledger-balance">${values["balance"]:,.2f}</span>'
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
    .ledger-day header.ledger-header-low {background:#ffed9e;border-top:5px solid #d5a500;color:#302500;}
    .ledger-day header.ledger-header-negative {background:#ffd4d4;border-top:5px solid #c12626;color:#510f0f;}
    .ledger-date {display:inline-flex;align-items:center;justify-content:center;min-width:30px;min-height:30px;padding:2px 5px;box-sizing:border-box;border:1px solid #8a96a5;border-radius:3px;background:rgba(127,150,180,.2);font-weight:700;}
    button.ledger-date {color:inherit;cursor:pointer;font:inherit;font-weight:700;}
    .ledger-date-green {background:#36b75e!important;color:#092b13!important;border-color:#287e42!important;}
    .ledger-date-yellow {background:#f7d75c!important;color:#392c00!important;border-color:#b79622!important;}
    .ledger-balance {font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;}
    .ledger-body {padding:8px;flex:1;overflow-wrap:anywhere;}
    .ledger-day footer {padding:7px;border-top:1px solid #8a96a5;background:rgba(127,150,180,.12);font-size:.85rem;font-variant-numeric:tabular-nums;}
    .ledger-card-spent {background:#00b4e6;color:#002b36;padding:6px;border-radius:3px;font-weight:600;margin-top:6px;}
    .ledger-card-budget {display:block;width:100%;border:0;background:transparent;color:#d14343;text-align:left;padding:3px 0;font:inherit;cursor:pointer;text-decoration:underline;}
    .ledger-card-budget:focus-visible {outline:2px solid #1370c3;outline-offset:2px;}
    .ledger-note {font-size:.85rem;padding:4px 0;}
    .ledger-outside {opacity:.55;}
    </style>'''
    names = ''.join('<div>'+name+'</div>' for name in ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'])
    return style + '<div class="ledger-calendar-scroll"><div class="ledger-calendar"><div class="ledger-weekdays">' + names + '</div><div class="ledger-days">' + ''.join(cells) + '</div></div></div>'


def render_editable_calendar():
    st.html('''<style>
        .st-key-calendar_heading h1 {font-size:2rem;padding:0;margin:0;}
        .st-key-calendar_heading [data-testid="stVerticalBlock"] {gap:.4rem;}
        .ledger-calendar-summary {display:flex;align-items:center;gap:2rem;flex-wrap:wrap;margin:0 0 .25rem;}
        .ledger-calendar-summary h2 {font-size:1.4rem;margin:0;padding:0;}
        .ledger-calendar-summary .summary-label {font-size:.85rem;opacity:.8;}
        .ledger-calendar-summary .summary-value {font-size:1.3rem;font-weight:600;font-variant-numeric:tabular-nums;}
    </style>''')
    with st.container(key='calendar_heading'):
        st.title(cash_accounts()[active_cash_id()]['name'] + ': Cash Flow Calendar')
        summary_slot = st.empty()
        first = month_year_picker('Calendar', 'calendar')
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    summary_slot.html('<div class="ledger-calendar-summary"><h2>' + first.strftime('%B %Y') + '</h2></div>')
    try:
        settings = load_calendar_settings()
        opening_dates = [date.fromisoformat(key.split(':', 1)[1]) for key in settings
                         if key.startswith('opening:') and key.split(':', 1)[1] <= first.isoformat()]
        if opening_dates:
            ensure_unified_budget_months(max(opening_dates), first)
        all_rows = get_all_transactions_cached()
        transactions = [dict(r,credit_month_spent=str(credit_month_spent(all_rows,r['credit_account_id'],r['credit_budget_month']))) if r.get('credit_budget_month') else r for r in get_transactions_cached()]
        reconciled = load_card_weeks()
    except Exception as exc:
        logger.exception('Unable to load editable calendar.')
        st.error('The calendar could not be loaded. A scheduled entry or database request failed.')
        with st.expander('Error details'):
            st.code(str(getattr(exc, 'message', None) or exc))
        return

    unclassified = [row for row in transactions if row.get('direction') not in ('Income', 'Expense')]
    if unclassified:
        st.warning(f"{len(unclassified)} existing transactions need Income/Expense classification. Use Edit Transaction in the sidebar to review and save each one.")
        st.dataframe(pd.DataFrame(unclassified), hide_index=True, use_container_width=True)

    opening_key = 'opening:' + first.isoformat()
    anchors = [key for key in settings if key.startswith('opening:')
               and key.split(':', 1)[1] <= first.isoformat()]
    calendar_warning_slot = st.empty()
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
        checking_ref = 'c:' + str(active_cash_id())
        pending_savings = [r for r in load_budget_table('LedgerUnifiedBudgetOccurrences')
            if r['transaction_type'] == 'Transfer' and not r['cancelled']
            and r.get('transfer_id') is None
            and (str(r['paid_from']).startswith('s:') or str(r['paid_to']).startswith('s:'))
            and checking_ref in (r['paid_from'], r['paid_to'])]
        for occurrence in pending_savings:
            planned.append(dict(id='pending-transfer-' + str(occurrence['id']),
                pending_transfer_id=occurrence['id'], name=occurrence['name'],
                due_date=occurrence['actual_date'], amount=occurrence['amount'],
                enabled=True, transaction_id=None,
                direction='Expense' if occurrence['paid_from'] == checking_ref else 'Income',
                description=occurrence.get('description') or 'Budgeted savings transfer'))
        planned += manual_savings_plans_for_checking(
            load_budget_table('LedgerManualSavingsPlans'), active_cash_id())
        balances, anchor = calendar_balances(transactions, settings, first, last,
                                             reconciled=reconciled, planned=planned)
    except Exception:
        logger.exception('Budget calendar load failed.')
        st.error('The calendar could not calculate. Apply budget_setup.sql and check the connection and transaction classifications.')
        return

    if reconciled:
        next_week = date.fromisoformat(max(reconciled)) + timedelta(days=7)
        st.info(f'New card entries apply to the budget week ending {next_week:%b %d, %Y}, regardless of transaction date.')
    direct = {}
    transfer_completion = ({int(t['id']): t.get('completed_at')
        for t in load_budget_table('LedgerTransfers')}
        if any(row.get('transfer_id') is not None for row in transactions) else {})
    for row in transactions:
        if row.get('type') in ('Direct', 'Check', 'Savings Transfer', 'Federal student loan') or (
                row.get('type') == 'Line of credit' and row.get('credit_action') == 'Payment'):
            display_row = dict(row)
            if row.get('transfer_id') is not None:
                display_row['completed_at'] = transfer_completion.get(int(row['transfer_id']))
            direct.setdefault(str(row['date'])[:10], []).append(display_row)
    for item in planned:
        if item['enabled'] and item.get('transaction_id') is None:
            direct.setdefault(item['due_date'], []).append({
                'id': 'planned-' + str(item['id']), 'amount': item['amount'],
                'direction': item['direction'], 'merchant': item['name'],
                'description': 'Planned: ' + (item.get('description') or item['name']),
                'planned': True, 'budget_item_id': None if item.get('payday_id') or item.get('transfer_schedule_id') or item.get('pending_transfer_id') else item['id'],
                'payday_id': item.get('payday_id'),
                'pending_transfer_id': item.get('pending_transfer_id'),
            })
    unset = sum(1 for r in paydays if r['enabled'] and r['amount'] is None and r.get('transaction_id') is None
                and anchor.isoformat() <= r['due_date'] <= last.isoformat())
    if unset:
        calendar_warning_slot.warning(
            f'{unset} payday amounts are unset in the balance period and excluded from projections. Enter them in Budget → Payday income.')
    # Render as HTML directly: Markdown interprets dollar amounts as math and
    # can break markup around multiline tooltip attributes.
    render_check_calendar(first, balances, direct, settings)
    monthly_surplus = sum((values['surplus'] for values in balances.values()), Decimal(0))
    surplus_html = (f'<div title="Includes completed weeks whose Saturday falls in this month">'
        f'<div class="summary-label">Monthly budget surplus</div>'
        f'<div class="summary-value">${monthly_surplus:,.2f}</div></div>' if active_cash_id() == 1 else '')
    summary_slot.html(f'<div class="ledger-calendar-summary"><h2>{first:%B %Y}</h2>'
        f'<div><div class="summary-label">Projected month-end balance</div>'
        f'<div class="summary-value">${balances[last]["balance"]:,.2f}</div></div>{surplus_html}</div>')
    if active_cash_id() == 1:
        st.caption('Includes completed weeks whose Saturday falls in this month. '
                   'Over-budget weeks show zero surplus and are flagged above. '
                   'Reconciled payments and budget surplus are preserved from the saved reconciliation.')
    with st.expander('Transaction Register', expanded=False):
        st.dataframe(pd.DataFrame([{'Date':t['date'],'Type':'Transfer' if t.get('transfer_id') or t['type']=='Savings Transfer' else t['type'],
            'Direction':t['direction'],'Amount':float(money(t['amount'])),'Merchant':t.get('merchant',''),
            'Category':'Transfer' if t.get('transfer_id') or t['type']=='Savings Transfer' else t.get('category',''),
            'Description':t.get('description','')} for t in transactions
            if not (t.get('type') == 'Line of credit' and t.get('credit_action') == 'Charge')]),
            use_container_width=True, hide_index=True)


# ============================================================
# BUDGET DEFAULTS AND MONTHLY PLANS
# ============================================================

def load_budget_table(table):
    rows, offset = [], 0
    order = ('month' if table == 'LedgerBudgetMonths' else
             'name' if table == 'LedgerRetiredCategories' else
             'week_number' if table == 'LedgerBillPaydayWeeks' else
             'stream' if table == 'LedgerPaydayDefaults' else 'id')
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
    checking = {'c:' + str(i): row['name'] for i, row in cash_accounts().items()
        if not row.get('archived_at')}
    savings = {'s:' + str(i): row['name'] for i, row in savings_accounts().items()
        if not row.get('archived_at')}
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


def budget_rule_effective_date(selected_month, today, existing_enabled,
                               transaction_type, enabled, latest_card_week=None):
    effective = max(selected_month, today + timedelta(days=1 if existing_enabled else 0))
    if transaction_type == 'AMZ Card' and enabled and latest_card_week:
        effective = max(effective, latest_card_week + timedelta(days=1))
    return effective


@st.dialog('Budget item', width='large')
def unified_budget_item_dialog(rule, selected_month):
    require_session()
    accounts = unified_account_options()
    generation = str(st.session_state.get('unified_budget_generation', 0))
    identity = str(rule['id']) if rule else 'new'
    types = ['AMZ Card', 'Direct', 'Check', 'Transfer', 'Line of credit', 'Federal student loan']
    current_type = rule['transaction_type'] if rule else 'Direct'
    transaction_type = st.selectbox('Transaction type', types,
        index=types.index(current_type), key='budget_type_' + identity + '_' + generation)
    source_default = rule['paid_from'] if rule else 'c:1'
    if transaction_type == 'AMZ Card':
        paid_from = 'c:1'
        st.text_input('Paid from', value=accounts['c:1'], disabled=True)
    else:
        available_sources = ([key for key in accounts if key.startswith('c:')]
            if transaction_type in ('Line of credit', 'Federal student loan') else list(accounts))
        paid_from = st.selectbox('Paid from', available_sources,
            index=available_sources.index(source_default) if source_default in available_sources else 0,
            format_func=accounts.get, key='budget_source_' + identity + '_' + generation)
    schedules = ['Monthly'] if transaction_type in ('Line of credit', 'Federal student loan') else ['Monthly', 'Weekly', 'As needed']
    current_schedule = rule['schedule'] if rule else 'Monthly'
    schedule = st.selectbox('Schedule', schedules,
        index=schedules.index(current_schedule) if current_schedule in schedules else 0,
        key='budget_schedule_' + identity + '_' + generation)
    credit_account_id = None
    federal_loan_id = None
    if transaction_type == 'Transfer':
        destinations = [key for key in accounts if key != paid_from]
        if not destinations:
            st.error('Create another account before adding a transfer.')
            return
        current_target = rule['paid_to'] if rule else destinations[0]
        paid_to = st.selectbox('Paid to', destinations,
            index=destinations.index(current_target) if current_target in destinations else 0,
            format_func=accounts.get)
        category = 'Transfer'
        available_buckets = load_budget_table('LedgerSavingsBuckets')
        def choose_transfer_bucket(ref, side):
            if not ref.startswith('s:'):
                return None
            account_id = int(ref[2:])
            choices = {int(b['id']): b for b in available_buckets
                       if int(b['savings_account_id']) == account_id
                       and b['active'] and b['balance'] is not None}
            if not choices:
                st.error('Establish an active savings category balance before budgeting this transfer.')
                return None
            current = rule.get(side + '_savings_bucket_id') if rule else None
            current = int(current) if current is not None else None
            general = next((i for i, b in choices.items() if b['is_general']), None)
            selected = current if current in choices else general
            keys = list(choices)
            return st.selectbox(side.capitalize() + ' savings category', keys,
                index=keys.index(selected) if selected in keys else 0,
                format_func=lambda i: choices[i]['name'],
                key='budget_' + side + '_bucket_' + identity + '_' + generation + '_' + ref)
        source_savings_bucket_id = choose_transfer_bucket(paid_from, 'source')
        destination_savings_bucket_id = choose_transfer_bucket(paid_to, 'destination')
    else:
        source_savings_bucket_id = None
        destination_savings_bucket_id = None
        if transaction_type == 'Federal student loan':
            loans = {int(a['id']): a for a in load_budget_table('LedgerFederalLoans')}
            if not loans:
                st.error('Add the student loan on Lines of Credit before budgeting its payment.')
                return
            choices = list(loans)
            current_loan = int(rule['federal_loan_id']) if rule and rule.get('federal_loan_id') else None
            federal_loan_id = st.selectbox('Student loan to pay', choices,
                index=choices.index(current_loan) if current_loan in choices else 0,
                format_func=lambda i: loans[i]['name'])
            paid_to = loans[federal_loan_id]['name']
            st.caption('This monthly payment appears in checking on its scheduled date. The loan changes only when marked complete.')
        elif transaction_type == 'Line of credit':
            credit_accounts = {a['id']:a for a in load_budget_table('LedgerCreditAccounts')
                if a['active'] or (rule and a['id'] == rule.get('credit_account_id'))}
            if not credit_accounts:
                st.error('Add a credit account on Lines of Credit before budgeting a payment.')
                return
            choices = sorted(credit_accounts, key=lambda i: (credit_accounts[i]['name'].casefold(), str(i)))
            current_credit = rule.get('credit_account_id') if rule else None
            credit_account_id = st.selectbox('Credit account to pay',choices,
                index=choices.index(current_credit) if current_credit in choices else 0,
                format_func=lambda i: credit_accounts[i]['name'])
            paid_to = credit_accounts[credit_account_id]['name']
            st.caption('This monthly payment reduces checking on its scheduled date and credit owed only when marked complete.')
        else:
            merchants = get_existing_merchants()
            current_target = (rule['name'] if rule and rule['paid_to'] == 'Merchant'
                              else rule['paid_to'] if rule else None)
            if current_target and current_target.casefold() not in {m.casefold() for m in merchants}:
                merchants = sorted(merchants + [current_target], key=str.casefold)
            paid_to = st.selectbox('Paid to (merchant)', merchants, index=merchants.index(current_target)
                if current_target in merchants else None, placeholder='Select or enter a merchant',
                accept_new_options=True)
            st.caption('Select a saved merchant or type a new merchant and press Enter.')
        categories = get_existing_categories()
        current_category = str(rule.get('category') or 'Budget') if rule else None
        if current_category and current_category not in categories:
            categories = sorted(categories + [current_category], key=str.casefold)
        category = st.selectbox('Category', categories,
            index=categories.index(current_category) if current_category in categories else None,
            placeholder='Select or enter a category', accept_new_options=True,
            key='budget_category_' + identity + '_' + generation)
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
        enabled = st.checkbox('Include', value=bool(rule['enabled']) if rule else schedule in ('Monthly', 'Weekly'),
            key='budget_include_' + identity + '_' + generation + '_' + schedule)
        if rule:
            st.caption('Changes to this recurring item begin with future occurrences. Earlier entries stay as recorded.')
        save = st.form_submit_button('Save budget item', type='primary')
    if save:
        if amount is None:
            st.error('Enter an amount.')
            return
        if not paid_to or not str(paid_to).strip():
            st.error('Choose or enter a merchant.' if transaction_type != 'Transfer' else 'Choose a destination account.')
            return
        if not category or not str(category).strip():
            st.error('Choose or enter a category.')
            return
        today = datetime.now(LOCAL_TZ).date()
        latest_card_week = None
        if transaction_type == 'AMZ Card' and enabled:
            try:
                response = conn.table('LedgerCardWeeks').select('week_ending').order(
                    'week_ending', desc=True).range(0, 0).execute()
                if response.data:
                    latest_card_week = date.fromisoformat(response.data[0]['week_ending'])
            except Exception:
                st.error('Card-week status could not be checked. Nothing was saved.')
                return
        effective = budget_rule_effective_date(selected_month, today,
            bool(rule and rule['enabled']), transaction_type, enabled, latest_card_week)
        details = dict(name=name.strip(),amount=str(money(amount)),description=description,
            transaction_type=transaction_type,paid_from=paid_from,paid_to=str(paid_to).strip(),
            schedule=schedule,day_of_month=day_of_month,weekday=weekday,enabled=enabled,
            category=str(category).strip(),
            source_savings_bucket_id=source_savings_bucket_id,
            destination_savings_bucket_id=destination_savings_bucket_id,
            credit_account_id=credit_account_id,
            federal_loan_id=federal_loan_id)
        if transaction_type == 'Transfer' and ((paid_from.startswith('s:') and source_savings_bucket_id is None)
                or (paid_to.startswith('s:') and destination_savings_bucket_id is None)):
            st.error('Choose a category for each savings account in the transfer.')
            return
        save_unified_budget_action('ledger_save_unified_budget_rule_with_savings', dict(
            p_id=rule['id'] if rule else None,
            p_revision=rule['revision'] if rule else None,
            p_effective_from=effective.isoformat(),p_data=details))
    if rule:
        confirm_key = 'delete_budget_rule_' + identity + '_' + generation
        if st.button('Delete recurring budget item', key=confirm_key + '_review'):
            st.session_state[confirm_key] = True
        if st.session_state.get(confirm_key):
            st.warning('This removes the budget row and future entries you have not individually edited. '
                       'Earlier and individually edited entries remain recorded.')
            if st.button('Confirm delete budget item', key=confirm_key + '_confirm'):
                save_unified_budget_action('ledger_delete_unified_budget_rule', dict(
                    p_id=rule['id'], p_revision=rule['revision']))


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


@st.dialog('Delete category', width='large')
def delete_category_dialog(category_name):
    """Preview every checking transaction and budget rule before retiring a category."""
    require_session()
    category_key = category_name.casefold()
    transactions = [row for row in get_all_transactions_cached()
                    if str(row.get('category') or '').strip().casefold() == category_key]
    rules = [row for row in load_budget_table('LedgerUnifiedBudgetRules')
             if str(row.get('category') or '').strip().casefold() == category_key]
    accounts = cash_accounts()
    entries = []
    for row in transactions:
        entries.append({'Kind': 'Transaction', 'Account': accounts.get(int(row.get('cash_account_id') or 1), {}).get('name', 'Unavailable'),
                        'Date': str(row['date']), 'Item / merchant': str(row.get('merchant') or ''),
                        'Amount': float(money(row['amount'])), 'Replacement category': None})
    for row in rules:
        entries.append({'Kind': 'Budget item', 'Account': unified_account_options().get(row['paid_from'], 'Unavailable'),
                        'Date': str(row['effective_from']), 'Item / merchant': str(row['name']),
                        'Amount': float(money(row['amount'])), 'Replacement category': None})
    st.write(f'Deleting “{category_name}” will update all checking accounts. Choose a replacement for every listed entry.')
    replacement_name = st.text_input('Add a replacement category (optional)',
        key='category_new_replacement_' + category_key)
    options = [name for name in get_existing_categories() if name.casefold() != category_key]
    if replacement_name.strip() and replacement_name.strip().casefold() not in {name.casefold() for name in options}:
        options.append(replacement_name.strip())
    options.sort(key=str.casefold)
    if entries:
        edited = st.data_editor(pd.DataFrame(entries), hide_index=True, use_container_width=True,
            disabled=['Kind', 'Account', 'Date', 'Item / merchant', 'Amount'],
            column_config={'Amount': st.column_config.NumberColumn(format='$%.2f'),
                           'Replacement category': st.column_config.SelectboxColumn(
                               'Replacement category', options=options, required=False)},
            key='category_reassignment_' + category_key)
    else:
        edited = pd.DataFrame(entries)
        st.info('No transactions or budget items currently use this category.')
    if st.button('Delete category and save reassignment', type='primary'):
        replacements = edited['Replacement category'].tolist() if entries else []
        if any(not isinstance(name, str) or not name.strip() or
               name.strip().casefold() == category_key for name in replacements):
            st.error('Choose a different replacement category for every listed entry.')
            return
        transaction_changes = [dict(id=row['id'], revision=row['check_revision'],
                                    category=replacements[index].strip())
                               for index, row in enumerate(transactions)]
        rule_changes = [dict(id=row['id'], revision=row['revision'],
                             category=replacements[len(transactions) + index].strip())
                        for index, row in enumerate(rules)]
        save_unified_budget_action('ledger_delete_transaction_category', dict(
            p_name=category_name, p_transactions=transaction_changes, p_rules=rule_changes))


def render_manage_categories_page():
    require_session()
    st.title('Transaction Categories')
    categories = [name for name in get_existing_categories()
                  if name.casefold() not in ('transfer', 'savings transfer')]
    if not categories:
        st.info('There are no editable categories.')
        return
    selected = st.selectbox('Category to delete', categories)
    if st.button('Review category deletion'):
        delete_category_dialog(selected)


def visible_budget_versions(rules, selected_month):
    """Show one dated version per budget item in the selected month."""
    month_end = selected_month.replace(day=monthrange(selected_month.year, selected_month.month)[1])
    current_by_series = {}
    for rule in rules:
        if rule.get('deleted_at'):
            continue
        if rule['effective_from'] > month_end.isoformat() or (
            rule['effective_until'] is not None and rule['effective_until'] < selected_month.isoformat()):
            continue
        series = rule['series_id']
        if series not in current_by_series or (rule['effective_from'], int(rule['id'])) > (
            current_by_series[series]['effective_from'], int(current_by_series[series]['id'])):
            current_by_series[series] = rule
    return sorted(current_by_series.values(), key=lambda row: (row['name'].casefold(), int(row['id'])))


def visible_current_budget_versions(rules):
    """Show the one current recurring rule per item, independent of a review month."""
    current_by_series = {}
    for rule in rules:
        if rule.get('deleted_at') or rule.get('effective_until') is not None:
            continue
        series = rule['series_id']
        if series not in current_by_series or (rule['effective_from'], int(rule['id'])) > (
                current_by_series[series]['effective_from'], int(current_by_series[series]['id'])):
            current_by_series[series] = rule
    return sorted(current_by_series.values(), key=lambda row: (row['name'].casefold(), int(row['id'])))


def render_budget_page():
    require_session()
    st.title('View / Edit Budget')
    selected_month = datetime.now(LOCAL_TZ).date().replace(day=1)
    try:
        response = conn.rpc('ledger_prepare_unified_budget_month',
            {'p_month': selected_month.isoformat()}).execute()
        if response.data is not True:
            raise RuntimeError('The budget month was not prepared.')
        rules = load_budget_table('LedgerUnifiedBudgetRules')
        occurrences = load_budget_table('LedgerUnifiedBudgetOccurrences')
        accounts = unified_account_options()
    except Exception as exc:
        logger.exception('Unified budget load failed.')
        st.error('The shared budget could not be loaded. A scheduled entry or database request failed.')
        with st.expander('Error details'):
            st.code(str(getattr(exc, 'message', None) or exc))
        return
    visible = visible_current_budget_versions(rules)
    st.subheader('Budgeted bills and income')
    inclusion = st.selectbox('Show budget items', ['Both', 'Included only', 'Excluded only'],
        key='budget_inclusion_filter')
    visible = filter_budget_inclusion(visible, inclusion)
    credit_names = {a['id']: a['name'] for a in load_budget_table('LedgerCreditAccounts')}
    loan_names = {int(a['id']): a['name'] for a in load_budget_table('LedgerFederalLoans')}
    rows = []
    for rule in visible:
        day = (['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][int(rule['weekday'])]
               if rule['schedule'] == 'Weekly' else str(rule['day_of_month']))
        rows.append({'ID#': int(rule['id']), 'Item': rule['name'],
            'Transaction type': rule['transaction_type'],
            'Category': rule.get('category') or 'Budget',
            'Paid from': accounts.get(rule['paid_from'], 'Unavailable account'),
            'Paid to': credit_names.get(rule.get('credit_account_id'),
                loan_names.get(int(rule['federal_loan_id']) if rule.get('federal_loan_id') else None,
                    accounts.get(rule['paid_to'], rule['paid_to']))),
            'Amount': float(money(rule['amount'])), 'Day': day,
            'Schedule': rule['schedule'], 'Include': bool(rule['enabled']),
            'Description': rule['description'],
             'Apply change': 'Future occurrences',
             'Status': 'Current' if rule['enabled'] else 'Excluded'})
    st.caption('These recurring items apply in every month while Include is checked. Payday income is managed below. Click an ID# to edit an item.')
    frame = pd.DataFrame(rows, columns=['ID#','Item','Transaction type','Category','Paid from','Paid to',
        'Amount','Day','Schedule','Include','Description','Apply change','Status'])
    event = st.dataframe(frame, hide_index=True, use_container_width=True,
        key='unified_budget_table_' + inclusion + '_' + str(st.session_state.get('unified_budget_generation', 0)),
        on_select='rerun', selection_mode='single-cell',
        column_config={'Amount': st.column_config.NumberColumn(format='$%.2f')})
    cells = event.selection.cells
    selected_index = cells[0][0] if cells and cells[0][1] == 'ID#' else None
    selection_token = (inclusion, visible[selected_index]['id']) if selected_index is not None and 0 <= selected_index < len(visible) else None
    if selection_token is None:
        st.session_state.pop('unified_budget_handled_selection', None)
    add_item = st.button('Add budget item', type='primary')
    if add_item:
        unified_budget_item_dialog(None, selected_month)
    elif selection_token is not None:
        if st.session_state.get('unified_budget_handled_selection') != selection_token:
            st.session_state['unified_budget_handled_selection'] = selection_token
            unified_budget_item_dialog(visible[selected_index], selected_month)
    savings_entries = [o for o in occurrences if not o['cancelled']
        and o['paid_from'].startswith('s:') and o['transaction_type'] != 'Transfer']
    if savings_entries:
        with st.expander('Individual expenses paid from savings'):
            selected = st.selectbox('Savings entry',
                sorted(savings_entries, key=lambda o: (o['actual_date'], int(o['id'])), reverse=True),
                format_func=lambda o: o['actual_date'] + ' · ' + o['name'] + ' · $' + str(o['amount']))
            if st.button('Edit selected savings entry'):
                unified_savings_occurrence_dialog(selected)
    transfer_entries = [o for o in occurrences if not o['cancelled']
        and o['transfer_id'] is not None]
    if transfer_entries:
        with st.expander('Individual budget transfers'):
            selected = st.selectbox('Transfer entry',
                sorted(transfer_entries, key=lambda o: (o['actual_date'], int(o['id'])), reverse=True),
                format_func=lambda o: o['actual_date'] + ' · ' + o['name'] + ' · $' + str(o['amount']))
            linked = next((t for t in load_budget_table('LedgerTransfers')
                if int(t['id']) == int(selected['transfer_id'])), None)
            if linked:
                transfer_form(linked)
    st.divider()
    previous_account = active_cash_id()
    st.session_state['cash_account_id'] = 1
    try:
        with st.expander('Payday income', expanded=False):
            render_payday_tables()
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


def filter_budget_inclusion(rules, selection):
    if selection == 'Included only':
        return [r for r in rules if r['enabled']]
    if selection == 'Excluded only':
        return [r for r in rules if not r['enabled']]
    return list(rules)


def manual_savings_plans_for_checking(plans, account_id):
    return [dict(id='manual-transfer-' + str(p['id']),
        pending_transfer_id='manual-' + str(p['id']), name='Account transfer',
        due_date=p['date'], amount=p['amount'], enabled=True, transaction_id=None,
        direction='Expense' if p.get('source_account') == account_id else 'Income',
        description=p.get('description') or 'Planned savings transfer')
        for p in plans if not p['cancelled'] and p.get('transfer_id') is None
        and account_id in (p.get('source_account'), p.get('destination_account'))]


@st.dialog('Planned savings transfer', width='medium')
def manual_savings_transfer_dialog(plan_id):
    require_session()
    plan = next((p for p in load_budget_table('LedgerManualSavingsPlans')
        if int(p['id']) == plan_id), None)
    if not plan or plan['cancelled'] or plan.get('transfer_id') is not None:
        st.error('This transfer changed. Reload the calendar.')
        return
    transfer_form(plan, pending=True)


def render_weekly_savings_deposit():
    config = next((r for r in load_budget_table('LedgerSavingsWeeklyDeposit')
        if int(r['account_id']) == 1), None)
    today = datetime.now(LOCAL_TZ).date()
    with st.expander('General savings weekly deposit', expanded=False):
        st.caption('External deposit into General savings every Friday. Checking is unchanged. '
                   'Due deposits are recorded once when you open the ledger, including missed Fridays since the start date. '
                   'Amount changes apply to future deposits; recorded deposits keep their original amounts.')
        with st.form('weekly_savings_deposit_' + str(config['revision'] if config else 'new')):
            amount = st.number_input('Weekly deposit amount ($)', min_value=0.01,
                max_value=999999999.99, value=float(money(config['amount'])) if config else 62.0, format='%.2f')
            enabled = st.checkbox('Automatically record Friday deposits', value=bool(config and config['enabled']))
            if config:
                start = date.fromisoformat(config['start_date'])
                st.caption('Starts ' + start.strftime('%B %d, %Y') + '. Resuming starts with upcoming Fridays, without filling paused dates.')
            else:
                start = st.date_input('Start date', value=today, min_value=today)
            saved = st.form_submit_button('Save weekly deposit')
        if saved:
            account_action('ledger_save_savings_weekly_deposit', dict(
                p_revision=config['revision'] if config else None,
                p_amount=str(money(amount)), p_start=start.isoformat(), p_enabled=enabled))


def process_due_savings_deposits():
    try:
        response = conn.rpc('ledger_post_due_savings_deposits', {}).execute()
        if isinstance(response.data, bool) or not isinstance(response.data, int) or response.data < 0:
            raise ValueError('Deposit processing was not confirmed.')
        if response.data:
            clear_transaction_caches()
            st.session_state.pop('savings_snapshot', None)
            st.session_state['savings_generation'] = st.session_state.get('savings_generation', 0) + 1
    except Exception as exc:
        st.warning('Friday savings deposits could not be checked. Install savings_friday_update.sql, then reload. ' +
            str(getattr(exc, 'message', None) or exc))


def render_savings_page():
    require_session()
    st.title(savings_accounts()[active_savings_id()]['name'])
    st.caption('General savings is unearmarked money; named categories are portions of the same account. '
               'Pending savings transfers are excluded until marked made. This is a ledger, not live bank synchronization.')
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
    if active_savings_id() == 1:
        render_weekly_savings_deposit()
    manual_pending = [p for p in load_budget_table('LedgerManualSavingsPlans')
        if not p['cancelled'] and p.get('transfer_id') is None
        and (p.get('source_bucket') in by_id or p.get('destination_bucket') in by_id)]
    if manual_pending:
        with st.expander('Pending savings transfers'):
            selected_plan = st.selectbox('Planned transfer', manual_pending,
                format_func=lambda p: p['date'] + ' · $' + str(p['amount']) + ' · ' + (p['description'] or 'Account transfer'))
            if st.button('Review planned transfer'):
                manual_savings_transfer_dialog(int(selected_plan['id']))
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
            'Deposit date': (r.get('details') or {}).get('deposit_date'),
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




def credit_month_spent(rows, account_id, month):
    return sum((money(r['amount']) for r in rows if r.get('credit_account_id')==account_id
        and r.get('credit_action')=='Charge' and r.get('credit_spending_month')==str(month)),Decimal(0))


CREDIT_TABLE_JS = r"""
export default function({parentElement,data,setTriggerValue}) {
    const root=parentElement.querySelector('.credit-account-table');
    root.innerHTML=data.html;
    root.onclick=e=>{
        const button=e.target.closest('button[data-debt-account]');
        if(button && root.contains(button)) setTriggerValue('action',{account:button.dataset.debtAccount});
    };
    root.querySelectorAll('input[data-whatif]').forEach(input=>{
        const original=input.value;
        const commit=()=>{
            if(input.value!==original)
                setTriggerValue('action',{whatif:input.dataset.whatif,value:input.value});
        };
        input.addEventListener('change',commit);
        input.addEventListener('blur',commit);
        input.addEventListener('keydown',e=>{if(e.key==='Enter') commit();});
    });
}
"""
credit_table_component=component('ledger_credit_account_table_'+sha256(CREDIT_TABLE_JS.encode('utf-8')).hexdigest()[:16],html='<div class="credit-account-table"></div>',js=CREDIT_TABLE_JS)


def credit_account_table_html(rows, selected=None):
    columns=['Account','Current balance','APR %','What-if monthly payment','Estimated time to pay off',
        'Type','Status','Credit limit','Current usage %','Remaining credit','Payment to target use']
    parts=['<style>.credit-account-table{overflow-x:auto}.credit-account-table table{border-collapse:collapse;width:100%;font:inherit} .credit-account-table td,.credit-account-table th{border:1px solid #899795;padding:8px;text-align:left;white-space:nowrap}.credit-account-table button{border:0;background:transparent;color:#1670c5;text-decoration:underline;cursor:pointer;font:inherit;padding:0}.credit-account-table input{width:120px;font:inherit;padding:5px;color:inherit;background:transparent;border:1px solid #899795;border-radius:4px}.credit-account-table tr.selected{background:rgba(42,131,196,.12)}</style><table><thead><tr>']
    parts += ['<th>'+escape(c)+'</th>' for c in columns]
    parts.append('</tr></thead><tbody>')
    for row in rows:
        key=escape(row['_key'],quote=True)
        parts.append('<tr'+(' class="selected"' if selected==row['_key'] else '')+'>')
        for c in columns:
            value=row.get(c)
            missing=value is None or (isinstance(value,float) and math.isnan(value))
            if c=='Account':
                cell=f'<button type="button" data-debt-account="{key}" aria-label="{escape("Show entries for "+str(value),quote=True)}">{escape(str(value))}</button>'
            elif c=='What-if monthly payment':
                val='' if missing else str(money(value))
                cell=f'<input type="number" min="0" max="999999999.99" step="0.01" data-whatif="{key}" value="{val}" aria-label="{escape("What-if monthly payment for "+row["Account"],quote=True)}">'
            elif missing: cell='—'
            elif c in ('APR %','Current usage %'): cell=f'{money(value):,.2f}%'
            elif c in ('Current balance','Credit limit','Remaining credit','Payment to target use'): cell=f'${money(value):,.2f}'
            else: cell=escape(str(value))
            parts.append('<td>'+cell+'</td>')
        parts.append('</tr>')
    parts.append('</tbody></table>')
    return ''.join(parts)


def render_credit_account_table(rows, whatif_values):
    event=credit_table_component(data={'html':credit_account_table_html(rows,st.session_state.get('debt_account_filter'))},key='credit_account_table',on_action_change=lambda:None)
    action=event.action
    keys={r['_key'] for r in rows}
    if action and action.get('account') in keys:
        st.session_state['debt_account_filter']=action['account']
        st.session_state.pop('credit_edit_id',None)
        st.rerun()
    elif action and action.get('whatif') in keys:
        try:
            raw=action.get('value','').strip()
            amount=None if not raw else money(raw)
            if amount is not None and (not amount.is_finite() or amount<0 or amount>=Decimal('1000000000')):
                raise ValueError('Enter a nonnegative monthly payment.')
            updated=dict(whatif_values)
            if amount is None: updated.pop(action['whatif'],None)
            else: updated[action['whatif']]=float(amount)
            if updated!=whatif_values:
                st.session_state['credit_whatif_payments']=updated
                st.rerun()
        except (ValueError,InvalidOperation) as exc:
            st.error(str(exc))

def render_credit_page():
    require_session()
    st.title('Lines of Credit')
    st.caption('Balances continue from one dated charge or payment to the next. Older monthly worksheets are available below as history.')
    if st.button('Reload credit accounts / discard unsaved changes'):
        st.session_state.pop('credit_snapshot', None)
        st.session_state['credit_generation'] = st.session_state.get('credit_generation', 0) + 1
        st.rerun()
    try:
        snapshot = st.session_state.get('credit_snapshot')
        if not snapshot:
            accounts = load_budget_table('LedgerCreditAccounts')
            settings = load_budget_table('LedgerCreditSettings')
            if len(settings) != 1:
                raise RuntimeError('Credit settings are missing.')
            snapshot = dict(accounts=accounts,settings=settings[0])
            st.session_state['credit_snapshot'] = snapshot
        accounts, setting = snapshot['accounts'], snapshot['settings']
        entries = [r for r in get_all_transactions_cached()
                   if r.get('type') in ('Line of credit', 'Federal student loan')]
    except Exception:
        st.error('Lines of Credit could not be loaded. Install the ongoing credit update, then reload.')
        return
    generation = str(st.session_state.get('credit_generation', 0))
    target = money(setting['target_percent'])
    with st.expander('Target use settings'):
        with st.form('credit_target_' + generation):
            percent = st.number_input('Target credit use (%)', min_value=0.0,
                max_value=100.0, value=float(target), step=1.0, format='%.2f')
            save_target = st.form_submit_button('Save target percentage')
        if save_target:
            credit_action('ledger_save_credit_target',
                dict(p_revision=setting['revision'], p_target=str(money(percent))))
    with st.expander('Add or manage a credit account', expanded=not accounts):
        with st.form('credit_add_' + generation):
            new_name = st.text_input('Account name')
            new_kind = st.selectbox('Account type',
                ['Credit Card', 'Line of Credit', 'Lender', 'Loan'])
            add = st.form_submit_button('Add account')
        if add:
            try:
                response = conn.rpc('ledger_create_credit_account',
                    {'p_name': new_name, 'p_type': new_kind}).execute()
                if not response.data:
                    raise RuntimeError('Account creation was not confirmed.')
                st.session_state.pop('credit_snapshot', None)
                st.rerun()
            except Exception as exc:
                st.error('Account was not added: ' + str(getattr(exc, 'message', None) or exc))
        if accounts:
            by_id = {a['id']: a for a in accounts}
            selected = st.selectbox('Account to manage',
                sorted(by_id, key=lambda i: (by_id[i]['name'].casefold(), str(i))),
                format_func=lambda i: by_id[i]['name'])
            account = by_id[selected]
            has_entries = any(r.get('credit_account_id') == selected for r in entries)
            with st.form('credit_manage_' + str(selected) + '_' + generation):
                name = st.text_input('Name', value=account['name'])
                kinds = ['Credit Card', 'Line of Credit', 'Lender', 'Loan']
                kind = st.selectbox('Type', kinds,
                    index=kinds.index(account['account_type']))
                active = st.checkbox('Active', value=account['active'])
                monthly_budget = st.checkbox('Use a monthly spending budget', value=bool(account.get('monthly_spending_budget')))
                st.caption('Set its monthly amount and payment day in Budgeted bills and income. Checking reserves the greater of that budget and charges; payment completion applies the actual amount paid. Existing monthly occurrences keep their saved budget.')
                limit = st.number_input('Credit limit ($)', min_value=0.0,
                    max_value=999999999.99, value=float(money(account['credit_limit']))
                    if account.get('credit_limit') is not None else None, format='%.2f')
                apr = st.number_input('APR (%)', min_value=0.0,max_value=100.0,
                    value=float(money(account['apr'])) if account.get('apr') is not None else None,
                    format='%.2f')
                minimum = st.number_input('Minimum payment reference ($)',
                    min_value=0.0,max_value=999999999.99,
                    value=float(money(account['minimum_payment']))
                    if account.get('minimum_payment') is not None else None,format='%.2f')
                opening = st.number_input(
                    'Current balance ($)' if has_entries else 'Starting balance ($)',
                    min_value=0.0,
                    max_value=999999999.99,value=float(money(account['live_balance']))
                    if account.get('live_balance') is not None else None,
                    format='%.2f',disabled=has_entries)
                if has_entries:
                    st.caption('Shown balance includes dated entries and cannot be edited here. '
                               'Edit a dated charge or payment to correct it.')
                manage = st.form_submit_button('Save account details')
            if manage:
                credit_action('ledger_save_credit_profile_with_budget', dict(
                    p_id=selected,p_revision=account['revision'],p_name=name,p_type=kind,
                    p_active=active,p_limit=None if limit is None else str(money(limit)),
                    p_apr=None if apr is None else str(money(apr)),
                    p_minimum=None if minimum is None else str(money(minimum)),
                    p_opening=None if opening is None else str(money(opening)),p_monthly_budget=monthly_budget))
            if account.get('live_balance') is not None:
                with st.form('credit_balance_correction_' + str(selected) + '_' + generation):
                    st.caption('Correct this balance to a statement figure. This does not create a charge or affect checking.')
                    corrected = st.number_input('Correct current balance ($)', min_value=0.0,
                        max_value=999999999.99, value=float(money(account['live_balance'])),
                        format='%.2f')
                    correction_note = st.text_input('Statement note (optional)')
                    save_correction = st.form_submit_button('Save balance correction')
                if save_correction:
                    credit_action('ledger_correct_credit_balance', dict(
                        p_id=selected, p_revision=account['revision'],
                        p_balance=str(money(corrected)), p_note=correction_note))
        render_loan_manager()
    loans = load_budget_table('LedgerFederalLoans')
    if not accounts and not loans:
        st.info('Add a credit account or student loan to begin.')
        return
    known = [money(a['live_balance']) for a in accounts if a.get('live_balance') is not None]
    unknown = sum(a.get('live_balance') is None for a in accounts)
    limited_accounts = [a for a in accounts if a['active'] and a.get('credit_limit') is not None
        and money(a['credit_limit']) > 0]
    total_limit = sum((money(a['credit_limit']) for a in limited_accounts), Decimal(0))
    usage_balance = sum((money(a['live_balance']) for a in limited_accounts
        if a.get('live_balance') is not None), Decimal(0))
    c1,c2,c3,c4 = st.columns(4)
    today = datetime.now(LOCAL_TZ).date()
    loan_totals = {}
    for loan in loans:
        try:
            p, i = loan_balance(loan, max(today,date.fromisoformat(loan['as_of'])))
            loan_totals[int(loan['id'])] = p+i
        except ValueError as exc:
            st.error('Loan balance could not be estimated: ' + str(exc))
    c1.metric('Current balances' if not unknown else 'Established balance subtotal',
        f'${sum(known,Decimal(0))+sum(loan_totals.values(),Decimal(0)):,.2f}')
    c2.metric('Entered credit limits',f'${total_limit:,.2f}')
    usage_known = all(a.get('live_balance') is not None for a in limited_accounts)
    c3.metric('Current usage',f'{usage_balance/total_limit*100:,.2f}%' if total_limit and usage_known else 'N/A')
    c4.metric('Target use',f'{target:,.2f}%')
    if unknown:
        st.warning(f'{unknown} account(s) need an opening balance before charges and payments can be recorded.')
    rows = []
    whatif_values = st.session_state.get('credit_whatif_payments', {})
    def whatif_result(principal, accrued, apr, payment, federal=False, basis=Decimal('365.25'), start_date=None):
        if payment is None:
            return ''
        if principal is None or apr is None:
            return 'Set balance and APR'
        estimate = payoff_estimate(principal, accrued, apr, payment, start_date or today, federal, basis)
        return (f"{estimate['months']} months" if estimate['months'] is not None
            else estimate['status'])
    for a in sorted(accounts,key=lambda item:item['name'].casefold()):
        balance = None if a.get('live_balance') is None else money(a['live_balance'])
        limit = None if a.get('credit_limit') is None else money(a['credit_limit'])
        use = None if balance is None or not limit else balance/limit*100
        target_amount = None if limit is None else limit*target/100
        whatif_key = 'credit:' + str(a['id'])
        whatif_payment = whatif_values.get(whatif_key)
        rows.append({'_key':whatif_key,'Account':a['name'],'Type':a['account_type'],
            'Status':'Active' if a['active'] else 'Inactive',
            'Current balance':None if balance is None else float(balance),
            'Credit limit':None if limit is None else float(limit),
            'APR %':None if a.get('apr') is None else float(money(a['apr'])),
            'Current usage %':None if use is None else float(use),
            'Remaining credit':None if balance is None or limit is None else float(limit-balance),
            'Payment to target use':None if balance is None or target_amount is None
                else float(max(balance-target_amount,Decimal(0))),
            'What-if monthly payment':whatif_payment,
            'Estimated time to pay off':whatif_result(balance, Decimal(0), a.get('apr'),
                whatif_payment)})
    for loan in loans:
        balance = loan_totals.get(int(loan['id']))
        whatif_key = 'loan:' + str(loan['id'])
        whatif_payment = whatif_values.get(whatif_key)
        try:
            principal, accrued = loan_balance(loan, max(today, date.fromisoformat(loan['as_of'])))
            payoff_time = whatif_result(principal, accrued, loan['rate'], whatif_payment,
                True, Decimal(str(loan['day_basis'])), max(today, date.fromisoformat(loan['as_of'])))
        except ValueError:
            payoff_time = 'Loan balance unavailable' if whatif_payment is not None else ''
        rows.append({'_key':whatif_key,'Account':loan['name'],'Type':'Federal student loan',
            'Status':'Active','Current balance':None if balance is None else float(balance),
            'Credit limit':None,'APR %':float(money(loan['rate'])),
            'Current usage %':None,'Remaining credit':None,'Payment to target use':None,
            'What-if monthly payment':whatif_payment,
            'Estimated time to pay off':payoff_time})
    st.caption('Enter a hypothetical monthly payment in the table to estimate payoff time. It does not create a transaction. Estimates start today, assume fixed rates and no new borrowing or fees; cards use monthly interest and the federal loan uses daily simple interest.')
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values('Current balance', ascending=False,
            na_position='last', kind='stable').reset_index(drop=True)
    render_credit_account_table(frame.to_dict('records'), whatif_values)
    st.caption('Card charges raise credit owed without reducing checking. Scheduled card and loan payments affect checking on their dates and reduce debt only when marked complete. Card statement interest can be included through a balance correction; loan interest is estimated daily.')
    if entries:
        st.subheader('Dated debt entries')
        names = {a['id']:a['name'] for a in accounts}
        loan_names = {int(a['id']):a['name'] for a in loans}
        def debt_name(row):
            return (loan_names.get(int(row['federal_loan_id']), 'Unavailable loan')
                if row.get('federal_loan_id') is not None else
                names.get(row.get('credit_account_id'), 'Unavailable account'))
        selected_account = st.session_state.get('debt_account_filter')
        if selected_account:
            title = next((r['Account'] for r in rows if r['_key']==selected_account), 'Selected account')
            st.caption('Showing ' + title)
            if st.button('Show all accounts'):
                st.session_state.pop('debt_account_filter',None)
                st.session_state.pop('credit_edit_id',None)
                st.rerun()
        def row_account_key(row):
            return ('loan:' + str(row['federal_loan_id']) if row.get('federal_loan_id') is not None
                else 'credit:' + str(row.get('credit_account_id')))
        ordered = sorted([e for e in entries if not selected_account or row_account_key(e)==selected_account],key=lambda e:int(e['id']),reverse=True)
        if not ordered:
            st.info('No dated entries for this account.')

        st.dataframe(pd.DataFrame([{'Date':r['date'],
            'Account':debt_name(r),
            'Action':r.get('credit_action') or 'Loan payment','Amount':float(money(r['amount'])),
            'Merchant':r['merchant'],'Category':r['category'],
            'Status':'Applied' if r.get('credit_action')=='Charge' or r.get('credit_applied_at')
                or r.get('federal_loan_applied_at')
                else 'Scheduled'} for r in ordered]),hide_index=True,use_container_width=True,
            column_config={'Amount':st.column_config.NumberColumn(format='$%.2f')})
        choice = st.selectbox('Credit entry to edit',ordered, index=0 if ordered else None,
            format_func=lambda r: str(r['date'])+' · '+debt_name(r)+
                ' · '+(r.get('credit_action') or 'Loan payment')+' · $'+str(money(r['amount'])))
        if st.button('Edit selected credit entry',disabled=not ordered):
            st.session_state['credit_edit_id'] = choice['id']
        selected_edit = next((r for r in ordered
            if r['id'] == st.session_state.get('credit_edit_id')), None)
        if selected_edit is not None:
            if selected_edit.get('federal_loan_id') is not None:
                edit_federal_loan_payment_form(selected_edit)
            else:
                edit_credit_transaction_form(selected_edit)
    with st.expander('Prior monthly worksheets — read-only history'):
        try:
            months = load_budget_table('LedgerCreditMonths')
            names = {a['id']:a['name'] for a in accounts}
            history = []
            for row in sorted(months,key=lambda r:(r['month'],names.get(r['account_id'],'')),reverse=True):
                result = credit_calculations(row,target)
                history.append({'Worksheet month':row['month'],
                    'Account':names.get(row['account_id'],'Unavailable account'),
                    'Statement balance':None if row['statement_balance'] is None
                        else float(money(row['statement_balance'])),
                    'Saved current balance':None if result['Current balance'] is None
                        else float(result['Current balance'])})
            if history:
                st.dataframe(pd.DataFrame(history),hide_index=True,use_container_width=True)
            else:
                st.caption('No older worksheets.')
        except Exception:
            st.error('Older worksheets could not be loaded.')


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


def account_impact(kind, account_id):
    require_session()
    response = conn.rpc('ledger_account_impact', {
        'p_kind': kind, 'p_id': account_id}).execute()
    if not isinstance(response.data, dict):
        raise RuntimeError('Account review was not confirmed.')
    return response.data


def render_manage_account_page():
    require_session()
    name = st.session_state['ledger_account']
    checking = cash_accounts()
    savings = savings_accounts()
    found = next((('checking', aid) for aid, row in checking.items()
        if row['name'] == name and not row.get('archived_at')), None)
    if found is None:
        found = next((('savings', aid) for aid, row in savings.items()
            if row['name'] == name and not row.get('archived_at')), None)
    if found is None:
        st.error('Select an active account first.')
        return
    kind, account_id = found
    st.title('Manage ' + name)
    try:
        impact = account_impact(kind, account_id)
    except Exception:
        logger.exception('Account impact could not be loaded.')
        st.error('Account details could not be checked. Apply the follow-up SQL update and reload.')
        return
    st.write(f"Recorded transactions: {impact['transactions']}; linked transfers: {impact['transfers']}; "
        f"budget records: {impact['budget_records']}; audit records: {impact['audit_records']}.")
    if impact.get('day_markers'):
        st.write(f"Calendar day markers: {impact['day_markers']}.")
    if kind == 'savings':
        st.write(f"Current savings balance: ${money(impact['balance']):,.2f}.")
    if impact['primary']:
        st.info('Primary accounts are protected from archiving and deletion.')
        return
    if impact['active_budget_rules']:
        st.warning('Pause budget rules involving this account before archiving it.')
    if impact.get('pending_savings_plans'):
        st.warning('Complete or cancel pending savings transfers before archiving this account.')
    if kind == 'savings' and money(impact['balance']) != 0:
        st.warning('Move the savings balance before archiving this account.')
    st.caption('Archiving hides an account from active navigation and preserves its history. '
        'Permanent deletion is available only when the account has never been used and is empty.')
    stage_key = (kind, account_id)
    current_stage = st.session_state.get('account_confirm_stage')
    if impact['can_archive'] and st.button('Review archiving this account'):
        st.session_state['account_confirm_stage'] = (*stage_key, 'archive')
        current_stage = st.session_state['account_confirm_stage']
    if impact['can_delete'] and st.button('Review permanent deletion'):
        st.session_state['account_confirm_stage'] = (*stage_key, 'delete')
        current_stage = st.session_state['account_confirm_stage']
    if current_stage not in ((*stage_key, 'archive'), (*stage_key, 'delete')):
        return
    action = current_stage[2]
    if action == 'archive' and not impact['can_archive'] or action == 'delete' and not impact['can_delete']:
        st.session_state.pop('account_confirm_stage', None)
        st.error('Account eligibility changed. Review it again.')
        return
    st.warning(('Final confirmation: archive ' if action == 'archive' else
        'Final confirmation: permanently delete ') + name + '?')
    typed = st.text_input('Type the exact account name to confirm', key='confirm_account_name_' + str(account_id) + action)
    if st.button('Archive account' if action == 'archive' else 'Permanently delete account',
        type='primary', disabled=typed != name):
        rpc = 'ledger_archive_account' if action == 'archive' else 'ledger_delete_empty_account'
        account_action(rpc, {'p_kind': kind, 'p_id': account_id,
            'p_revision': impact['revision'], 'p_confirm_name': typed})


def render_archived_accounts_page():
    require_session()
    st.title('Archived Accounts')
    archived = [('checking', aid, row) for aid, row in cash_accounts().items()
        if row.get('archived_at')]
    archived += [('savings', aid, row) for aid, row in savings_accounts().items()
        if row.get('archived_at')]
    if not archived:
        st.info('No archived accounts.')
        return
    st.caption('Archived accounts are read-only. Their recorded history remains available here.')
    for kind, account_id, row in archived:
        if st.button(row['name'], key='archived_account_' + kind + str(account_id), use_container_width=True):
            st.session_state['archived_detail'] = (kind, account_id)
    choice = st.session_state.get('archived_detail')
    selected = next(((kind, account_id, row) for kind, account_id, row in archived
        if choice == (kind, account_id)), None)
    if selected is None:
        return
    kind, account_id, row = selected
    st.subheader(row['name'])
    try:
        impact = account_impact(kind, account_id)
        if kind == 'checking':
            history = [t for t in get_all_transactions_cached()
                if int(t.get('cash_account_id') or 1) == account_id]
            if history:
                st.dataframe(pd.DataFrame(history).sort_values('date', ascending=False),
                    hide_index=True, use_container_width=True)
            else:
                st.info('No transactions recorded.')
            marked = conn.table('LedgerDayMarkers').select('day,color').eq('account_id', account_id).execute().data or []
            if marked:
                st.caption('Saved calendar markers')
                st.dataframe(pd.DataFrame(marked).sort_values('day', ascending=False),
                    hide_index=True, use_container_width=True)
        else:
            buckets = [b for b in load_budget_table('LedgerSavingsBuckets')
                if int(b['savings_account_id']) == account_id]
            if buckets:
                st.dataframe(pd.DataFrame(buckets), hide_index=True, use_container_width=True)
            bucket_ids = {b['id'] for b in buckets}
            audit = [a for a in load_budget_table('LedgerSavingsAudit') if a['bucket_id'] in bucket_ids]
            if audit:
                st.dataframe(pd.DataFrame(audit).sort_values('recorded_at', ascending=False),
                    hide_index=True, use_container_width=True)
    except Exception:
        logger.exception('Archived account history could not be loaded.')
        st.error('Archived account history could not be loaded.')
        return
    typed = st.text_input('Type the account name to restore it', key='restore_account_name_' + kind + str(account_id))
    if st.button('Restore account', disabled=typed != row['name']):
        account_action('ledger_restore_account', {'p_kind': kind, 'p_id': account_id,
            'p_revision': impact['revision'], 'p_confirm_name': typed})


def account_action(name, payload):
    require_session()
    try:
        response = conn.rpc(name, payload).execute()
        if response.data is not True:
            raise ValueError('Save was not confirmed.')
        clear_transaction_caches()
        # The Savings page holds a per-session snapshot until explicitly reloaded.
        # Any linked transfer may change category balances, so discard that view.
        st.session_state.pop('savings_snapshot', None)
        st.session_state['savings_generation'] = st.session_state.get('savings_generation', 0) + 1
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


def convert_direct_to_savings_form(original):
    """Replace one older Direct debit with a completed linked savings transfer."""
    buckets = [b for b in load_budget_table('LedgerSavingsBuckets')
        if b['active'] and b['balance'] is not None]
    active_savings = {i for i, row in savings_accounts().items() if not row.get('archived_at')}
    choices = {int(b['id']): b for b in buckets
        if int(b['savings_account_id']) in active_savings}
    if not choices:
        st.error('Establish an active savings category balance before making this transfer.')
        return
    preferred = linked_current_budget_savings_category(original)
    rules = load_budget_table('LedgerUnifiedBudgetRules')
    if preferred not in choices:
        preferred = None
    if preferred is None:
        label = str(original.get('description') or original.get('merchant') or '').strip().casefold()
        matching = [r for r in rules if r['transaction_type'] == 'Transfer'
            and r['effective_until'] is None and r['paid_from'] == 'c:' + str(original['cash_account_id'])
            and str(r['name']).strip().casefold() == label
            and r.get('destination_savings_bucket_id') in choices]
        if len(matching) == 1:
            preferred = int(matching[0]['destination_savings_bucket_id'])
    if preferred is None:
        transfer_day = date.fromisoformat(str(original['date'])[:10])
        matching = [r for r in rules if r['transaction_type'] == 'Transfer'
            and r['enabled'] and r['effective_until'] is None
            and r['paid_from'] == 'c:' + str(original['cash_account_id'])
            and r.get('destination_savings_bucket_id') in choices
            and money(r['amount']) == money(original['amount'])
            and ((r['schedule'] == 'Weekly' and int(r['weekday']) == transfer_day.weekday())
                 or (r['schedule'] == 'Monthly' and int(r['day_of_month']) == transfer_day.day))]
        if len(matching) == 1:
            preferred = int(matching[0]['destination_savings_bucket_id'])
    keys = list(choices)
    savings_names = savings_accounts()
    st.info('This is an older Direct expense. Marking it as a transfer replaces that checking debit '
            'with a linked transfer of the same amount and date, then adds the amount to savings once.')
    st.write('Date:', str(original['date'])[:10], ' · Amount: $' + f"{money(original['amount']):,.2f}")
    destination = st.selectbox('Savings category for this transfer', keys,
        index=keys.index(preferred) if preferred in keys else None,
        placeholder='Choose the category',
        format_func=lambda i: savings_names[int(choices[i]['savings_account_id'])]['name']
            + ' / ' + choices[i]['name'],
        key='convert_direct_bucket_' + str(original['id']))
    if st.button('Mark transfer made', type='primary',
                 disabled=destination is None,
                 key='convert_direct_transfer_' + str(original['id'])):
        account_action('ledger_convert_direct_to_savings_transfer', dict(
            p_transaction_id=original['id'], p_revision=original['check_revision'],
            p_destination_bucket=destination))


def transfer_form(original=None, pending=False):
    if original:
        st.caption('Transaction type: Transfer')
    accounts = {i: row for i, row in cash_accounts().items() if not row.get('archived_at')}
    savings = {i: row for i, row in savings_accounts().items() if not row.get('archived_at')}
    buckets = load_budget_table('LedgerSavingsBuckets')
    endpoints = {'c:' + str(i): row['name'] for i, row in accounts.items()}
    endpoints.update({'s:' + str(b['id']): savings[int(b['savings_account_id'])]['name'] + ' / ' + b['name']
        for b in buckets if int(b['savings_account_id']) in savings and b['active'] and b['balance'] is not None})
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
        if pending and st.button('Cancel unavailable planned transfer'):
            account_action('ledger_save_manual_savings_plan', dict(p_id=original['id'],
                p_revision=original['revision'], p_data={}, p_action='cancel'))
        return
    with st.form('transfer_' + str(original['id'] if original else 'new')):
        src = st.selectbox('From account', options, index=options.index(source), format_func=endpoints.get)
        dst = st.selectbox('To account', options, index=options.index(destination), format_func=endpoints.get)
        amount = st.number_input('Transfer amount', min_value=0.01, max_value=999999999.99,
            value=float(money(original['amount'])) if original else None, format='%.2f')
        tx_date = st.date_input('Transfer date',
            value=date.fromisoformat(original['date']) if original else datetime.now(LOCAL_TZ).date())
        description = st.text_input('Description', value=original['description'] if original else '')
        save = st.form_submit_button('Save changes / keep pending' if pending else 'Save transfer', type='primary')
        complete = st.form_submit_button('Mark transfer made') if pending else False
        delete = st.form_submit_button('Delete planned transfer' if pending else 'Delete linked transfer') if original else False
    if pending:
        st.info(('This transfer is in checking’s projection. ' if original.get('source_account') is not None or original.get('destination_account') is not None else '') +
                'Savings changes only when you mark it made.')
    if original and not pending:
        completed = bool(original.get('completed_at'))
        budget_savings = bool(original.get('unified_occurrence_id') is not None and
            (original.get('source_bucket') is not None or original.get('destination_bucket') is not None))
        st.caption('Status: ' + ('Completed' if completed else 'Not marked completed'))
        source_for_completion = original.get('source_bucket')
        destination_for_completion = original.get('destination_bucket')
        if budget_savings and not completed:
            st.warning('This budget transfer was posted before the confirmation workflow. '
                       'If it has not actually happened, return it to pending; that reverses its account entries.')
            st.caption('Savings already includes this older transfer. Marking it made moves the amount to the category below; '
                       'it will not increase total savings again. Editing the recurring budget item does not change an older transfer.')
            occurrence = next((r for r in load_budget_table('LedgerUnifiedBudgetOccurrences')
                if int(r['id']) == int(original['unified_occurrence_id'])), None)
            if occurrence is None:
                st.error('Budget transfer changed. Reload the calendar.')
                return
            for side in ('source', 'destination'):
                current_bucket = original.get(side + '_bucket')
                if current_bucket is None:
                    continue
                selected_bucket = occurrence.get(side + '_savings_bucket_id') or current_bucket
                account_id = next((int(b['savings_account_id']) for b in buckets
                    if int(b['id']) == int(current_bucket)), None)
                choices = {int(b['id']): b for b in buckets
                    if int(b['savings_account_id']) == account_id and b['active'] and b['balance'] is not None}
                if not choices:
                    st.error('Savings categories are unavailable. Restore an active balance before completing.')
                    return
                keys = list(choices)
                selected_bucket = int(selected_bucket)
                selected_bucket = st.selectbox(side.capitalize() + ' savings category for this transfer',
                    keys, index=keys.index(selected_bucket) if selected_bucket in keys else 0,
                    format_func=lambda i: choices[i]['name'],
                    key='complete_transfer_' + side + '_' + str(original['id']))
                if side == 'source':
                    source_for_completion = selected_bucket
                else:
                    destination_for_completion = selected_bucket
        if not budget_savings or not completed:
            if st.button('Remove completed mark' if completed else 'Mark transfer made',
                         key='mark_transfer_' + str(original['id'])):
                account_action('ledger_set_transfer_completed', dict(
                    p_id=original['id'], p_revision=original['revision'], p_completed=not completed,
                    p_source_bucket=source_for_completion if not completed else None,
                    p_destination_bucket=destination_for_completion if not completed else None))
        if budget_savings:
            reverse_confirmed = st.checkbox('I understand this will reverse the linked checking and savings entries',
                key='reopen_transfer_confirm_' + str(original['id']))
            if st.button('Return this transfer to pending',
                         key='reopen_transfer_' + str(original['id']), disabled=not reverse_confirmed):
                account_action('ledger_reopen_budget_savings_occurrence', dict(
                    p_transfer_id=original['id'], p_revision=original['revision']))
    if save or delete or complete:
        if not delete and (src == dst or amount is None):
            st.error('Choose different accounts and enter an amount.')
            return
        payload = dict(source_account=int(src[2:]) if src.startswith('c:') else None,
            source_bucket=int(src[2:]) if src.startswith('s:') else None,
            destination_account=int(dst[2:]) if dst.startswith('c:') else None,
            destination_bucket=int(dst[2:]) if dst.startswith('s:') else None,
            amount=str(money(amount or 0)),date=tx_date.isoformat(),description=description,
            schedule_id=None,occurrence_date=None)
        if pending or (not original and (payload['source_bucket'] is not None or payload['destination_bucket'] is not None)):
            account_action('ledger_save_manual_savings_plan', dict(p_id=original['id'] if original else None,
                p_revision=original['revision'] if original else None, p_data=payload,
                p_action='cancel' if delete else 'complete' if complete else 'save'))
        else:
            account_action('ledger_save_transfer', dict(p_id=original['id'] if original else None,
                p_revision=original['revision'] if original else None,p_data=payload,p_delete=delete))


def loan_balance(loan, through):
    principal=money(loan['principal']); interest=Decimal(str(loan['accrued_interest']))
    annual=Decimal(str(loan['rate']))/100; basis=Decimal(str(loan.get('day_basis',365.25)))
    day=date.fromisoformat(loan['as_of'])
    if through<day: raise ValueError('Projection date precedes the statement baseline.')
    events=list(enumerate(loan.get('events',[])))
    if loan.get('id') is not None:
        linked = [r for r in get_all_transactions_cached()
            if r.get('type') == 'Federal student loan'
            and r.get('federal_loan_id') is not None
            and int(r['federal_loan_id']) == int(loan['id'])
            and r.get('federal_loan_applied_at')
            and str(r['date'])[:10] > loan['as_of']]
        events.extend((len(events)+int(r['id']),dict(date=str(r['date'])[:10],
            kind='Payment',amount=r['amount'])) for r in linked)
    events.sort(key=lambda pair:(pair[1]['date'],pair[0]))
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


def render_loan_manager():
    st.divider(); st.subheader('Student loan statement')
    loans=load_budget_table('LedgerFederalLoans')
    choices={None:'Add federal consolidation loan',**{r['id']:r['name'] for r in loans}}
    selected=st.selectbox('Student loan',list(choices),
        index=1 if loans else 0,format_func=choices.get)
    old=next((r for r in loans if r['id']==selected),None)
    st.caption('Enter principal and unpaid interest separately from a servicer statement. Completed budget payments after that statement date are included automatically; do not enter them again in the events table. If a newer statement includes earlier payments, move the baseline date forward to avoid counting them twice.')
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
        save=st.form_submit_button('Save loan statement')
    if save:
        try:
            if principal is None or interest is None: raise ValueError('Enter principal and accrued interest, including zero when applicable.')
            ev=[dict(date=str(e['Date'])[:10],kind=e['Kind'],amount=str(money(e['Amount']))) for e in events.to_dict('records')]
            payload=dict(name=name,principal=str(money(principal)),accrued_interest=str(money(interest)),rate=str(rate),as_of=as_of.isoformat(),day_basis=str(basis),events=ev)
            loan_balance(payload,max([as_of]+[date.fromisoformat(e['date']) for e in ev]))
            st.session_state.pop('credit_snapshot',None)
            account_action('ledger_save_federal_loan',dict(p_id=selected,p_revision=old['revision'] if old else None,p_data=payload))
        except (ValueError,TypeError,InvalidOperation) as exc: st.error(str(exc))
    if old:
        with st.expander('Saved loan statement history'):
            history=[r for r in load_budget_table('LedgerAccountAudit') if r['kind']=='Federal loan statement and events'
                and (r.get('after_data') or {}).get('id')==old['id']]
            if history:
                st.dataframe(pd.DataFrame([{'Saved':r['recorded_at'],'Statement date':r['after_data']['as_of'],
                    'Principal':float(money(r['after_data']['principal'])),'Unpaid interest':float(money(r['after_data']['accrued_interest'])),
                    'Rate %':float(r['after_data']['rate']),'Events':json.dumps(r['after_data']['events'])} for r in reversed(history)]),hide_index=True)
            else: st.caption('No saved history yet.')


# ============================================================
# SIDEBAR BUTTONS & ACCOUNT CONTROLS
# ============================================================

try:
    checking_accounts = cash_accounts()
    saved_savings_accounts = savings_accounts()
except Exception:
    st.error('Install unified_budget_update.sql after the multi-account update, then reload.')
    st.stop()
account_names = {row['name']: aid for aid, row in checking_accounts.items() if not row.get('archived_at')}
savings_names = {row['name']: aid for aid, row in saved_savings_accounts.items() if not row.get('archived_at')}
choices = list(account_names) + list(savings_names)
if st.session_state.get('ledger_account') not in choices:
    st.session_state['ledger_account'] = 'Primary Checking'
    st.session_state['ledger_view'] = 'Account'

st.sidebar.caption('Accounts')
for name in choices:
    if st.sidebar.button(name, key='select_account_' + name,
        type='primary' if st.session_state['ledger_account'] == name
            and st.session_state.get('ledger_view', 'Account') == 'Account' else 'secondary',
        use_container_width=True):
        st.session_state['ledger_account'] = name
        st.session_state['ledger_view'] = 'Account'
        st.session_state.pop('savings_snapshot', None)
        st.session_state.pop('account_confirm_stage', None)

account_selection = st.session_state['ledger_account']
process_due_savings_deposits()

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

if st.sidebar.button('Manage selected account', use_container_width=True):
    st.session_state['ledger_view'] = 'Manage Account'
    st.session_state.pop('account_confirm_stage', None)
if st.sidebar.button('Archived Accounts', use_container_width=True):
    st.session_state['ledger_view'] = 'Archived Accounts'
st.sidebar.divider()

with st.sidebar.container(key='sidebar_add_transaction'):
    if st.button('➕ Add Transaction', type='primary', use_container_width=True,
        disabled=st.session_state.get('ledger_view') in ('Archived Accounts','Manage Account')):
        reset_add_transaction_state()
        add_transaction_dialog()
if st.sidebar.button('✏️ Edit Transaction', use_container_width=True,
    disabled=st.session_state.get('ledger_view') in ('Archived Accounts','Manage Account')):
    st.session_state.pop('edit_loaded_tx_id', None)
    edit_transaction_dialog()
if st.sidebar.button('Transaction Categories', use_container_width=True):
    st.session_state['ledger_view'] = 'Categories'
if st.sidebar.button('AMZ Card Ledger', use_container_width=True):
    st.session_state['ledger_view'] = 'Reconcile'

if st.sidebar.button('View / Edit Budget', use_container_width=True):
    st.session_state['ledger_view'] = 'Budget'
if st.sidebar.button('Lines of Credit', use_container_width=True):
    if st.session_state.get('ledger_view') != 'Lines of Credit':
        st.session_state['credit_generation'] = st.session_state.get('credit_generation', 0) + 1
    st.session_state['ledger_view'] = 'Lines of Credit'

if st.session_state.get('ledger_view') in ('Budget', 'Reconcile', 'Lines of Credit', 'Categories',
    'Archived Accounts', 'Manage Account'):
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

elif account_selection == "Categories":
    render_manage_categories_page()

elif account_selection == "Lines of Credit":
    render_credit_page()

elif account_selection == "Manage Account":
    render_manage_account_page()

elif account_selection == "Archived Accounts":
    render_archived_accounts_page()

elif account_selection in savings_names:
    render_savings_page()








