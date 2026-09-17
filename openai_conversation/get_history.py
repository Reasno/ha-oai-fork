"""Read-only history tool for the Home Assistant LLM API."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

_MAX_DAYS = 30
_MAX_ENTITIES = 10
_ASSISTANT = "conversation"


def _number(value: str) -> float | None:
    """Return a finite numeric state, if possible."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _trend(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the change between the first and last daily values."""
    if not points:
        return {"direction": "unknown"}

    first_state = points[0]["state"]
    last_state = points[-1]["state"]
    first_number = _number(first_state)
    last_number = _number(last_state)
    if first_number is None or last_number is None:
        return {
            "direction": "changed" if first_state != last_state else "unchanged",
            "first": first_state,
            "last": last_state,
        }

    delta = last_number - first_number
    return {
        "direction": "up" if delta > 0 else "down" if delta < 0 else "unchanged",
        "first": first_number,
        "last": last_number,
        "delta": delta,
    }


class GetHistoryTool(llm.Tool):
    """Return daily history for conversation-exposed entities."""

    name = "get_history"
    description = (
        "Get read-only Home Assistant entity history, grouped by local calendar day. "
        "Only entities exposed to Assist can be queried."
    )
    parameters = vol.Schema(
        {
            vol.Required("entity_ids"): vol.All(
                cv.ensure_list,
                vol.Length(min=1, max=_MAX_ENTITIES),
                [cv.entity_id],
            ),
            vol.Optional("days", default=7): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=_MAX_DAYS)
            ),
            vol.Optional("aggregation", default="daily_last"): vol.In(
                ("daily_last",)
            ),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict[str, Any]:
        """Query Recorder without changing any Home Assistant state."""
        entity_ids = list(dict.fromkeys(tool_input.tool_args["entity_ids"]))
        if len(entity_ids) > _MAX_ENTITIES:
            raise HomeAssistantError(
                f"At most {_MAX_ENTITIES} unique entities may be queried"
            )

        denied = [
            entity_id
            for entity_id in entity_ids
            if not async_should_expose(hass, _ASSISTANT, entity_id)
        ]
        if denied:
            raise HomeAssistantError(
                "History access denied for entities not exposed to Assist: "
                + ", ".join(denied)
            )

        missing = [
            entity_id
            for entity_id in entity_ids
            if hass.states.get(entity_id) is None
        ]
        if missing:
            raise HomeAssistantError("Unknown entities: " + ", ".join(missing))

        days = tool_input.tool_args["days"]
        end_time = dt_util.utcnow()
        local_today = dt_util.as_local(end_time).date()
        start_local = dt_util.start_of_local_day(
            local_today - timedelta(days=days - 1)
        )
        start_time = dt_util.as_utc(start_local)

        states_by_entity = await get_instance(hass).async_add_executor_job(
            partial(
                history.get_significant_states,
                hass,
                start_time,
                end_time,
                entity_ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=True,
            )
        )

        result: dict[str, Any] = {}
        for entity_id in entity_ids:
            daily: dict[str, State] = {}
            for state in states_by_entity.get(entity_id, []):
                if not isinstance(state, State):
                    continue
                local_day = dt_util.as_local(state.last_changed).date().isoformat()
                daily[local_day] = state

            points = [
                {
                    "date": day,
                    "state": state.state,
                    "last_changed": state.last_changed.isoformat(),
                }
                for day, state in sorted(daily.items())
            ]
            current = hass.states.get(entity_id)
            result[entity_id] = {
                "unit": (
                    current.attributes.get("unit_of_measurement") if current else None
                ),
                "daily_last": points,
                "trend": _trend(
                    [
                        point
                        for point in points
                        if point["state"] not in ("unknown", "unavailable")
                    ]
                ),
            }

        return {
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "timezone": str(dt_util.DEFAULT_TIME_ZONE),
            "aggregation": "daily_last",
            "entities": result,
        }
