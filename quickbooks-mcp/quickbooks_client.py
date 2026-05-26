"""QuickBooks API client for the MCP server. Uses Firestore for OAuth tokens (tokens/quickbooks)."""

import logging
import os
import time
from datetime import date, datetime
from typing import Any, List, Optional

import requests
from google.cloud import firestore
from intuitlib.client import AuthClient
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class QuickBooksAPIError(RuntimeError):
    """Raised when QuickBooks returns an error or a malformed response."""


def _qbo_query_entity_list(query_response: dict, entity_key: str) -> List[dict]:
    """QBO query JSON returns one entity as a dict and several as a list; normalize to a list of dicts."""
    raw = query_response.get(entity_key)
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    logger.warning("Unexpected %s payload type: %s", entity_key, type(raw).__name__)
    return []


def _qbo_line_list(raw: Any) -> List[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _expense_categories_from_lines(lines_raw: Any) -> Optional[str]:
    names: List[str] = []
    for line in _qbo_line_list(lines_raw):
        abd = line.get("AccountBasedExpenseLineDetail") or {}
        ibd = line.get("ItemBasedExpenseLineDetail") or {}
        ref = abd.get("AccountRef") or ibd.get("AccountRef") or ibd.get("ItemRef")
        if isinstance(ref, dict) and ref.get("name"):
            names.append(str(ref["name"]))
        desc = line.get("Description")
        if desc and not names:
            names.append(str(desc)[:120])
    return "; ".join(names) if names else None


def _txn_primary_description(txn: dict, lines_raw: Any) -> str:
    for key in ("PrivateNote", "Memo"):
        v = txn.get(key)
        if v:
            return str(v)
    for line in _qbo_line_list(lines_raw):
        d = line.get("Description")
        if d:
            return str(d)
    return ""


def _ref_value_name(ref: Any) -> tuple[str, str]:
    if not isinstance(ref, dict):
        return "", ""
    return str(ref.get("value") or ""), str(ref.get("name") or "")


def _txn_date(txn: dict) -> date:
    return datetime.strptime(txn["TxnDate"], "%Y-%m-%d").date()


def _first_deposit_category(lines_raw: Any) -> str:
    for line in _qbo_line_list(lines_raw):
        detail = line.get("DepositLineDetail") or {}
        account_ref = detail.get("AccountRef") or {}
        if isinstance(account_ref, dict) and account_ref.get("name"):
            return str(account_ref["name"])
        if line.get("Description"):
            return str(line["Description"])[:120]
    return ""


def _linked_txn_ids(lines_raw: Any) -> List[str]:
    ids: List[str] = []
    for line in _qbo_line_list(lines_raw):
        linked = line.get("LinkedTxn")
        linked_items = linked if isinstance(linked, list) else [linked] if isinstance(linked, dict) else []
        for item in linked_items:
            if isinstance(item, dict) and item.get("TxnId"):
                ids.append(str(item["TxnId"]))
    return ids


def _bill_payment_bank_account_ref(txn: dict) -> tuple[str, str]:
    check_payment = txn.get("CheckPayment")
    if not isinstance(check_payment, dict):
        return "", ""
    return _ref_value_name(check_payment.get("BankAccountRef"))


def _matches_external_id(txn: dict, external_transaction_id: Optional[str]) -> bool:
    if not external_transaction_id:
        return True
    needle = str(external_transaction_id).strip()
    if not needle:
        return True
    candidate_keys = ("Id", "DocNumber", "PaymentRefNum", "CheckPayment", "TxnSource")
    for key in candidate_keys:
        value = txn.get(key)
        if value is not None and str(value) == needle:
            return True
    for key in ("MetaData", "CreditCardPayment", "TxnTaxDetail"):
        raw = txn.get(key)
        if isinstance(raw, dict) and any(str(v) == needle for v in raw.values() if v is not None):
            return True
    return False


class BankEntry(BaseModel):
    id: str
    date: date
    amount: float
    description: str
    account_id: Optional[str] = None
    account_name: str
    category: str


class BankAccount(BaseModel):
    """Bank account from QuickBooks (AccountType=Bank)."""

    id: str
    name: str
    account_type: str
    account_sub_type: Optional[str] = None
    current_balance: Optional[float] = None
    currency_code: Optional[str] = None


class TransferEntry(BaseModel):
    """Transfer transaction between two accounts."""

    id: str
    date: date
    amount: float
    from_account_id: str
    from_account_name: str
    to_account_id: str
    to_account_name: str
    private_note: Optional[str] = None


class BankOutEntry(BaseModel):
    """Money leaving a bank account: transfer, purchase, or expense (register \"Spent\")."""

    id: str
    date: date
    amount: float
    transaction_type: str  # transfer | purchase | expense
    bank_account_id: str
    bank_account_name: str
    private_note: Optional[str] = None
    payee_name: Optional[str] = None
    category: Optional[str] = None
    to_account_id: Optional[str] = None
    to_account_name: Optional[str] = None


class BankCreditEntry(BaseModel):
    """Incoming credit candidate for a bank reconciliation workflow."""

    id: str
    date: date
    amount: float
    transaction_type: str
    bank_account_id: Optional[str] = None
    bank_account_name: Optional[str] = None
    description: Optional[str] = None
    counterparty_name: Optional[str] = None
    category: Optional[str] = None
    doc_number: Optional[str] = None
    payment_ref_num: Optional[str] = None
    linked_txn_ids: List[str] = Field(default_factory=list)


class CompanyInfo(BaseModel):
    """QuickBooks company context for diagnostics."""

    realm_id: str
    environment: Optional[str] = None
    company_name: Optional[str] = None
    legal_name: Optional[str] = None
    country: Optional[str] = None
    email: Optional[str] = None


def _get_firestore():
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    database = os.getenv("GOOGLE_CLOUD_DATABASE", "data-warehouse-firestore")
    return firestore.Client(project=project_id, database=database)


def _load_credentials() -> dict:
    db = _get_firestore()
    doc = db.collection("tokens").document("quickbooks").get()
    if not doc.exists:
        raise RuntimeError("QuickBooks tokens not found in Firestore (tokens/quickbooks)")
    return doc.to_dict()


def _save_credentials(data: dict) -> None:
    db = _get_firestore()
    db.collection("tokens").document("quickbooks").set(data)


class QuickBooksService:
    """QuickBooks API client with Firestore-backed OAuth tokens."""

    def __init__(self):
        self._initialized = False
        self.credentials: Optional[dict] = None
        self.realm_id: Optional[str] = None
        self.base_url: Optional[str] = None
        self.auth_client: Optional[AuthClient] = None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        self.credentials = _load_credentials()
        self.realm_id = self.credentials["realm_id"]
        environment = self.credentials["environment"]
        host = "sandbox-quickbooks.api.intuit.com" if environment == "sandbox" else "quickbooks.api.intuit.com"
        self.base_url = f"https://{host}/v3/company/{self.realm_id}"

        self.auth_client = AuthClient(
            client_id=self.credentials["client_id"],
            client_secret=self.credentials["client_secret"],
            redirect_uri=self.credentials["redirect_uri"],
            environment=self.credentials["environment"],
        )
        self.auth_client.refresh_token = self.credentials["refresh_token"]

        now = time.time()
        expires_at = self.credentials.get("expires_at", 0)
        if not self.credentials.get("access_token") or now >= expires_at - 600:
            logger.info("Access token expired or missing. Refreshing...")
            if not self._refresh_access_token():
                logger.error("Failed to refresh access token.")
                raise QuickBooksAPIError("QuickBooks access token expired and refresh failed")
            logger.info("Access token refreshed.")
        else:
            self.auth_client.access_token = self.credentials["access_token"]

        self._initialized = True

    def _refresh_access_token(self) -> bool:
        try:
            self.auth_client.refresh()
            self.credentials["access_token"] = self.auth_client.access_token
            self.credentials["refresh_token"] = self.auth_client.refresh_token
            self.credentials["expires_at"] = time.time() + self.auth_client.expires_in
            _save_credentials(self.credentials)
            return True
        except Exception as e:
            logger.error("Failed to refresh token: %s", e)
            return False

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credentials['access_token']}",
            "Accept": "application/json",
            "Content-Type": "application/text",
        }

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> dict:
        self._ensure_initialized()
        url = f"{self.base_url}/{endpoint}"
        headers = self._get_headers()

        try:
            response = requests.request(method, url, headers=headers, params=params, json=data)
            if response.status_code == 401:
                logger.warning("401 Unauthorized — attempting token refresh.")
                if self._refresh_access_token():
                    headers = self._get_headers()
                    response = requests.request(method, url, headers=headers, params=params, json=data)
                else:
                    raise QuickBooksAPIError("QuickBooks token refresh failed after 401 response")
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and "Fault" in payload:
                    raise QuickBooksAPIError(f"QuickBooks API fault: {payload['Fault']}")
                if not isinstance(payload, dict):
                    raise QuickBooksAPIError(f"QuickBooks returned unexpected {type(payload).__name__} payload")
                return payload
            raise QuickBooksAPIError(f"QuickBooks API error {response.status_code}: {response.text}")
        except Exception as e:
            if isinstance(e, QuickBooksAPIError):
                raise
            raise QuickBooksAPIError(f"QuickBooks request failed: {e}") from e

    def _query_response(self, query: str) -> dict:
        result = self._make_request("query", params={"query": query})
        query_response = result.get("QueryResponse")
        if not isinstance(query_response, dict):
            raise QuickBooksAPIError(f"QuickBooks query response missing QueryResponse for query: {query}")
        return query_response

    def get_bank_transactions(
        self,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        account_id: Optional[str] = None,
        max_results: int = 100,
        start_position: int = 1,
    ) -> List[BankEntry]:
        query = "SELECT * FROM Deposit"
        conditions: List[str] = []
        filter_after_query = bool(account_id)
        fetch_cap = (
            min(max((max(0, start_position - 1) + max_results) * 4, max_results), 1000)
            if filter_after_query
            else max_results
        )
        query_start_position = 1 if filter_after_query else start_position
        if start_date and end_date:
            start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
            end_str = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)
            conditions.append(f"TxnDate >= '{start_str}' AND TxnDate <= '{end_str}'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDERBY TxnDate DESC"
        query += f" STARTPOSITION {query_start_position} MAXRESULTS {fetch_cap}"

        query_response = self._query_response(query)

        entries: List[BankEntry] = []
        for deposit in _qbo_query_entity_list(query_response, "Deposit"):
            try:
                deposit_account_id, account_name = _ref_value_name(deposit.get("DepositToAccountRef"))
                if account_id and deposit_account_id != account_id:
                    continue
                lines_raw = deposit.get("Line")
                entries.append(
                    BankEntry(
                        id=str(deposit["Id"]),
                        date=_txn_date(deposit),
                        amount=float(deposit.get("TotalAmt", 0)),
                        description=deposit.get("PrivateNote") or deposit.get("Memo") or "",
                        account_id=deposit_account_id or None,
                        account_name=account_name,
                        category=_first_deposit_category(lines_raw),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skip deposit %s: %s", deposit.get("Id"), e)
        if filter_after_query:
            start_idx = max(0, start_position - 1)
            return entries[start_idx : start_idx + max_results]
        return entries

    def get_bank_accounts(self) -> List[BankAccount]:
        query = "SELECT * FROM Account WHERE AccountType = 'Bank' MAXRESULTS 1000"
        query_response = self._query_response(query)

        accounts: List[BankAccount] = []
        for acc in _qbo_query_entity_list(query_response, "Account"):
            try:
                balance = acc.get("CurrentBalance")
                if balance is not None:
                    balance = float(balance)
                currency_ref = acc.get("CurrencyRef")
                currency_code = currency_ref.get("value") if isinstance(currency_ref, dict) else None
                accounts.append(
                    BankAccount(
                        id=str(acc["Id"]),
                        name=acc.get("Name", ""),
                        account_type=acc.get("AccountType", "Bank"),
                        account_sub_type=acc.get("AccountSubType"),
                        current_balance=balance,
                        currency_code=currency_code,
                    )
                )
            except (KeyError, TypeError) as e:
                logger.warning("Skip account: %s", e)
        return accounts

    def get_transfers(
        self,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        from_account_id: Optional[str] = None,
        to_account_id: Optional[str] = None,
        max_results: int = 100,
        start_position: int = 1,
    ) -> List[TransferEntry]:
        query = "SELECT * FROM Transfer"
        conditions: List[str] = []
        filter_after_query = bool(from_account_id or to_account_id)
        fetch_cap = (
            min(max((max(0, start_position - 1) + max_results) * 4, max_results), 1000)
            if filter_after_query
            else max_results
        )
        query_start_position = 1 if filter_after_query else start_position
        if start_date and end_date:
            start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
            end_str = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)
            conditions.append(f"TxnDate >= '{start_str}' AND TxnDate <= '{end_str}'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDERBY TxnDate DESC"
        query += f" STARTPOSITION {query_start_position} MAXRESULTS {fetch_cap}"

        query_response = self._query_response(query)

        entries: List[TransferEntry] = []
        for t in _qbo_query_entity_list(query_response, "Transfer"):
            try:
                from_ref = t.get("FromAccountRef", {})
                to_ref = t.get("ToAccountRef", {})
                from_id = str(from_ref.get("value", ""))
                to_id = str(to_ref.get("value", ""))
                if from_account_id and from_id != from_account_id:
                    continue
                if to_account_id and to_id != to_account_id:
                    continue
                entries.append(
                    TransferEntry(
                        id=str(t["Id"]),
                        date=datetime.strptime(t["TxnDate"], "%Y-%m-%d").date(),
                        amount=float(t.get("Amount", 0)),
                        from_account_id=from_id,
                        from_account_name=from_ref.get("name", ""),
                        to_account_id=to_id,
                        to_account_name=to_ref.get("name", ""),
                        private_note=t.get("PrivateNote"),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skip transfer %s: %s", t.get("Id"), e)
        if filter_after_query:
            start_idx = max(0, start_position - 1)
            return entries[start_idx : start_idx + max_results]
        return entries

    def get_bank_money_out_transactions(
        self,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        from_account_id: Optional[str] = None,
        to_account_id: Optional[str] = None,
        max_results: int = 100,
        start_position: int = 1,
    ) -> List[BankOutEntry]:
        """Money leaving bank accounts: Transfer plus Purchase and Expense paid from a Bank account."""
        bank_accounts = self.get_bank_accounts()
        bank_id_set: set[str] = {a.id for a in bank_accounts}
        if from_account_id:
            if from_account_id not in bank_id_set:
                return []
            bank_id_set = {from_account_id}

        per_type_cap = max(max_results * 3, max_results)

        transfers = self.get_transfers(
            start_date=start_date,
            end_date=end_date,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            max_results=per_type_cap,
            start_position=1,
        )

        merged: List[BankOutEntry] = []
        for t in transfers:
            merged.append(
                BankOutEntry(
                    id=str(t.id),
                    date=t.date,
                    amount=t.amount,
                    transaction_type="transfer",
                    bank_account_id=t.from_account_id,
                    bank_account_name=t.from_account_name,
                    private_note=t.private_note,
                    payee_name=None,
                    category=t.to_account_name,
                    to_account_id=t.to_account_id or None,
                    to_account_name=t.to_account_name or None,
                )
            )

        date_where = ""
        if start_date and end_date:
            start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
            end_str = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)
            date_where = f" WHERE TxnDate >= '{start_str}' AND TxnDate <= '{end_str}'"

        # QuickBooks UI "Expense" and "Check" records are exposed by the QBO API as Purchase records.
        for entity_name, tname in (("Purchase", "purchase"),):
            query = f"SELECT * FROM {entity_name}{date_where} ORDERBY TxnDate DESC STARTPOSITION 1 MAXRESULTS {per_type_cap}"
            query_response = self._query_response(query)
            for txn in _qbo_query_entity_list(query_response, entity_name):
                try:
                    acct_ref = txn.get("AccountRef") or {}
                    if not isinstance(acct_ref, dict):
                        continue
                    pay_from_id = str(acct_ref.get("value", ""))
                    if pay_from_id not in bank_id_set:
                        continue
                    lines_raw = txn.get("Line")
                    ent_ref = txn.get("EntityRef") or {}
                    payee = ent_ref.get("name") if isinstance(ent_ref, dict) else None
                    desc = _txn_primary_description(txn, lines_raw)
                    merged.append(
                        BankOutEntry(
                            id=str(txn["Id"]),
                            date=datetime.strptime(txn["TxnDate"], "%Y-%m-%d").date(),
                            amount=float(txn.get("TotalAmt", 0)),
                            transaction_type=tname,
                            bank_account_id=pay_from_id,
                            bank_account_name=str(acct_ref.get("name", "")),
                            private_note=desc or None,
                            payee_name=payee,
                            category=_expense_categories_from_lines(lines_raw),
                            to_account_id=None,
                            to_account_name=None,
                        )
                    )
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning("Skip %s %s: %s", entity_name, txn.get("Id"), e)

        query = f"SELECT * FROM BillPayment{date_where} ORDERBY TxnDate DESC STARTPOSITION 1 MAXRESULTS {per_type_cap}"
        query_response = self._query_response(query)
        for txn in _qbo_query_entity_list(query_response, "BillPayment"):
            try:
                pay_from_id, pay_from_name = _bill_payment_bank_account_ref(txn)
                if pay_from_id not in bank_id_set:
                    continue
                vendor_id, vendor_name = _ref_value_name(txn.get("VendorRef"))
                merged.append(
                    BankOutEntry(
                        id=str(txn["Id"]),
                        date=datetime.strptime(txn["TxnDate"], "%Y-%m-%d").date(),
                        amount=float(txn.get("TotalAmt", 0)),
                        transaction_type="bill_payment",
                        bank_account_id=pay_from_id,
                        bank_account_name=pay_from_name,
                        private_note=txn.get("PrivateNote") or txn.get("DocNumber"),
                        payee_name=vendor_name or vendor_id or None,
                        category="BillPayment",
                        to_account_id=None,
                        to_account_name=None,
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skip BillPayment %s: %s", txn.get("Id"), e)

        merged.sort(key=lambda e: (e.date, e.id), reverse=True)
        start_idx = max(0, start_position - 1)
        return merged[start_idx : start_idx + max_results]

    def get_bank_credit_transactions(
        self,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        account_id: Optional[str] = None,
        external_transaction_id: Optional[str] = None,
        max_results: int = 100,
        start_position: int = 1,
    ) -> List[BankCreditEntry]:
        """Incoming credit candidates across Deposit, incoming Transfer, Payment, and SalesReceipt."""
        conditions: List[str] = []
        if start_date and end_date:
            start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
            end_str = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)
            conditions.append(f"TxnDate >= '{start_str}' AND TxnDate <= '{end_str}'")

        fetch_cap = min(max((max(0, start_position - 1) + max_results) * 2, max_results), 1000)
        merged: List[BankCreditEntry] = []

        def run_query(entity_name: str, extra_conditions: Optional[List[str]] = None) -> List[dict]:
            entity_conditions = list(conditions)
            if extra_conditions:
                entity_conditions.extend(extra_conditions)
            query = f"SELECT * FROM {entity_name}"
            if entity_conditions:
                query += " WHERE " + " AND ".join(entity_conditions)
            query += f" ORDERBY TxnDate DESC STARTPOSITION 1 MAXRESULTS {fetch_cap}"
            return _qbo_query_entity_list(self._query_response(query), entity_name)

        for deposit in run_query("Deposit"):
            if not _matches_external_id(deposit, external_transaction_id):
                continue
            try:
                account_ref = deposit.get("DepositToAccountRef")
                account_id_value, account_name = _ref_value_name(account_ref)
                if account_id and account_id_value != account_id:
                    continue
                lines_raw = deposit.get("Line")
                merged.append(
                    BankCreditEntry(
                        id=str(deposit["Id"]),
                        date=_txn_date(deposit),
                        amount=float(deposit.get("TotalAmt", 0)),
                        transaction_type="deposit",
                        bank_account_id=account_id_value or None,
                        bank_account_name=account_name or None,
                        description=deposit.get("PrivateNote") or deposit.get("Memo"),
                        category=_first_deposit_category(lines_raw) or None,
                        doc_number=deposit.get("DocNumber"),
                        linked_txn_ids=_linked_txn_ids(lines_raw),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skip deposit credit %s: %s", deposit.get("Id"), e)

        for transfer in run_query("Transfer"):
            if not _matches_external_id(transfer, external_transaction_id):
                continue
            try:
                to_id, to_name = _ref_value_name(transfer.get("ToAccountRef"))
                if account_id and to_id != account_id:
                    continue
                from_id, from_name = _ref_value_name(transfer.get("FromAccountRef"))
                merged.append(
                    BankCreditEntry(
                        id=str(transfer["Id"]),
                        date=_txn_date(transfer),
                        amount=float(transfer.get("Amount", 0)),
                        transaction_type="transfer_in",
                        bank_account_id=to_id or None,
                        bank_account_name=to_name or None,
                        description=transfer.get("PrivateNote"),
                        counterparty_name=from_name or None,
                        category=from_name or from_id or None,
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skip incoming transfer credit %s: %s", transfer.get("Id"), e)

        for entity_name, transaction_type in (("Payment", "payment"), ("SalesReceipt", "sales_receipt")):
            for txn in run_query(entity_name):
                if not _matches_external_id(txn, external_transaction_id):
                    continue
                try:
                    account_id_value, account_name = _ref_value_name(txn.get("DepositToAccountRef"))
                    if account_id and account_id_value != account_id:
                        continue
                    customer_id, customer_name = _ref_value_name(txn.get("CustomerRef"))
                    lines_raw = txn.get("Line")
                    merged.append(
                        BankCreditEntry(
                            id=str(txn["Id"]),
                            date=_txn_date(txn),
                            amount=float(txn.get("TotalAmt", 0)),
                            transaction_type=transaction_type,
                            bank_account_id=account_id_value or None,
                            bank_account_name=account_name or None,
                            description=txn.get("PrivateNote") or txn.get("Memo"),
                            counterparty_name=customer_name or customer_id or None,
                            category=_expense_categories_from_lines(lines_raw),
                            doc_number=txn.get("DocNumber"),
                            payment_ref_num=txn.get("PaymentRefNum"),
                            linked_txn_ids=_linked_txn_ids(lines_raw),
                        )
                    )
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning("Skip %s credit %s: %s", entity_name, txn.get("Id"), e)

        merged.sort(key=lambda e: (e.date, e.id, e.transaction_type), reverse=True)
        start_idx = max(0, start_position - 1)
        return merged[start_idx : start_idx + max_results]

    def get_company_info(self) -> CompanyInfo:
        self._ensure_initialized()
        result = self._make_request(f"companyinfo/{self.realm_id}")
        info = result.get("CompanyInfo")
        if not isinstance(info, dict):
            raise QuickBooksAPIError("QuickBooks companyinfo response missing CompanyInfo")
        email = info.get("Email")
        return CompanyInfo(
            realm_id=str(self.realm_id or ""),
            environment=str(self.credentials.get("environment")) if self.credentials else None,
            company_name=info.get("CompanyName"),
            legal_name=info.get("LegalName"),
            country=info.get("Country"),
            email=email.get("Address") if isinstance(email, dict) else None,
        )
