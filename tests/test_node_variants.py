import json
import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tide_logic import tn, _events, _utc  # noqa: E402
from test_tide_nodes_add import HubPoly, FakeCtl, RecNode, SID  # noqa: E402

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = json.load(open(os.path.join(PLUGIN_DIR, 'profile.json')))
EDITORS = set(e['id'] for e in PROFILE['editors'])
NODEDEFS = dict((nd['id'], nd) for nd in PROFILE['nodedefs'])
CLASSES = (tn.NoaaStatusNode, tn.TideStationNode, tn.TideTableNode, tn.ObservedNode, tn.TideInfoNode)
INFO_NO_DIST = {'ST', 'LATITUDE', 'LONGITUDE', 'OBS_SRC', 'SUNRISE', 'SUNSET', 'MOON', 'MOON_ILLUM'}


class VariantPoly(HubPoly):
    '''HubPoly that also records updateJsonProfile calls and which nodedef id / drivers each node was added with.'''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile_docs = []
        self.profile_calls = 0
        self.profile_error = None
        self.ids = {}
        self.node_drivers = {}
        self.log = []

    def updateJsonProfile(self, doc, opts=None):
        self.profile_calls += 1
        if self.profile_error:
            raise RuntimeError(self.profile_error)
        self.profile_docs.append(doc)
        for nd in doc.get('nodedefs', []):
            self.log.append(('profile', nd['id']))
        return {'ok': True}

    def addNode(self, node, conn_status=None, rename=False):
        self.ids[node.address] = node.id
        self.node_drivers[node.address] = [d['driver'] for d in node.drivers]
        self.log.append(('add', node.address))
        return super().addNode(node, conn_status, rename)


def _setup_v(tmp_path, events=None, **kwargs):
    poly = VariantPoly(**kwargs)
    ctl = FakeCtl(tmp_path, poly)
    t = tn._ts(ctl)
    t['cfg'] = {'metric': False, 'obs_min': 10, 'stations': []}
    t['stations'][SID] = {'meta': {'id': SID, 'name': 'Montauk', 'state': 'NY', 'lat': 41.05, 'lng': -71.96, 'type': 'R'},
                          'events': events or _events(), 'fetched': 0, 'obs': {'src': None, 'kind': 0, 'dist': 0.0},
                          'nodes': {}, 'sig': {}, 'astro_key': None}
    return ctl, poly, t['stations'][SID]


def _observed(tmp_path, data, kind=2, **kwargs):
    ctl, poly, st = _setup_v(tmp_path, **kwargs)
    st['obs'] = {'src': {'id': '8510560', 'name': 'Montauk', 'sensors': ['water_level']}, 'kind': kind,
                 'dist': 17.0 if kind == 2 else 0.0}
    tn._ts(ctl)['obs_cache']['8510560'] = {'data': data, 'ts': 0}
    return ctl, poly, st


def _defs(poly):
    defs = dict(NODEDEFS)
    for doc in poly.profile_docs:
        for nd in doc.get('nodedefs', []):
            defs[nd['id']] = nd
    return defs


def _variant_props(poly, vid):
    for doc in poly.profile_docs:
        for nd in doc.get('nodedefs', []):
            if nd['id'] == vid:
                return [p['id'] for p in nd['properties']]
    return None


def _assert_nodes_match_definitions(poly):
    '''The invariant behind the blank-property fix: what a node reports == what its nodedef lists.'''
    defs = _defs(poly)
    assert poly.ids
    for address, nid in poly.ids.items():
        assert nid in defs, '%s uses a nodedef the hub was never given: %s' % (address, nid)
        assert set(poly.node_drivers[address]) == set(p['id'] for p in defs[nid]['properties']), address


BASIC_OBS = {'water_level': {'epoch': 1, 'v': 2.0}, 'water_temperature': {'epoch': 1, 'v': 59.0},
             'wind': {'epoch': 1, 'spd': 5.0, 'dir': 200.0, 'gust': 7.0}}
FULL_OBS = dict(BASIC_OBS, air_temperature={'epoch': 1, 'v': 61.0}, air_pressure={'epoch': 1, 'v': 1015.0},
                humidity={'epoch': 1, 'v': 70.0})


# ---------------------------------------------------------------- definitions
def test_every_node_class_lists_exactly_the_properties_of_its_nodedef():
    for cls in CLASSES:
        assert set(d['driver'] for d in cls.drivers) == set(p['id'] for p in NODEDEFS[cls.id]['properties']), cls.id


def test_full_property_set_keeps_the_base_id_and_a_subset_gets_a_stable_short_id():
    props = [p['id'] for p in NODEDEFS['tide_observed']['properties']]
    assert tn.variant_id('tide_observed', props, props) == 'tide_observed'
    sub = [p for p in props if p != 'CLIHUM']
    vid = tn.variant_id('tide_observed', props, sub)
    assert vid.startswith('tide_observed_') and vid != 'tide_observed'
    assert len(vid) <= 31 and re.fullmatch(r'[A-Za-z0-9_]+', vid)
    assert vid == tn.variant_id('tide_observed', props, list(reversed(sub)))       # order does not matter
    assert vid != tn.variant_id('tide_observed', props, [p for p in props if p != 'SURGE'])


def test_variant_nodedef_lists_only_kept_properties_in_profile_order_and_leaves_the_base_alone():
    base = NODEDEFS['tide_info']
    kept = ['ST', 'LATITUDE', 'LONGITUDE', 'OBS_SRC', 'SUNRISE', 'SUNSET', 'MOON', 'MOON_ILLUM']
    nd = tn.variant_nodedef(base, 'tide_info_abc123', set(kept))
    assert nd['id'] == 'tide_info_abc123' and nd['name'] == base['name']
    assert [p['id'] for p in nd['properties']] == kept
    assert all(p['editor'] in EDITORS for p in nd['properties'])
    assert 'links' in nd and 'cmds' in nd
    assert len(base['properties']) == 9 and base['id'] == 'tide_info'


# ---------------------------------------------------------------- nodes use them
def test_info_node_without_a_nearby_source_gets_a_definition_without_distance(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    tn._ensure_nodes(ctl, SID)
    vid = poly.ids['i' + SID]
    assert vid.startswith('tide_info_')
    assert set(_variant_props(poly, vid)) == INFO_NO_DIST
    assert set(poly.node_drivers['i' + SID]) == INFO_NO_DIST
    # the hub is told about the definition before the node that uses it
    assert poly.log.index(('profile', vid)) < poly.log.index(('add', 'i' + SID))
    # nodes that report every property keep the standard definitions
    assert poly.ids['t' + SID] == 'tide_station' and poly.ids['b' + SID] == 'tide_table'
    _assert_nodes_match_definitions(poly)


def test_info_node_with_a_nearby_source_keeps_the_standard_definition(tmp_path):
    ctl, poly, st = _observed(tmp_path, BASIC_OBS, kind=2)
    tn._ensure_nodes(ctl, SID)
    assert poly.ids['i' + SID] == 'tide_info'
    assert not [d for d in poly.profile_docs if d['nodedefs'][0]['id'] == 'tide_info']
    _assert_nodes_match_definitions(poly)


def test_observed_node_without_humidity_pressure_air_temp_or_surge(tmp_path):
    ctl, poly, st = _observed(tmp_path, BASIC_OBS, kind=2)
    tn._ensure_nodes(ctl, SID)
    vid = poly.ids['o' + SID]
    assert vid.startswith('tide_observed_')
    listed = set(_variant_props(poly, vid))
    assert listed == {'ST', 'TIDELVL', 'OBS_AGE', 'WATERT', 'SPEED', 'GUST', 'WINDDIR'}
    assert not listed & {'CLIHUM', 'SURGE', 'BARPRES', 'TEMPOUT'}
    _assert_nodes_match_definitions(poly)


def test_observed_node_with_every_sensor_keeps_the_standard_definition(tmp_path):
    ctl, poly, st = _observed(tmp_path, FULL_OBS, kind=1)
    tn._ensure_nodes(ctl, SID)
    assert poly.ids['o' + SID] == 'tide_observed'
    assert not [d for d in poly.profile_docs if d['nodedefs'][0]['id'].startswith('tide_observed')]
    _assert_nodes_match_definitions(poly)


def test_station_with_two_tides_a_day_gets_a_two_slot_table_definition(tmp_path):
    diurnal = [{'epoch': 1000000 + i * 44640, 'v': 1.0, 'type': 'H' if i % 2 == 0 else 'L', 'off': 0}
               for i in range(10)]
    ctl, poly, st = _setup_v(tmp_path, events=diurnal)
    tn._ensure_nodes(ctl, SID)
    vid = poly.ids['b' + SID]
    assert vid.startswith('tide_table_')
    assert set(_variant_props(poly, vid)) == tn.table_keep(2)
    _assert_nodes_match_definitions(poly)


def test_each_definition_is_sent_once_per_process(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    tn._ensure_nodes(ctl, SID)
    calls = poly.profile_calls
    assert calls >= 1
    tn._ensure_nodes(ctl, SID)
    tn._ensure_nodes(ctl, SID, force=True)
    assert poly.profile_calls == calls


def test_a_changed_property_set_rebuilds_the_node_under_a_new_definition(tmp_path):
    ctl, poly, st = _observed(tmp_path, BASIC_OBS, kind=2)
    tn._ensure_nodes(ctl, SID)
    first = poly.ids['o' + SID]
    tn._ts(ctl)['obs_cache']['8510560']['data'].update(FULL_OBS)       # the source gains sensors
    assert tn._ensure_nodes(ctl, SID) is True
    assert poly.ids['o' + SID] != first
    assert 'o' + SID in poly.deleted
    _assert_nodes_match_definitions(poly)


# ---------------------------------------------------------------- when the hub refuses a definition
def test_refused_definition_falls_back_to_the_standard_one_and_is_not_hammered(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    poly.profile_error = 'Update profile request failed'
    assert tn._ensure_nodes(ctl, SID) is True
    assert poly.ids['i' + SID] == 'tide_info'                 # fallback: the node is still created
    assert 'info' in st['nodes']
    attempts = poly.profile_calls
    assert attempts == 1
    tn._ensure_nodes(ctl, SID)
    assert poly.profile_calls == attempts                     # throttled


def test_node_is_upgraded_once_the_hub_accepts_the_definition(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    poly.profile_error = 'boom'
    tn._ensure_nodes(ctl, SID)
    assert poly.ids['i' + SID] == 'tide_info'
    poly.profile_error = None
    tn._ts(ctl)['profile_retry'] = 0
    assert tn._ensure_nodes(ctl, SID) is True
    assert poly.ids['i' + SID].startswith('tide_info_')
    assert 'i' + SID in poly.deleted
    _assert_nodes_match_definitions(poly)


def test_plugin_still_works_when_the_interface_has_no_updatejsonprofile(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    poly.updateJsonProfile = None
    assert tn._ensure_nodes(ctl, SID) is True
    assert poly.ids['i' + SID] == 'tide_info'


# ---------------------------------------------------------------- sun and moon (no third-party library)
def _local_minutes(epoch, off):
    d = tn.local_dt(epoch, off)
    return d.hour * 60 + d.minute


def test_sun_times_match_the_published_worked_example():
    rise, sset = tn.sun_events(40.9, -74.3, date(1990, 6, 25), -14400)      # Wayne NJ, EDT
    assert abs(_local_minutes(rise, -14400) - (5 * 60 + 26)) <= 2
    assert abs(_local_minutes(sset, -14400) - (20 * 60 + 33)) <= 2


@pytest.mark.parametrize('lat,lon,off,day,rise,sset', [
    (21.3, -157.87, -36000, date(2026, 6, 21), 5 * 60 + 50, 19 * 60 + 16),     # Honolulu, west of the date line
    (13.44, 144.65, 36000, date(2026, 12, 21), 6 * 60 + 39, 17 * 60 + 59),     # Guam, east of it
    (40.995, -72.33, -18000, date(2026, 11, 1), 6 * 60 + 20, 16 * 60 + 44),    # Noyack Bay, after the DST change
])
def test_sun_times_across_longitudes_agree_with_an_independent_calculation(lat, lon, off, day, rise, sset):
    r, s = tn.sun_events(lat, lon, day, off)
    assert abs(_local_minutes(r, off) - rise) <= 3
    assert abs(_local_minutes(s, off) - sset) <= 3
    assert r < s


def test_no_sunrise_or_sunset_in_polar_day_and_night_but_the_moon_still_works():
    assert tn.sun_events(71.3, -156.8, date(2026, 6, 21), -28800) == (None, None)
    assert tn.sun_events(71.3, -156.8, date(2026, 12, 21), -28800) == (None, None)
    a = tn.astro_for(71.3, -156.8, date(2026, 6, 21), -28800)
    assert 'sunrise' not in a and 'sunset' not in a and 'moon_idx' in a


def test_moon_phase_at_known_new_and_full_moons():
    assert tn.moon_for_epoch(tn.NEW_MOON_REF) == (0, 0)
    assert tn.moon_for_epoch(int(_utc(2026, 11, 9, 8, 2))) == (0, 0)            # new moon 9 Nov 2026
    assert tn.moon_for_epoch(int(_utc(2026, 11, 24, 14, 53))) == (4, 100)       # full moon 24 Nov 2026
    first_quarter = tn.moon_for_epoch(int(_utc(2026, 11, 9, 8, 2)) + int(tn.SYNODIC_DAYS / 4 * 86400))
    # the mean lunar month can be about a day off the real phase, so illumination is only roughly 50%
    assert first_quarter[0] == 2 and 35 <= first_quarter[1] <= 65


def test_the_info_node_receives_sun_and_moon_values(tmp_path):
    ctl, poly, st = _setup_v(tmp_path)
    node = RecNode(sorted(INFO_NO_DIST))
    st['nodes']['info'] = node
    tn._push_station(ctl, st, _utc(2026, 11, 1, 17, 0))
    for drv in ('SUNRISE', 'SUNSET', 'MOON', 'MOON_ILLUM'):
        assert drv in node.set, drv
    assert node.set['SUNRISE'][0] < node.set['SUNSET'][0]
    assert re.fullmatch(r'\d{1,2}:\d{2} [AP]M', node.set['SUNRISE'][1])


def test_astral_is_no_longer_required():
    assert not hasattr(tn, 'HAS_ASTRAL')
    assert 'astral' not in open(os.path.join(PLUGIN_DIR, 'requirements.txt')).read().lower()
