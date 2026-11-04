from __future__ import annotations

from .client import LingxingClient, extract_rows

SELLER_LIST = "/erp/sc/data/seller/lists"
ORDER_PROFIT = "/basicOpen/finance/mreport/OrderProfit"
FBA_AGE_LIST = "/erp/sc/routing/fba/fbaStock/getFbaAgeList"
RECEIVED_INVENTORY = "/erp/sc/data/fba_report/receivedInventory"
STORAGE_FEE_MONTH = "/erp/sc/data/fba_report/storageFeeMonth"
LISTING = "/erp/sc/data/mws/listing"
CURRENCY_MONTH = "/erp/sc/routing/finance/currency/currencyMonth"


class LingxingApi:
    def __init__(self, client: LingxingClient, page_length: dict[str, int]):
        self.client = client
        self.page_length = page_length

    def _len(self, path: str) -> int:
        return self.page_length.get(path, 1000)

    def sellers(self) -> list[dict]:
        payload = self.client.request("GET", SELLER_LIST, {})
        return extract_rows(payload)[0]

    def order_profit(self, sids: list[int], start: str, end: str) -> list[dict]:
        return self.client.paged(
            ORDER_PROFIT,
            {"sids": [int(s) for s in sids], "startDate": start, "endDate": end, "currencyCode": "CNY"},
            self._len(ORDER_PROFIT),
        )

    def fba_age_list(self, sid: int) -> list[dict]:
        return self.client.paged(FBA_AGE_LIST, {"sid": int(sid)}, self._len(FBA_AGE_LIST))

    def received_inventory(self, sid: int, start_date: str, end_date: str) -> list[dict]:
        return self.client.paged(
            RECEIVED_INVENTORY,
            {"sid": int(sid), "start_date": start_date, "end_date": end_date},
            self._len(RECEIVED_INVENTORY),
        )

    def storage_fee_month(self, sid: int, month: str) -> list[dict]:
        return self.client.paged(STORAGE_FEE_MONTH, {"sid": int(sid), "month": month}, self._len(STORAGE_FEE_MONTH))

    def listing(self, sid: int) -> list[dict]:
        return self.client.paged(LISTING, {"sid": str(sid)}, self._len(LISTING))

    def currency_month(self, month: str) -> list[dict]:
        payload = self.client.request("POST", CURRENCY_MONTH, {"date": month})
        return extract_rows(payload)[0]
