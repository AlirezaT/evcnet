"""Tests for the reimbursement step in the config and options flows."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.evcnet.config_flow import EvcNetConfigFlow, EvcNetOptionsFlow
from custom_components.evcnet.const import DEFAULT_MOBILE_ENDPOINT

LOGIN = {
    "base_url": "https://example.com",
    "username": "User@Example.com",
    "password": "secret",
}
ENABLED = {
    "mobile_reimbursement_enabled": True,
    "mobile_reimbursement_endpoint": DEFAULT_MOBILE_ENDPOINT,
    "reimbursement_vat_enabled": True,
    "reimbursement_vat_percent": 6.0,
}


def _defaults(result: Any) -> dict[str, Any]:
    """Return the default value of each field in a shown form."""
    return {
        str(key): key.default()
        for key in result["data_schema"].schema
        if callable(getattr(key, "default", None))
    }


def _flow() -> EvcNetConfigFlow:
    flow = EvcNetConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = None
    flow.context = {"source": "user"}
    return flow


@pytest.mark.asyncio
async def test_setup_asks_reimbursement_with_production_endpoint_prefilled() -> None:
    """After login, setup shows the API step with the endpoint prefilled, off by default."""
    flow = _flow()

    result = await flow._async_login_done(dict(LOGIN))  # noqa: SLF001

    assert result["type"] == "form"
    assert result["step_id"] == "reimbursement"
    defaults = _defaults(result)
    assert defaults["mobile_reimbursement_endpoint"] == DEFAULT_MOBILE_ENDPOINT
    assert defaults["mobile_reimbursement_enabled"] is False
    assert defaults["reimbursement_vat_enabled"] is False


@pytest.mark.asyncio
async def test_setup_stores_reimbursement_choices_as_options() -> None:
    """The entry keeps login data in data and the API/VAT choices in options."""
    flow = _flow()
    await flow._async_login_done(dict(LOGIN))  # noqa: SLF001

    result = await flow.async_step_reimbursement(dict(ENABLED))

    assert result["type"] == "create_entry"
    assert result["title"] == LOGIN["username"]
    assert result["data"] == LOGIN
    assert result["options"] == ENABLED


@pytest.mark.asyncio
async def test_setup_rejects_invalid_endpoint_only_when_enabled() -> None:
    """A bad address blocks setup only if the mobile API is switched on."""
    flow = _flow()
    await flow._async_login_done(dict(LOGIN))  # noqa: SLF001
    bad = {**ENABLED, "mobile_reimbursement_endpoint": "http://example.com/graphql"}

    result = await flow.async_step_reimbursement(bad)
    assert result["type"] == "form"
    assert result["errors"] == {
        "mobile_reimbursement_endpoint": "invalid_mobile_endpoint"
    }

    result = await flow.async_step_reimbursement(
        {**bad, "mobile_reimbursement_enabled": False}
    )
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
async def test_reimbursement_step_without_login_aborts() -> None:
    """The step cannot be reached without a completed login."""
    result = await _flow().async_step_reimbursement()
    assert result["type"] == "abort"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({}, DEFAULT_MOBILE_ENDPOINT),
        ({"mobile_reimbursement_endpoint": ""}, DEFAULT_MOBILE_ENDPOINT),
        (
            {"mobile_reimbursement_endpoint": "https://other.example/graphql"},
            "https://other.example/graphql",
        ),
    ],
)
async def test_options_prefill_endpoint(stored: dict[str, str], expected: str) -> None:
    """Options prefill the production endpoint unless one is already saved."""
    flow = EvcNetOptionsFlow()
    entry = SimpleNamespace(options=stored)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(EvcNetOptionsFlow, "config_entry", entry)
        result = await flow.async_step_init()

    assert _defaults(result)["mobile_reimbursement_endpoint"] == expected


@pytest.mark.asyncio
async def test_options_keep_card_and_channel_selections() -> None:
    """Saving reimbursement options preserves other stored options."""
    flow = EvcNetOptionsFlow()
    entry = SimpleNamespace(options={"selected_card_ids": {"1": "card"}})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(EvcNetOptionsFlow, "config_entry", entry)
        result = await flow.async_step_init(dict(ENABLED))

    assert result["type"] == "create_entry"
    assert result["data"] == {"selected_card_ids": {"1": "card"}, **ENABLED}
