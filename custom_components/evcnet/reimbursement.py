"""Opt-in 50five mobile reimbursement source, isolated from portal 2FA cookies."""

from dataclasses import dataclass, field
from datetime import date, datetime
import math
import time
from typing import Any

import aiohttp
from yarl import URL

from .utils import parse_locale_number

# Client ID of the 50five mobile app, sent as the plugz-application-id header.
# Same value as in nikagl/homeassistant-evcnet (MIT), where it is documented as
# taken from the mobile app's JWT payload. Not an official public API ID.
FIFTYFIVE_APPLICATION_ID = "dd6ee167-17d0-4bea-877d-9156bd5917dd"
REFRESH_SECONDS = 900
INVOICE_REFRESH_SECONDS = 6 * 3600
REIMBURSEMENT_BILLING_TYPES = frozenset({"Reimbursement", "ReimbursementPayout"})

_LOGIN_QUERY = (
    "mutation L($e:String!,$p:String!)"
    "{loginFiftyFive(email:$e,password:$p){access_token}}"
)
_TARIFF_QUERY = (
    "query($id:ID!){getChargeStationById(id:$id)"
    "{homeChargingCompensation{hccEnabled hccTariff}}}"
)
_INVOICES_QUERY = (
    "query($f:InvoiceFilter,$s:InvoiceSort)"
    "{getInvoices(page:1,itemsPerPage:30,filters:$f,sort:$s){items{"
    "id date billingType status vatPercentage amountInclVat "
    "transactions{items{totalEnergy chargeStation{id}}}}}}"
)


class ReimbursementError(Exception):
    """The optional mobile tariff source could not supply a current value."""


@dataclass(frozen=True)
class ReimbursementData:
    """Current reimbursement data, distinct from transaction or charging prices."""

    tariff: float | None = None
    source: str = "unavailable"
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RawInvoice:
    """One reimbursement invoice for the whole account."""

    invoice_id: str
    invoice_date: date
    status: str | None
    vat_percent: float | None
    amount: float
    station_energy: dict[str, tuple[float, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class InvoiceSummary:
    """A reimbursement invoice, attributed to one charge spot."""

    invoice_date: date
    amount: float
    energy_kwh: float
    sessions: int
    vat_percent: float | None
    status: str | None

    @property
    def paid_rate(self) -> float | None:
        """Amount actually paid per kWh on this invoice."""
        if self.energy_kwh <= 0:
            return None
        return round(self.amount / self.energy_kwh, 4)


@dataclass(frozen=True)
class InvoiceData:
    """Invoice-derived reimbursement figures for one charge spot."""

    latest: InvoiceSummary | None = None
    year: int | None = None
    ytd_amount: float | None = None
    ytd_energy_kwh: float = 0.0
    ytd_invoices: int = 0
    vat_percent: float | None = None
    updated_at: datetime | None = None


def parse_invoices(items: Any) -> list[RawInvoice]:
    """Keep reimbursement invoices only, newest first, skipping malformed ones."""
    invoices: list[RawInvoice] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("billingType") not in REIMBURSEMENT_BILLING_TYPES:
            continue
        raw_date = item.get("date")
        amount = item.get("amountInclVat")
        if not isinstance(raw_date, str) or not isinstance(amount, (int, float)):
            continue
        try:
            invoice_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        energy: dict[str, tuple[float, int]] = {}
        tx = item.get("transactions")
        for t in (tx.get("items") if isinstance(tx, dict) else None) or []:
            if not isinstance(t, dict):
                continue
            station = t.get("chargeStation")
            station_id = str(station.get("id")) if isinstance(station, dict) else ""
            kwh = t.get("totalEnergy")
            kwh = float(kwh) if isinstance(kwh, (int, float)) and kwh > 0 else 0.0
            total, count = energy.get(station_id, (0.0, 0))
            energy[station_id] = (total + kwh, count + 1)
        vat = item.get("vatPercentage")
        invoices.append(
            RawInvoice(
                invoice_id=str(item.get("id", "")),
                invoice_date=invoice_date,
                status=item.get("status")
                if isinstance(item.get("status"), str)
                else None,
                vat_percent=float(vat)
                if isinstance(vat, (int, float)) and 0 <= vat <= 100
                else None,
                amount=abs(float(amount)),
                station_energy=energy,
            )
        )
    invoices.sort(key=lambda inv: inv.invoice_date, reverse=True)
    return invoices


def summarize_invoices(
    invoices: list[RawInvoice],
    spot_id: str,
    today: date,
    updated_at: datetime | None = None,
) -> InvoiceData:
    """Attribute account invoices to one spot by its share of the charged energy."""
    summaries: list[InvoiceSummary] = []
    for inv in invoices:
        total_kwh = sum(kwh for kwh, _ in inv.station_energy.values())
        spot_kwh, sessions = inv.station_energy.get(str(spot_id), (0.0, 0))
        if total_kwh > 0:
            share = spot_kwh / total_kwh
        else:
            # No energy breakdown: only attribute when no other station is named.
            others = set(inv.station_energy) - {str(spot_id), ""}
            share = 0.0 if others else 1.0
        if share <= 0:
            continue
        summaries.append(
            InvoiceSummary(
                invoice_date=inv.invoice_date,
                amount=round(inv.amount * share, 2),
                energy_kwh=round(spot_kwh, 3),
                sessions=sessions,
                vat_percent=inv.vat_percent,
                status=inv.status,
            )
        )
    this_year = [s for s in summaries if s.invoice_date.year == today.year]
    vat = next(
        (inv.vat_percent for inv in invoices if inv.vat_percent is not None), None
    )
    return InvoiceData(
        latest=summaries[0] if summaries else None,
        year=today.year,
        ytd_amount=round(sum(s.amount for s in this_year), 2),
        ytd_energy_kwh=round(sum(s.energy_kwh for s in this_year), 3),
        ytd_invoices=len(this_year),
        vat_percent=vat,
        updated_at=updated_at,
    )


def valid_endpoint(value: str) -> bool:
    """Require an explicitly configured HTTPS endpoint without URL credentials."""
    try:
        url = URL(value)
        return bool(
            url.scheme == "https"
            and url.host
            and url.user is None
            and not url.query_string
            and not url.fragment
            and url.path.endswith("/graphql")
        )
    except (TypeError, ValueError):
        return False


def tariff_number(value: Any) -> float | None:
    """Parse a nonnegative finite tariff without turning missing values into zero."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.removesuffix("EUR").strip()
    parsed = parse_locale_number(value, default=None)
    if parsed is None or not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


class MobileReimbursementClient:
    """Read-only tariff queries through an explicitly trusted mobile endpoint."""

    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Keep mobile tokens in memory; never reuse or modify portal cookies."""
        if not valid_endpoint(endpoint):
            raise ValueError("Invalid reimbursement endpoint")
        self._endpoint = endpoint
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = None
        self._token_expires = 0.0
        self._retry_after: dict[str, float] = {}

    async def _request(self, body: dict[str, Any], *, authenticated: bool) -> dict:
        """Reject redirects and redact errors rather than logging response bodies."""
        headers = {"plugz-application-id": FIFTYFIVE_APPLICATION_ID}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.post(
                self._endpoint,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    if response.status in (401, 403):
                        self._token = None
                    raise ReimbursementError("Mobile tariff request rejected")
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ReimbursementError("Mobile tariff endpoint unavailable") from err
        if not isinstance(payload, dict) or payload.get("errors"):
            self._token = None
            raise ReimbursementError("Mobile tariff query or authentication failed")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ReimbursementError("Mobile tariff response has no data")
        return data

    async def _ensure_token(self) -> None:
        """Log in when needed; stop on auth/2FA errors without challenge bypasses."""
        if self._token and time.monotonic() < self._token_expires:
            return
        data = await self._request(
            {
                "query": _LOGIN_QUERY,
                "variables": {"e": self._username, "p": self._password},
            },
            authenticated=False,
        )
        login = data.get("loginFiftyFive")
        token = login.get("access_token") if isinstance(login, dict) else None
        if not isinstance(token, str) or not token:
            raise ReimbursementError("Mobile authentication needs user attention")
        self._token = token
        self._token_expires = time.monotonic() + 1800

    def _check_cooldown(self, operation: str) -> None:
        if time.monotonic() < self._retry_after.get(operation, 0.0):
            raise ReimbursementError("Mobile source is in retry cooldown")

    def _start_cooldown(self, operation: str) -> None:
        self._retry_after[operation] = time.monotonic() + REFRESH_SECONDS

    async def get_tariff(self, spot_id: str) -> float:
        """Fetch the home charging compensation tariff for one charge station."""
        self._check_cooldown("tariff")
        try:
            await self._ensure_token()
            data = await self._request(
                {"query": _TARIFF_QUERY, "variables": {"id": spot_id}},
                authenticated=True,
            )
            station = data.get("getChargeStationById")
            hcc = (
                station.get("homeChargingCompensation")
                if isinstance(station, dict)
                else None
            )
            if not isinstance(hcc, dict):
                raise ReimbursementError("Mobile reimbursement data missing")  # noqa: TRY301
            if hcc.get("hccEnabled") is False:
                return 0.0
            tariff = tariff_number(hcc.get("hccTariff"))
            if hcc.get("hccEnabled") is not True or tariff is None:
                raise ReimbursementError("Mobile reimbursement tariff invalid")  # noqa: TRY301
        except ReimbursementError:
            self._start_cooldown("tariff")
            raise
        return tariff

    async def get_invoices(self, date_from: date) -> list[RawInvoice]:
        """Fetch reimbursement invoices since date_from, newest first."""
        self._check_cooldown("invoices")
        try:
            await self._ensure_token()
            data = await self._request(
                {
                    "query": _INVOICES_QUERY,
                    "variables": {
                        "f": {"dateFrom": date_from.isoformat()},
                        "s": {"invoiceDate": "desc"},
                    },
                },
                authenticated=True,
            )
            result = data.get("getInvoices")
            if not isinstance(result, dict):
                raise ReimbursementError("Mobile invoice data missing")  # noqa: TRY301
        except ReimbursementError:
            self._start_cooldown("invoices")
            raise
        return parse_invoices(result.get("items"))
