"""Allowlisted Micra controls and limits reported by its own dashboard."""
import math
from functools import partial

from pylamarzocco.const import PreExtractionMode, SteamTargetLevel, WidgetType


class InvalidSetting(ValueError):
    """Fixed, credential-free validation error."""


NUMERIC_LIMITS = {
    'steam_level': (1, 3, 1),
    'coffee_temperature': (80, 100, 0.5),
    'prebrew_on': (1, 9, 0.5),
    'prebrew_off': (1, 9, 0.5),
}


def number_in_range(value, minimum, maximum, step):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum and step > 0
            and math.isclose((value - minimum) / step, round((value - minimum) / step), abs_tol=1e-7))


def validate_input(setting, value):
    if setting == 'steam_enabled':
        if type(value) is not bool:
            raise InvalidSetting('expected_boolean')
    elif setting == 'prebrew_mode':
        if value not in ('PreBrewing', 'Disabled'):
            raise InvalidSetting('unsupported_prebrew_mode')
    elif isinstance(setting, str) and setting in NUMERIC_LIMITS:
        if not number_in_range(value, *NUMERIC_LIMITS[setting]):
            raise InvalidSetting('value_outside_supported_range_or_step')
    else:
        raise InvalidSetting('unsupported_setting')


def controls(widgets):
    result = {}
    steam = widgets.get(WidgetType.CM_STEAM_BOILER_LEVEL)
    if steam is not None:
        levels = {SteamTargetLevel.LEVEL_1: 1, SteamTargetLevel.LEVEL_2: 2, SteamTargetLevel.LEVEL_3: 3}
        result['steam'] = {'enabled': steam.enabled, 'level': levels.get(steam.target_level),
                           'enabled_supported': steam.enabled_supported,
                           'level_supported': steam.target_level_supported}
    coffee = widgets.get(WidgetType.CM_COFFEE_BOILER)
    if coffee is not None:
        result['coffee'] = {'target': coffee.target_temperature, 'minimum': coffee.target_temperature_min,
                            'maximum': coffee.target_temperature_max, 'step': coffee.target_temperature_step}
    prebrew = widgets.get(WidgetType.CM_PRE_BREWING)
    if prebrew is not None and not prebrew.dose_index_supported and len(prebrew.times.pre_brewing) == 1:
        times = prebrew.times.pre_brewing[0]
        result['prebrew'] = {'mode': str(prebrew.mode),
            'available_modes': [str(mode) for mode in prebrew.available_modes],
            'on': times.seconds.seconds_in, 'off': times.seconds.seconds_out,
            'on_minimum': times.seconds_min.seconds_in, 'on_maximum': times.seconds_max.seconds_in,
            'on_step': times.seconds_step.seconds_in,
            'off_minimum': times.seconds_min.seconds_out, 'off_maximum': times.seconds_max.seconds_out,
            'off_step': times.seconds_step.seconds_out}
    return result


def prepare_command(machine, settings, setting, value):
    validate_input(setting, value)
    if setting.startswith('steam_'):
        steam = settings.get('steam')
        supported = 'enabled_supported' if setting == 'steam_enabled' else 'level_supported'
        if not steam or not steam[supported]:
            raise InvalidSetting('steam_control_unavailable')
        if setting == 'steam_enabled':
            return partial(machine.set_steam, value)
        return partial(machine.set_steam_level, SteamTargetLevel(f'Level{int(value)}'))
    if setting == 'coffee_temperature':
        coffee = settings.get('coffee')
        if not coffee or not number_in_range(value, coffee['minimum'], coffee['maximum'], coffee['step']):
            raise InvalidSetting('temperature_outside_machine_limits')
        return partial(machine.set_coffee_target_temperature, float(value))
    prebrew = settings.get('prebrew')
    if not prebrew:
        raise InvalidSetting('prebrew_control_unavailable')
    if setting == 'prebrew_mode':
        if value not in prebrew['available_modes']:
            raise InvalidSetting('prebrew_mode_unavailable')
        return partial(machine.set_pre_extraction_mode, PreExtractionMode(value))
    if prebrew['mode'] != 'PreBrewing':
        raise InvalidSetting('enable_prebrew_before_changing_times')
    field = 'on' if setting == 'prebrew_on' else 'off'
    if not number_in_range(value, prebrew[field+'_minimum'], prebrew[field+'_maximum'], prebrew[field+'_step']):
        raise InvalidSetting('prebrew_time_outside_machine_limits')
    # Read/modify/write under the backend lock, preserving the OTHER time from a fresh read.
    return partial(machine.set_pre_extraction_times,
                   float(value) if field == 'on' else prebrew['on'],
                   float(value) if field == 'off' else prebrew['off'])
