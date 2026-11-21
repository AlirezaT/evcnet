# ruff: noqa: SLF001
"""Tests for reimbursement invoices, automatic VAT and invoice sensors."""

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.evcnet.coordinator import EvcNetCoordinator
from custom_components.evcnet.reimbursement import (
    InvoiceData,
    MobileReimbursementClient,
    RawInvoice,
    ReimbursementError,
    parse_invoices,
    summarize_invoices,
)
from custom_components.evcnet.sensor import SENSOR_TYPES
from homeassistant.util import dt as dt_util

SPOT = "1234733026"
SENSORS = {d.key: d for d in SENSOR_TYPES}


def _invoice(
    day: str,
    amount: float,
    tx: list[tuple[str, float]],
    billing: str = "Reimbursement",
    vat: Any = 6,
    status: str = "Paid",
) -> dict[str, Any]:
    return {
        "id": day,
        "date": f"{day}T00:00:00+01:00",
        "billingType": billing,
        "status": status,
        "vatPercentage": vat,
        "amountInclVat": -amount,
        "transactions": {
            "items": [
                {"totalEnergy": kwh, "chargeStation": {"id": station}}
                for station, kwh in tx
            ]
        },
    }


def test_parse_keeps_reimbursements_newest_first() -> None:
    """Non-reimbursement and malformed invoices are dropped; amounts are positive."""
    raw = parse_invoices(
        [
            _invoice("2026-10-10", 55.7, [(SPOT, 172.8)]),
            _invoice("2026-11-10", 69.58, [(SPOT, 100.0), (SPOT, 115.7)]),
            _invoice("2026-11-01", 12.0, [(SPOT, 5.0)], billing="Regular"),
            {"date": None, "billingType": "Reimbursement"},
            "garbage",
        ]
    )

    assert [r.invoice_date for r in raw] == [date(2026, 11, 10), date(2026, 10, 10)]
    assert raw[0].amount == 69.58
    assert raw[0].station_energy == {SPOT: (215.7, 2)}
    assert raw[0].vat_percent == 6.0


def test_parse_ignores_invalid_vat() -> None:
    """VAT outside 0-100 or non-numeric is treated as unknown."""
    raw = parse_invoices(
        [
            _invoice("2026-11-10", 10, [(SPOT, 1)], vat=150),
            _invoice("2026-10-10", 10, [(SPOT, 1)], vat="6"),
        ]
    )
    assert [r.vat_percent for r in raw] == [None, None]


def test_summarize_latest_paid_rate_ytd_and_vat() -> None:
    """The newest invoice drives the sensors; year to date only counts this year."""
    raw = parse_invoices(
        [
            _invoice("2026-11-10", 69.58, [(SPOT, 215.7)], status="Sent"),
            _invoice("2026-10-10", 55.7, [(SPOT, 172.8)]),
            _invoice("2025-12-15", 22.27, [(SPOT, 72.5)]),
        ]
    )

    data = summarize_invoices(raw, SPOT, date(2026, 11, 21))

    assert data.latest is not None
    assert data.latest.invoice_date == date(2026, 11, 10)
    assert data.latest.amount == 69.58
    assert data.latest.status == "Sent"
    assert data.latest.paid_rate == 0.3226
    assert data.ytd_amount == 125.28
    assert data.ytd_invoices == 2
    assert data.ytd_energy_kwh == 388.5
    assert data.vat_percent == 6.0


def test_summarize_splits_amount_by_energy_share() -> None:
    """With several chargers on one invoice, each gets its share of the amount."""
    raw = parse_invoices(
        [_invoice("2026-11-10", 100.0, [(SPOT, 30.0), ("other", 70.0)])]
    )

    mine = summarize_invoices(raw, SPOT, date(2026, 11, 21))
    other = summarize_invoices(raw, "other", date(2026, 11, 21))
    absent = summarize_invoices(raw, "third", date(2026, 11, 21))

    assert mine.latest.amount == 30.0
    assert other.latest.amount == 70.0
    assert absent.latest is None
    assert absent.ytd_amount == 0.0


def test_summarize_without_energy_breakdown() -> None:
    """Invoices without session data count for a spot unless another one is named."""
    no_tx = parse_invoices([_invoice("2026-11-10", 50.0, [])])
    other_only = parse_invoices([_invoice("2026-11-10", 50.0, [("other", 0.0)])])

    assert summarize_invoices(no_tx, SPOT, date(2026, 11, 21)).latest.amount == 50.0
    assert summarize_invoices(no_tx, SPOT, date(2026, 11, 21)).latest.paid_rate is None
    assert summarize_invoices(other_only, SPOT, date(2026, 11, 21)).latest is None


def test_summarize_empty() -> None:
    """No invoices yet: nothing to report and no VAT."""
    data = summarize_invoices([], SPOT, date(2026, 1, 3))
    assert (data.latest, data.ytd_amount, data.vat_percent) == (None, 0.0, None)


class _Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> Any:
        return self._payload


def _client(*responses: _Response) -> tuple[MobileReimbursementClient, MagicMock]:
    session = MagicMock()
    session.post.side_effect = list(responses)
    client = MobileReimbursementClient(
        "https://api.example.com/graphql", "user", "secret", session
    )
    return client, session


def _login() -> _Response:
    return _Response({"data": {"loginFiftyFive": {"access_token": "tok"}}})


@pytest.mark.asyncio
async def test_client_requests_newest_invoices_since_date() -> None:
    """The invoice query is sorted newest first and filtered by date."""
    client, session = _client(
        _login(),
        _Response(
            {"data": {"getInvoices": {"items": [_invoice("2026-11-10", 69.58, [])]}}}
        ),
    )

    invoices = await client.get_invoices(date(2025, 1, 1))

    variables = session.post.call_args_list[1].kwargs["json"]["variables"]
    assert variables == {"f": {"dateFrom": "2025-01-01"}, "s": {"invoiceDate": "desc"}}
    assert [i.amount for i in invoices] == [69.58]


@pytest.mark.asyncio
async def test_invoice_failure_does_not_block_tariff() -> None:
    """Invoices and tariff have separate retry cooldowns and share one login."""
    hcc = _Response(
        {
            "data": {
                "getChargeStationById": {
                    "homeChargingCompensation": {"hccEnabled": True, "hccTariff": 0.3}
                }
            }
        }
    )
    client, session = _client(_login(), _Response({"data": {"getInvoices": None}}), hcc)

    with pytest.raises(ReimbursementError):
        await client.get_invoices(date(2025, 1, 1))
    with pytest.raises(ReimbursementError, match="cooldown"):
        await client.get_invoices(date(2025, 1, 1))
    assert await client.get_tariff(SPOT) == 0.3
    assert session.post.call_count == 3


def _coordinator(client: Any = None, vat: float | None = None) -> EvcNetCoordinator:
    coordinator = object.__new__(EvcNetCoordinator)
    coordinator._reimbursement_client = client
    coordinator._reimbursement_vat_percent = vat
    coordinator._invoices = None
    coordinator._invoices_updated = None
    coordinator._invoices_warned = False
    return coordinator


@pytest.mark.parametrize(
    ("fixed", "invoice_vat", "expected"),
    [
        (21.0, 6.0, (21.0, "user_configured")),
        (None, 6.0, (6.0, "reimbursement_invoice")),
        (None, None, (None, None)),
        (0.0, 6.0, (0.0, "user_configured")),
    ],
)
def test_effective_vat_priority(
    fixed: float | None, invoice_vat: float | None, expected: tuple
) -> None:
    """A fixed rate wins; otherwise the invoice rate is used."""
    coordinator = _coordinator(vat=fixed)
    assert coordinator._effective_vat(InvoiceData(vat_percent=invoice_vat)) == expected


@pytest.mark.asyncio
async def test_invoices_refresh_cached_and_kept_on_error() -> None:
    """Invoices refresh every few hours; a failure keeps the last good list."""
    first = [RawInvoice("1", date(2026, 11, 10), "Paid", 6.0, 69.58)]
    client = MagicMock()
    client.get_invoices = AsyncMock(side_effect=[first, ReimbursementError("down")])
    coordinator = _coordinator(client)

    await coordinator._async_refresh_invoices()
    await coordinator._async_refresh_invoices()
    assert client.get_invoices.await_count == 1
    assert client.get_invoices.await_args.args[0] == date(dt_util.now().year - 1, 1, 1)

    coordinator._invoices_updated -= timedelta(hours=7)
    await coordinator._async_refresh_invoices()
    assert client.get_invoices.await_count == 2
    assert coordinator._invoices == first
    assert coordinator._invoices_warned is True


@pytest.mark.asyncio
async def test_no_invoice_fetch_without_mobile_api() -> None:
    """Invoices are only read when the mobile API is enabled."""
    coordinator = _coordinator(None)
    await coordinator._async_refresh_invoices()
    assert coordinator._invoices is None


def test_invoice_sensors() -> None:
    """Sensors report the latest invoice, paid rate and year to date."""
    raw = parse_invoices([_invoice("2026-11-10", 69.58, [(SPOT, 215.7)])])
    now = dt_util.utcnow()
    data = SimpleNamespace(
        invoices=summarize_invoices(raw, SPOT, date(2026, 11, 21), now),
        reimbursement_vat_percent=6.0,
        reimbursement_vat_source="reimbursement_invoice",
    )

    assert SENSORS["last_reimbursement_invoice"].value_fn(data) == 69.58
    attrs = SENSORS["last_reimbursement_invoice"].attributes_fn(data)
    assert attrs["invoice_date"] == "2026-11-10"
    assert attrs["paid_rate_per_kwh"] == 0.3226
    assert SENSORS["reimbursement_paid_rate"].value_fn(data) == 0.3226
    assert SENSORS["reimbursement_year_to_date"].value_fn(data) == 69.58
    assert SENSORS["vat_rate"].attributes_fn(data)["vat_source"] == (
        "reimbursement_invoice"
    )


def test_invoice_sensors_unknown_before_first_fetch() -> None:
    """Before invoices are fetched, the sensors are unknown, not zero."""
    data = SimpleNamespace(
        invoices=InvoiceData(),
        reimbursement_vat_percent=None,
        reimbursement_vat_source=None,
    )
    assert SENSORS["last_reimbursement_invoice"].value_fn(data) is None
    assert SENSORS["reimbursement_paid_rate"].value_fn(data) is None
    assert SENSORS["reimbursement_year_to_date"].value_fn(data) is None
