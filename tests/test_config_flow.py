"""Config flow: one entry, a second attempt aborts."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

DOMAIN = "roborock_live_map"


async def _start(hass):
    # Dependencies (core Roborock, http, frontend) are not under test.
    with patch("homeassistant.config_entries.async_process_deps_reqs"), patch(
        "homeassistant.config_entries.ConfigEntries.async_setup", return_value=True
    ):
        return await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )


async def test_single_entry_created_with_unique_id(hass):
    result = await _start(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roborock Live Map"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1 and entries[0].unique_id == DOMAIN


async def test_second_entry_aborts(hass):
    assert (await _start(hass))["type"] is FlowResultType.CREATE_ENTRY
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
