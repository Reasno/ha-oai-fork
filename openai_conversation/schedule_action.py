"""Silent persistent delayed service actions for the Home Assistant LLM API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import ulid as ulid_util

_LOGGER = logging.getLogger(__name__)

_ASSISTANT = "conversation"
_STORAGE_KEY = "openai_conversation.schedule_actions"
_STORAGE_VERSION = 1
_MAX_DELAY_SECONDS = 7 * 24 * 60 * 60
_MAX_ENTITIES = 10
_DATA_KEY = "openai_conversation_schedule_action_manager"

# Keep this tool limited to ordinary entity actions. Administrative, notification,
# automation, script, shell, and event services are intentionally excluded.
_ALLOWED_SERVICES = {
    "close_cover",
    "lock",
    "open_cover",
    "press",
    "set_cover_position",
    "set_fan_mode",
    "set_hvac_mode",
    "set_percentage",
    "set_preset_mode",
    "set_temperature",
    "stop_cover",
    "toggle",
    "turn_off",
    "turn_on",
    "unlock",
}


class ScheduleActionManager:
    """Persist and execute silent delayed Home Assistant service calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORAGE_VERSION, _STORAGE_KEY
        )
        self._actions: dict[str, dict[str, Any]] = {}
        self._unsub: dict[str, Callable[[], None]] = {}

    async def async_setup(self) -> None:
        """Restore pending actions after a Home Assistant restart."""
        stored = await self._store.async_load() or {}
        self._actions = stored.get("actions", {})
        now = dt_util.utcnow()
        for action_id, action in list(self._actions.items()):
            execute_at = dt_util.parse_datetime(action["execute_at"])
            if execute_at is None:
                self._actions.pop(action_id, None)
                continue
            self._schedule(action_id, max(execute_at, now))
        await self._async_save()

    async def async_add(
        self,
        delay_seconds: int,
        domain: str,
        service: str,
        entity_ids: list[str],
        service_data: dict[str, Any],
    ) -> tuple[str, datetime]:
        """Add and persist a delayed action."""
        action_id = ulid_util.ulid_now()
        execute_at = dt_util.utcnow() + timedelta(seconds=delay_seconds)
        self._actions[action_id] = {
            "execute_at": execute_at.isoformat(),
            "domain": domain,
            "service": service,
            "entity_ids": entity_ids,
            "service_data": service_data,
        }
        await self._async_save()
        self._schedule(action_id, execute_at)
        return action_id, execute_at

    @callback
    def _schedule(self, action_id: str, execute_at: datetime) -> None:
        """Schedule one action in the HA event loop."""
        self._unsub[action_id] = async_track_point_in_utc_time(
            self.hass,
            lambda now: self.hass.async_create_task(
                self._async_execute(action_id),
                f"Scheduled LLM action {action_id}",
            ),
            execute_at,
        )

    async def _async_execute(self, action_id: str) -> None:
        """Execute one action without any announcement."""
        self._unsub.pop(action_id, None)
        action = self._actions.pop(action_id, None)
        if action is None:
            return

        # Remove it before calling the service so a restart cannot execute it twice.
        await self._async_save()
        data = dict(action["service_data"])
        data["entity_id"] = action["entity_ids"]
        try:
            await self.hass.services.async_call(
                action["domain"],
                action["service"],
                data,
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Scheduled LLM action %s failed", action_id)

    async def _async_save(self) -> None:
        """Persist pending actions."""
        await self._store.async_save({"actions": self._actions})


def async_get_manager(hass: HomeAssistant) -> ScheduleActionManager:
    """Return the integration-wide schedule action manager."""
    return hass.data[_DATA_KEY]


async def async_setup_manager(hass: HomeAssistant) -> None:
    """Set up the integration-wide schedule action manager once."""
    if _DATA_KEY in hass.data:
        return
    manager = ScheduleActionManager(hass)
    hass.data[_DATA_KEY] = manager
    await manager.async_setup()


class ScheduleActionTool(llm.Tool):
    """Schedule a silent delayed service call."""

    name = "schedule_action"
    description = (
        "Schedule an exposed Home Assistant entity action to run silently after a "
        "delay. Use this for commands such as turning off a device in 30 minutes or "
        "changing a thermostat later. The action executes without an announcement."
    )
    parameters = vol.Schema(
        {
            vol.Required("delay_seconds"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=_MAX_DELAY_SECONDS)
            ),
            vol.Required("domain"): cv.string,
            vol.Required("service"): vol.In(_ALLOWED_SERVICES),
            vol.Required("entity_ids"): vol.All(
                cv.ensure_list,
                vol.Length(min=1, max=_MAX_ENTITIES),
                [cv.entity_id],
            ),
            vol.Optional("service_data", default={}): vol.Schema(
                {}, extra=vol.ALLOW_EXTRA
            ),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict[str, Any]:
        """Validate and schedule a silent delayed action."""
        args = tool_input.tool_args
        domain = args["domain"]
        service = args["service"]
        entity_ids = list(dict.fromkeys(args["entity_ids"]))

        if len(entity_ids) > _MAX_ENTITIES:
            raise HomeAssistantError(
                f"At most {_MAX_ENTITIES} unique entities may be scheduled"
            )
        if domain == "homeassistant":
            if service not in {"turn_on", "turn_off", "toggle"}:
                raise HomeAssistantError(
                    "The homeassistant domain only supports turn_on, turn_off, or toggle"
                )
        elif any(entity_id.split(".", 1)[0] != domain for entity_id in entity_ids):
            raise HomeAssistantError("Service domain must match every entity domain")

        denied = [
            entity_id
            for entity_id in entity_ids
            if not async_should_expose(hass, _ASSISTANT, entity_id)
        ]
        if denied:
            raise HomeAssistantError(
                "Scheduled action denied for entities not exposed to Assist: "
                + ", ".join(denied)
            )

        missing = [
            entity_id for entity_id in entity_ids if hass.states.get(entity_id) is None
        ]
        if missing:
            raise HomeAssistantError("Unknown entities: " + ", ".join(missing))
        if not hass.services.has_service(domain, service):
            raise HomeAssistantError(f"Unknown service: {domain}.{service}")

        action_id, execute_at = await async_get_manager(hass).async_add(
            args["delay_seconds"],
            domain,
            service,
            entity_ids,
            dict(args["service_data"]),
        )
        return {
            "success": True,
            "action_id": action_id,
            "execute_at": execute_at.isoformat(),
            "silent": True,
        }
