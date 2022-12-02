from __future__ import annotations

import voluptuous as vol
from typing import Any
import homeassistant.helpers.config_validation as cv
from .const import (
    DOMAIN, 
    CONF_BLUEPRINTS,
    CONF_SWITCH_CONFIGS,
    CONF_MANAGED_SWITCHES,
    CONF_STORE,
    LOGGER
)
from .store import SwitchManagerStore
from .helpers import load_blueprints, load_manifest, deploy_blueprints, check_blueprints_folder_exists
from .view import async_setup_view
from .models import ( Blueprint, ManagedSwitchConfig )

from homeassistant.core import Config, HomeAssistant
from homeassistant.components import websocket_api
from homeassistant.helpers import config_validation as cv
from homeassistant.config import _format_config_error
from homeassistant.helpers.script import SCRIPT_MODE_CHOICES, DEFAULT_SCRIPT_MODE

CONDITION_SCHEMA = vol.Schema({
    vol.Required('key'): cv.string,
    vol.Required('value'): cv.string,
})
BLUEPRINT_ACTION_SCHEMA = vol.Schema({
    vol.Required('title'): cv.string,
    vol.Optional('conditions'): vol.All(cv.ensure_list, [CONDITION_SCHEMA])
})
BLUEPRINT_BUTTON_SCHEMA = vol.Schema({
    vol.Required('actions'): vol.All(cv.ensure_list, [BLUEPRINT_ACTION_SCHEMA]),
    vol.Optional('conditions'): vol.All(cv.ensure_list, [CONDITION_SCHEMA]),
    vol.Optional('shape', default='rect'): vol.In(['rect','circle','path']),
    vol.Optional('x'): cv.positive_int,
    vol.Optional('y'): cv.positive_int,
    vol.Optional('width'): cv.positive_int,
    vol.Optional('height'): cv.positive_int,
    vol.Optional('d'): cv.string,
})
BLUEPRINT_SCHEMA = vol.Schema({
    vol.Required('name'): cv.string,
    vol.Required('service'): cv.string,
    vol.Required('event_type'): cv.string,
    vol.Required('identifier_key'): cv.string,
    vol.Optional('conditions'): vol.All(cv.ensure_list, [CONDITION_SCHEMA]),
    vol.Required('buttons'): vol.All(cv.ensure_list, [BLUEPRINT_BUTTON_SCHEMA])
})

SWITCH_MANAGER_CONFIG_ACTION_SCHEMA = vol.Schema({
    vol.Required('mode', default=DEFAULT_SCRIPT_MODE): vol.In(SCRIPT_MODE_CHOICES),
    vol.Required('sequence', default=[]): cv.SCRIPT_SCHEMA
})
SWITCH_MANAGER_CONFIG_BUTTON_SCHEMA = vol.Schema({
    vol.Required('actions'): vol.All(cv.ensure_list, [SWITCH_MANAGER_CONFIG_ACTION_SCHEMA])
})
SWITCH_MANAGER_CONFIG_SCHEMA = vol.Schema({
    vol.Required('id', default=None): vol.Any(str, int, None),
    vol.Required('name'): cv.string,
    vol.Required('enabled', default=True): bool,
    vol.Required('blueprint'): cv.string,
    vol.Required('identifier'): cv.string,
    vol.Required('buttons'): vol.All(cv.ensure_list, [SWITCH_MANAGER_CONFIG_BUTTON_SCHEMA])
}, extra=vol.ALLOW_EXTRA)

# CONFIG_SCHEMA = vol.Schema({DOMAIN: SWITCH_MANAGER_SCHEMA}, extra=vol.ALLOW_EXTRA)

async def async_setup( hass: HomeAssistant, config: Config ):
    """Set up is called when Home Assistant is loading our component."""
    
    hass.data.setdefault(DOMAIN, {})

    hass.data[DOMAIN] = {
        CONF_BLUEPRINTS: {},
        CONF_SWITCH_CONFIGS: {},
        CONF_MANAGED_SWITCHES: {},
        CONF_STORE: SwitchManagerStore(hass)
    }
    # Init hass storage
    await hass.data[DOMAIN][CONF_STORE].load()

    await async_migrate(hass)

    _init_blueprints(hass)
    await _init_switch_configs(hass)

    
    # Return boolean to indicate that initialization was successful.
    return True

async def async_setup_entry( hass, config_entry ):
    await async_setup_view(hass)

    websocket_api.async_register_command(hass, websocket_configs)
    websocket_api.async_register_command(hass, websocket_blueprints)
    websocket_api.async_register_command(hass, websocket_save_config)
    websocket_api.async_register_command(hass, websocket_toggle_config_enabled)
    websocket_api.async_register_command(hass, websocket_delete_config)

    return True

async def async_migrate( hass ):
    # Opening JSON file
    manifest = await load_manifest()
    store = hass.data[DOMAIN][CONF_STORE]
    if not store.compare_version( manifest['version'] ):
        LOGGER.debug('Migrating blueprints')
        await deploy_blueprints( hass )
        await store.update_version( manifest['version'] )
        return True
    elif not await check_blueprints_folder_exists( hass ):
        await deploy_blueprints( hass )

    return False

def _init_blueprints( hass: HomeAssistant ):
    # Ensure blueprints empty for clean state
    blueprints = hass.data[DOMAIN][CONF_BLUEPRINTS] = {}
    for config in load_blueprints(hass):
        try:
            c_validated = BLUEPRINT_SCHEMA(config.get('data'))
        except vol.Invalid as ex:
            LOGGER.error(_format_config_error(ex, f"{DOMAIN} {CONF_BLUEPRINTS}({config.get('id')})", config))
            continue
        blueprints[config.get('id')] = Blueprint(config.get('id'), c_validated, config.get('has_image'))

def _get_blueprint( hass: HomeAssistant, id: str ) -> Blueprint:
    return hass.data[DOMAIN][CONF_BLUEPRINTS].get(id, id)

async def _init_switch_configs( hass: HomeAssistant ):
    switches = await hass.data[DOMAIN][CONF_STORE].get_managed_switches()
    for _id in switches:
        switch = ManagedSwitchConfig( 
            hass, 
            _get_blueprint( hass, switches[_id].get('blueprint') ), 
            _id, 
            switches[_id] 
        )
        _set_switch_config( hass, switch )

def _set_switch_config( hass: HomeAssistant, config: ManagedSwitchConfig ):
    hass.data[DOMAIN][CONF_MANAGED_SWITCHES][config.id] = config
    config.start();

def _get_switch_config( hass: HomeAssistant, _id: str ) -> ManagedSwitchConfig:
    return hass.data[DOMAIN][CONF_MANAGED_SWITCHES].get(_id)

def _remove_switch_config( hass: HomeAssistant, _id: str ):
    hass.data[DOMAIN][CONF_MANAGED_SWITCHES][_id].stop()
    del hass.data[DOMAIN][CONF_MANAGED_SWITCHES][_id]

@websocket_api.websocket_command({
    vol.Required("type"): "switch_manager/blueprints", 
    vol.Optional("blueprint_id"): cv.string
})
@websocket_api.async_response
async def websocket_blueprints(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = { "blueprint": _get_blueprint(hass, msg['blueprint_id'] ) } if msg.get('blueprint_id') \
        else { "blueprints": hass.data[DOMAIN].get(CONF_BLUEPRINTS) }

    connection.send_result( msg["id"], data )

@websocket_api.websocket_command({
    vol.Required("type"): "switch_manager/configs", 
    vol.Optional("config_id"): cv.string
})
@websocket_api.async_response
async def websocket_configs(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    data = { "config":_get_switch_config(hass, msg['config_id'] ) } if msg.get('config_id') \
        else { "configs": hass.data[DOMAIN].get(CONF_MANAGED_SWITCHES) }

    connection.send_result( msg["id"], data )

@websocket_api.websocket_command({
    vol.Required("type"): "switch_manager/config/save", 
    vol.Required('config'): SWITCH_MANAGER_CONFIG_SCHEMA
})
@websocket_api.async_response
async def websocket_save_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    config: ManagedSwitchConfig
    store = hass.data[DOMAIN][CONF_STORE]

    if msg['config'].get('id'):
        config = _get_switch_config( hass, msg['config'].get('id') )
        config.update( msg['config'] )
    else:
        config = ManagedSwitchConfig( 
            hass, 
            _get_blueprint( hass, msg['config']['blueprint'] ), 
            store.get_available_id(), 
            msg['config'] 
        )
        _set_switch_config( hass, config )

    await store.set_managed_switch( config )    
    
    connection.send_result( msg['id'], {
        "config_id": config.id,
        "config": config
    })

@websocket_api.websocket_command({
    vol.Required("type"): "switch_manager/config/enabled", 
    vol.Required("config_id"): cv.string,
    vol.Required("enabled"): bool
})
@websocket_api.async_response
async def websocket_toggle_config_enabled(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    store = hass.data[DOMAIN][CONF_STORE]

    config = _get_switch_config( hass, msg['config_id'] )
    config.setEnabled( msg['enabled'] )
    config.start()

    await store.set_managed_switch( config )

    connection.send_result(msg['id'], {
        "switch_id": config.id,
        "enabled": msg['enabled']
    })

@websocket_api.websocket_command({
    vol.Required("type"): "switch_manager/config/delete", 
    vol.Optional("config_id"): cv.string
})
@websocket_api.async_response
async def websocket_delete_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    store = hass.data[DOMAIN][CONF_STORE]

    _remove_switch_config( hass, msg['config_id'] )    
    await store.delete_managed_switch( msg['config_id'] )
    
    connection.send_result( msg['id'], {
        "deleted": msg['config_id']
    })