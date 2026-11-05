import os
import sys
import time
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tide_nodes as tn  # noqa: E402

SUB_ID = '8500001'   # fixture id for a subordinate station (not a real station)

CATALOG = [
    {'id': SUB_ID, 'name': 'Noyack Bay', 'lat': 40.995, 'lng': -72.3317, 'state': 'NY', 'type': 'S', 'ref': '8461490'},
    {'id': '8510560', 'name': 'Montauk', 'lat': 41.0483, 'lng': -71.9594, 'state': 'NY', 'type': 'R', 'ref': ''},
    {'id': '8510000', 'name': 'Montauk Harbor', 'lat': 41.07, 'lng': -71.93, 'state': 'NY', 'type': 'S', 'ref': '8510560'},
    {'id': '1', 'name': 'Springfield', 'lat': 42.1, 'lng': -72.6, 'state': 'MA', 'type': 'R', 'ref': ''},
    {'id': '2', 'name': 'Springfield', 'lat': 38.0, 'lng': -90.0, 'state': 'NY', 'type': 'R', 'ref': ''},
    {'id': 'TWC0419', 'name': 'Test Point', 'lat': 27.0, 'lng': -97.0, 'state': 'TX', 'type': 'S', 'ref': ''},
]

OBS_CAT = [
    {'id': '8510560', 'name': 'Montauk', 'lat': 41.0483, 'lng': -71.9594, 'state': 'NY',
     'sensors': ['water_level', 'water_temperature', 'air_temperature', 'wind', 'air_pressure']},
    {'id': '8461490', 'name': 'New London', 'lat': 41.3717, 'lng': -72.0956, 'state': 'CT', 'sensors': ['water_level']},
    {'id': '9999999', 'name': 'Far Away', 'lat': 25.0, 'lng': -80.0, 'state': 'FL', 'sensors': ['water_level']},
]

# Sample NOAA high/low predictions for a subordinate station. Test fixtures only.
GMT = [('2026-10-31 01:54', '0.093', 'L'), ('2026-10-31 07:51', '2.203', 'H'), ('2026-10-31 13:54', '0.416', 'L'),
       ('2026-10-31 20:23', '2.809', 'H'), ('2026-11-01 02:54', '0.161', 'L'), ('2026-11-01 08:56', '2.198', 'H'),
       ('2026-11-01 15:02', '0.462', 'L'), ('2026-11-01 21:29', '2.653', 'H'), ('2026-11-02 03:54', '0.197', 'L'),
       ('2026-11-02 10:05', '2.244', 'H')]
LOCAL = [('2026-10-31 03:51', '2.203', 'H'), ('2026-10-31 09:54', '0.416', 'L'), ('2026-10-31 16:23', '2.809', 'H'),
         ('2026-10-31 22:54', '0.161', 'L'), ('2026-11-01 03:56', '2.198', 'H'), ('2026-11-01 10:02', '0.462', 'L'),
         ('2026-11-01 16:29', '2.653', 'H'), ('2026-11-01 22:54', '0.197', 'L'), ('2026-11-02 05:05', '2.244', 'H')]


class FakePoly(object):
    def db_getNodeDrivers(self, addr=None, init=False):
        return []


def _rows(items):
    return [{'t': t, 'v': v, 'type': k} for t, v, k in items]


def _events():
    return tn.build_events(_rows(GMT), _rows(LOCAL), fallback_off=-14400)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_resolve_by_id_case_insensitive():
    assert tn.resolve_station(SUB_ID, CATALOG)[0] == 'id'
    kind, found, _ = tn.resolve_station('twc0419', CATALOG)
    assert kind == 'id' and found[0]['id'] == 'TWC0419'


def test_resolve_by_name():
    kind, found, _ = tn.resolve_station('Noyack Bay', CATALOG)
    assert kind == 'name' and found[0]['id'] == SUB_ID
    kind, found, _ = tn.resolve_station('noyack', CATALOG)
    assert kind == 'name' and found[0]['id'] == SUB_ID


def test_exact_name_beats_partial():
    kind, found, _ = tn.resolve_station('Montauk', CATALOG)
    assert kind == 'name' and found[0]['id'] == '8510560'


def test_ambiguous_and_state_filter():
    kind, found, _ = tn.resolve_station('mont', CATALOG)
    assert kind == 'ambiguous' and len(found) == 2
    assert tn.resolve_station('Springfield', CATALOG)[0] == 'ambiguous'
    kind, found, _ = tn.resolve_station('Springfield, MA', CATALOG)
    assert kind == 'name' and found[0]['id'] == '1'


def test_not_found_and_invalid():
    assert tn.resolve_station('zzzz', CATALOG)[0] == 'notfound'
    assert tn.resolve_station('', CATALOG)[0] == 'invalid'
    assert tn.resolve_station('95,10', CATALOG)[0] == 'invalid'


def test_resolve_latlon_nearest():
    kind, found, _ = tn.resolve_station('40.99, -72.33', CATALOG)
    assert kind == 'nearest'
    assert found[0]['id'] == SUB_ID
    assert 'dist_nm' in found[0] and len(found) <= 5
    assert found[0]['dist_nm'] <= found[1]['dist_nm']


def test_haversine_noyack_to_montauk():
    d = tn.haversine_nm(40.995, -72.3317, 41.0483, -71.9594)
    assert 15 < d < 19


def test_choose_obs_nearby_within_50nm():
    meta = CATALOG[0]
    src, kind, dist, note = tn.choose_obs_station(meta, OBS_CAT, '')
    assert src['id'] == '8510560' and kind == 2 and 15 < dist < 19 and note == ''


def test_choose_obs_same_station():
    src, kind, dist, _ = tn.choose_obs_station(CATALOG[1], OBS_CAT, '')
    assert src['id'] == '8510560' and kind == 1 and dist == 0.0


def test_choose_obs_override_none_and_explicit_and_bad():
    meta = CATALOG[0]
    assert tn.choose_obs_station(meta, OBS_CAT, 'none')[0] is None
    src, kind, _, _ = tn.choose_obs_station(meta, OBS_CAT, '8461490')
    assert src['id'] == '8461490' and kind == 2
    src, kind, _, note = tn.choose_obs_station(meta, OBS_CAT, 'bogus')
    assert src['id'] == '8510560' and note


def test_choose_obs_nothing_within_50nm():
    meta = {'id': 'x', 'name': 'Remote', 'lat': 60.0, 'lng': -150.0, 'state': 'AK', 'type': 'R'}
    assert tn.choose_obs_station(meta, OBS_CAT, '')[0] is None


def test_build_events_skips_bad_rows():
    rows = [{'t': 'garbage', 'v': '1', 'type': 'H'}, {'t': '2026-11-01 08:56', 'v': 'x', 'type': 'H'}]
    assert tn.build_events(rows, []) == []


def test_pred_status_values():
    now = 1000000.0
    assert tn.pred_status({'events': [], 'pred_err': ''}, now) == 0
    assert tn.pred_status({'events': [], 'pred_err': 'net: x'}, now) == 3
    assert tn.pred_status({'events': [{'epoch': now + 99999}], 'fetched': now}, now) == 1
    assert tn.pred_status({'events': [{'epoch': now - 5}], 'fetched': now}, now) == 2
    assert tn.pred_status({'events': [{'epoch': now + 99999}], 'fetched': now - 49 * 3600}, now) == 2


def test_parse_obs_wind():
    rows = [{'t': '2026-11-01 15:30', 's': '6.61', 'd': '295.0', 'dr': 'WNW', 'g': '9.52', 'f': '0,0'}]
    rec = tn.parse_obs('wind', rows)
    assert rec['spd'] == 6.61 and rec['dir'] == 295.0 and rec['dr'] == 'WNW' and rec['gust'] == 9.52


def test_obs_keys_only_for_sensors_that_returned_data():
    t = {'obs_cache': {'8510560': {'data': {'water_level': {'epoch': 1, 'v': 2.0},
                                            'wind': {'epoch': 1, 'spd': 5.0, 'dir': None, 'gust': 7.0}}}}}
    st = {'obs': {'src': {'id': '8510560'}, 'kind': 2}, 'events': [1]}
    keys = tn.obs_keys(t, st)
    assert keys == {'ST', 'TIDELVL', 'OBS_AGE', 'SPEED', 'GUST'}
    st['obs']['kind'] = 1
    assert 'SURGE' in tn.obs_keys(t, st)


def test_conversions():
    assert tn.conv_ht(10, True) == 3.05 and tn.conv_ht(10, False) == 10
    assert tn.conv_temp(212, True) == 100.0 and tn.conv_temp(59.2, False) == 59.2
    assert tn.conv_wind(10, False) == 11.5 and tn.conv_wind(10, True) == 5.1
    assert tn.conv_pres(1013.25, False) == 29.92 and tn.conv_pres(1013.25, True) == 1013.2
    assert tn.conv_dist(10, True) == 18.5 and tn.conv_dist(10, False) == 11.5
    assert tn.uoms(False) == {'ht': 18, 'temp': 17, 'wind': 48, 'pres': 23, 'dist': 116}
    assert tn.uoms(True) == {'ht': 38, 'temp': 4, 'wind': 40, 'pres': 117, 'dist': 83}


def test_parse_config_defaults_and_clamping():
    assert tn.parse_config(None) == {'metric': False, 'obs_min': 10, 'stations': []}
    cfg = tn.parse_config({'useMetric': 'true', 'obsPollMinutes': 2,
                           'stations': [{'station': ' ' + SUB_ID + ' ', 'obsStation': 'none'}, {'station': ''}, 'Montauk, NY']})
    assert cfg['metric'] is True and cfg['obs_min'] == 6
    assert cfg['stations'] == [{'query': SUB_ID, 'obs': 'none'}, {'query': 'Montauk, NY', 'obs': ''}]
    assert tn.parse_config({'obsPollMinutes': 9999})['obs_min'] == 120
    assert tn.parse_config({'obsPollMinutes': 'abc'})['obs_min'] == 10
    assert tn.parse_config({})['stations'] == []


def test_node_driver_filtering_does_not_touch_class_defaults():
    keep = tn.table_keep(2)
    node = tn.TideTableNode(FakePoly(), 'p', 'a', 'n', keep)
    assert set(d['driver'] for d in node.drivers) == keep
    assert len(tn.TideTableNode.drivers) == 10
    full = tn.TideStationNode(FakePoly(), 'p', 'a', 'n')
    assert len(full.drivers) == 12
