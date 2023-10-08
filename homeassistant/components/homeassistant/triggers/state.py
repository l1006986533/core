"""Offer state listening automation rules."""
from __future__ import annotations

from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant import exceptions
from homeassistant.const import CONF_ATTRIBUTE, CONF_FOR, CONF_PLATFORM, MATCH_ALL
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, State, callback
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    template,
)
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_same_state,
    async_track_state_change_event,
    process_state_match,
)
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType, EventType

_LOGGER = logging.getLogger(__name__)

CONF_ENTITY_ID = "entity_id"
CONF_FROM = "from"
CONF_TO = "to"
CONF_NOT_FROM = "not_from"
CONF_NOT_TO = "not_to"

BASE_SCHEMA = cv.TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_PLATFORM): "state",
        vol.Required(CONF_ENTITY_ID): cv.entity_ids_or_uuids,
        vol.Optional(CONF_FOR): cv.positive_time_period_template,
        vol.Optional(CONF_ATTRIBUTE): cv.match_all,
    }
)

TRIGGER_STATE_SCHEMA = BASE_SCHEMA.extend(
    {
        # These are str on purpose. Want to catch YAML conversions
        vol.Exclusive(CONF_FROM, CONF_FROM): vol.Any(str, [str], None),
        vol.Exclusive(CONF_NOT_FROM, CONF_FROM): vol.Any(str, [str], None),
        vol.Exclusive(CONF_TO, CONF_TO): vol.Any(str, [str], None),
        vol.Exclusive(CONF_NOT_TO, CONF_TO): vol.Any(str, [str], None),
    }
)

TRIGGER_ATTRIBUTE_SCHEMA = BASE_SCHEMA.extend(
    {
        vol.Exclusive(CONF_FROM, CONF_FROM): cv.match_all,
        vol.Exclusive(CONF_NOT_FROM, CONF_FROM): cv.match_all,
        vol.Exclusive(CONF_TO, CONF_TO): cv.match_all,
        vol.Exclusive(CONF_NOT_TO, CONF_TO): cv.match_all,
    }
)


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate trigger config."""
    if not isinstance(config, dict):
        raise vol.Invalid("Expected a dictionary")

    # We use this approach instead of vol.Any because
    # this gives better error messages.
    if CONF_ATTRIBUTE in config:
        config = TRIGGER_ATTRIBUTE_SCHEMA(config)
    else:
        config = TRIGGER_STATE_SCHEMA(config)

    registry = er.async_get(hass)
    config[CONF_ENTITY_ID] = er.async_validate_entity_ids(
        registry, cv.entity_ids_or_uuids(config[CONF_ENTITY_ID])
    )

    return config


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
    *,
    platform_type: str = "state",
) -> CALLBACK_TYPE:
    """Listen for state changes based on configuration."""
    entity_ids = config[CONF_ENTITY_ID]
    match_from_state = get_match_state(config, CONF_FROM, CONF_NOT_FROM)
    match_to_state = get_match_state(config, CONF_TO, CONF_NOT_TO)
    time_delta = config.get(CONF_FOR)
    template.attach(hass, time_delta)
    match_all = all(
        item not in config for item in (CONF_FROM, CONF_NOT_FROM, CONF_NOT_TO, CONF_TO)
    )
    unsub_track_same = {}
    period: dict[str, timedelta] = {}
    attribute = config.get(CONF_ATTRIBUTE)
    job = HassJob(action, f"state trigger {trigger_info}")

    trigger_data = trigger_info["trigger_data"]
    _variables = trigger_info["variables"] or {}

    @callback
    def state_automation_listener(event: EventType[EventStateChangedData]) -> None:
        old_value, new_value = get_state_values(event, attribute)
        
        if not should_trigger(old_value, new_value, match_from_state, match_to_state, match_all, attribute):
            return
        
        handle_trigger(hass, event, old_value, new_value, job, trigger_data, platform_type, attribute, time_delta, period, config, _variables, unsub_track_same, trigger_info)

    unsub = async_track_state_change_event(hass, entity_ids, state_automation_listener)

    @callback
    def async_remove():
        """Remove state listeners async."""
        remove_listeners(unsub, unsub_track_same)

    return async_remove

def get_match_state(config, from_conf, not_from_conf):
    if (state_value := config.get(from_conf)) is not None:
        return process_state_match(state_value)
    elif (not_state_value := config.get(not_from_conf)) is not None:
        return process_state_match(not_state_value, invert=True)
    else:
        return process_state_match(MATCH_ALL)

def get_state_values(event, attribute):
    from_s = event.data["old_state"]
    to_s = event.data["new_state"]
    old_value = get_value(from_s, attribute)
    new_value = get_value(to_s, attribute)
    return old_value, new_value

def get_value(state, attribute):
    if state is None:
        return None
    elif attribute is None:
        return state.state
    else:
        return state.attributes.get(attribute)

def should_trigger(old_value, new_value, match_from_state, match_to_state, match_all, attribute):
    if attribute is not None and old_value == new_value:
        return False
    return match_from_state(old_value) and match_to_state(new_value) and (match_all or old_value != new_value)

def handle_trigger(hass, event, old_value, new_value, job, trigger_data, platform_type, attribute, time_delta, period, config, _variables, unsub_track_same, trigger_info):
    @callback
    def call_action():
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_data,
                    "platform": platform_type,
                    "entity_id": event.data["entity_id"],
                    "from_state": old_value,
                    "to_state": new_value,
                    "for": time_delta if not time_delta else period[event.data["entity_id"]],
                    "attribute": attribute,
                    "description": f"state of {event.data['entity_id']}",
                }
            },
            event.context,
        )

    if not time_delta:
        call_action()
        return

    update_period(hass, old_value, new_value, time_delta, _variables, event, config, period, call_action, unsub_track_same, trigger_info)

def update_period(hass, old_value, new_value, attribute, time_delta, _variables, event, config, period, call_action, unsub_track_same, trigger_info):
    entity = event.data["entity_id"]
    from_s = event.data["old_state"]
    to_s = event.data["new_state"]
    
    data = {
        "trigger": {
            "platform": "state",
            "entity_id": entity,
            "from_state": from_s,
            "to_state": to_s,
        }
    }
    variables = {**_variables, **data}
    
    try:
        period[entity] = cv.positive_time_period(
            template.render_complex(time_delta, variables)
        )
    except (exceptions.TemplateError, vol.Invalid) as ex:
        _LOGGER.error(
            "Error rendering '%s' for template: %s", trigger_info["name"], ex
        )
        return
    
    def _check_same_state(_, _2, new_st: State | None) -> bool:
        if new_st is None:
            return False

        cur_value: str | None
        if attribute is None:
            cur_value = new_st.state
        else:
            cur_value = new_st.attributes.get(attribute)

        if CONF_FROM in config and CONF_TO not in config:
            return cur_value != old_value

        return cur_value == new_value
    
    unsub_track_same[entity] = async_track_same_state(
        hass,
        period[entity],
        call_action,
        _check_same_state,
        entity_ids=entity,
    )


def remove_listeners(unsub, unsub_track_same):
    unsub()
    for async_remove in unsub_track_same.values():
        async_remove()
    unsub_track_same.clear()
