import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tide_logic import tn, _events, _utc  # noqa: E402

SID = '8510560'


class HubPoly(object):
    '''Fake poly that answers addNode the way the hub does: asynchronously on ADDNODEDONE. It models what
    the real hub was observed to do: a node whose primary is the controller (or itself) is accepted, a
    node nested one level deeper (primary is another non-controller node) is refused with a 400.'''
    ADDNODEDONE = 5

    def __init__(self, fail=(), delay=0.05):
        self.fail = set(fail)
        self.delay = delay
        self.sent = []
        self.primaries = {}
        self.names = {}
        self.rename_flags = {}
        self.renamed = []
        self.db_nodes = []
        self.hub = set()
        self.deleted = []
        self.subs = []

    def subscribe(self, topic, callback, address=None):
        self.subs.append(callback)

    def db_getNodeDrivers(self, addr=None, init=False):
        return []

    def getNodesFromDb(self, addr=None):
        return list(self.db_nodes)

    def getValidAddress(self, s):
        return s.lower()[:14]

    def getValidName(self, s):
        return s

    def delNode(self, address):
        self.deleted.append(address)

    def renameNode(self, address, newname):
        self.renamed.append((address, newname))

    def addNode(self, node, conn_status=None, rename=False):
        self.sent.append(node.address)
        self.primaries[node.address] = node.primary
        self.names[node.address] = node.name
        self.rename_flags[node.address] = rename
        parent_ok = node.primary in ('ctl', node.address)
        refuse = node.address in self.fail or not parent_ok

        def reply():
            time.sleep(self.delay)
            if refuse:
                data = {'address': node.address, 'error': 'Request failed with status code 400'}
            else:
                self.hub.add(node.address)
                data = {'id': node.address, 'address': node.address, 'nodeDefId': node.id}
            for cb in list(self.subs):
                cb(data)
        threading.Thread(target=reply, daemon=True).start()
        return node


class FakeCtl(object):
    def __init__(self, tmp_path, poly):
        self.poly = poly
        self.address = 'ctl'
        self.persist_dir = str(tmp_path)


class RecNode(object):
    def __init__(self, drivers):
        self.drivers = [{'driver': d} for d in drivers]
        self.set = {}

    def setDriver(self, drv, value, report=True, force=False, uom=None, text=None):
        self.set[drv] = (value, text)


def _setup(tmp_path, fail=(), delay=0.05):
    poly = HubPoly(fail, delay)
    ctl = FakeCtl(tmp_path, poly)
    t = tn._ts(ctl)
    t['cfg'] = {'metric': False, 'obs_min': 10, 'stations': []}
    t['stations'][SID] = {'meta': {'id': SID, 'name': 'Montauk', 'state': 'NY', 'lat': 41.05, 'lng': -71.96, 'type': 'R'},
                          'events': _events(), 'fetched': 0, 'obs': {'src': None, 'kind': 0, 'dist': 0.0},
                          'nodes': {}, 'sig': {}, 'astro_key': None}
    return ctl, poly, t['stations'][SID]


def test_every_node_hangs_directly_off_the_controller(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    assert tn._ensure_nodes(ctl, SID) is True
    assert set(poly.sent) == {'t' + SID, 'b' + SID, 'i' + SID}
    # nothing was refused with a 400, and every node is recorded
    assert poly.hub == set(poly.sent)
    assert set(poly.primaries.values()) == {'ctl'}
    assert set(st['nodes']) == {'tide', 'table', 'info'}
    assert set(tn._ts(ctl)['sigs']) == poly.hub


def test_nesting_under_another_node_is_what_the_hub_refuses(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    node = tn.TideTableNode(poly, 't' + SID, 'b' + SID, 'n')
    ok, why = tn._add_confirmed(ctl, node)
    assert ok is False and '400' in why


def test_refused_child_is_not_recorded_then_retried(tmp_path):
    ctl, poly, st = _setup(tmp_path, fail={'b' + SID})
    t = tn._ts(ctl)
    tn._ensure_nodes(ctl, SID)
    assert 'tide' in st['nodes'] and 'info' in st['nodes'] and 'table' not in st['nodes']
    assert 'b' + SID not in t['sigs']
    # a normal poll right away does not hammer the hub (backoff) ...
    before = len(poly.sent)
    assert tn._ensure_nodes(ctl, SID) is False
    assert len(poly.sent) == before
    # ... but once the hub is willing, a forced pass (config save) adds it
    poly.fail.clear()
    assert tn._ensure_nodes(ctl, SID, force=True) is True
    assert 'table' in st['nodes'] and 'b' + SID in t['sigs']
    assert 'table' not in st['fails']


def test_backoff_expires_and_doubles(tmp_path):
    ctl, poly, st = _setup(tmp_path, fail={'b' + SID})
    tn._ensure_nodes(ctl, SID)
    assert st['fails']['table']['n'] == 1
    st['fails']['table']['next'] = 0          # backoff elapsed
    tn._ensure_nodes(ctl, SID)
    assert st['fails']['table']['n'] == 2
    first = tn.ADD_RETRY_FIRST_S
    assert st['fails']['table']['next'] - time.time() > first   # second delay is longer than the first


def test_a_refused_tides_node_does_not_block_the_others(tmp_path):
    ctl, poly, st = _setup(tmp_path, fail={'t' + SID})
    assert tn._ensure_nodes(ctl, SID) is True
    assert set(st['nodes']) == {'table', 'info'}
    assert 't' + SID not in tn._ts(ctl)['sigs']
    assert st['fails']['tide']['n'] == 1


def test_no_answer_from_hub_is_reported_not_assumed_ok(tmp_path, monkeypatch):
    ctl, poly, st = _setup(tmp_path, delay=1.0)
    node = tn.TideStationNode(poly, 'ctl', 't' + SID, 'n')
    ok, why = tn._add_confirmed(ctl, node, wait=0.05)
    assert ok is False and 'no answer' in why
    assert tn._ts(ctl)['adds'] == {}


def test_error_text_from_hub_is_returned(tmp_path):
    ctl, poly, st = _setup(tmp_path, fail={'t' + SID})
    node = tn.TideStationNode(poly, 'ctl', 't' + SID, 'n')
    ok, why = tn._add_confirmed(ctl, node)
    assert ok is False and '400' in why


def test_answers_for_other_addresses_are_ignored(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    tn.on_add_node_done(ctl, {'address': 'nobody', 'error': 'x'})
    tn.on_add_node_done(ctl, None)
    assert tn._ts(ctl)['adds'] == {}


def test_station_node_has_next_high_and_low_time_drivers():
    drivers = {d['driver']: d for d in tn.TideStationNode.drivers}
    assert drivers['HIGH_TIME']['uom'] == 151 and drivers['LOW_TIME']['uom'] == 151
    assert len(drivers) == 12


def test_us_date_time_label():
    assert tn.fmt_us(int(_utc(2026, 11, 4, 5, 48)), -18000) == '11/04/2026 12:48 AM'
    assert tn.fmt_us(int(_utc(2026, 11, 1, 17, 0)), -18000) == '11/01/2026 12:00 PM'
    assert tn.fmt_us(int(_utc(2026, 7, 4, 13, 5)), -14400) == '07/04/2026 9:05 AM'


def test_push_publishes_time_of_next_high_and_low(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    node = RecNode(['ST', 'TIDELVL', 'TREND', 'NEXT_TYPE', 'NEXT_HT', 'TIMEREM', 'HIGH_HT', 'HIGH_MIN',
                    'HIGH_TIME', 'LOW_HT', 'LOW_MIN', 'LOW_TIME'])
    st['nodes']['tide'] = node
    now = _utc(2026, 11, 1, 17, 0)          # 12:00 local standard time
    tn._push_station(ctl, st, now)
    hi, lo = node.set['HIGH_TIME'], node.set['LOW_TIME']
    assert hi == (int(_utc(2026, 11, 1, 21, 29)), '11/01/2026 4:29 PM')
    assert lo == (int(_utc(2026, 11, 2, 3, 54)), '11/01/2026 10:54 PM')
    # the minutes properties keep the short clock label
    assert node.set['HIGH_MIN'][1] == '4:29 PM'
    # agrees with the minutes properties (those are rounded to the minute)
    assert abs(hi[0] - (now + node.set['HIGH_MIN'][0] * 60)) <= 30
    assert abs(lo[0] - (now + node.set['LOW_MIN'][0] * 60)) <= 30


# ---------------------------------------------------------------- node names (v1.2.1 / v1.2.2)
def _plain(s):
    return s


SUFFIXES = ('Tides', 'Tide Table', 'Observed', 'Info')


def test_node_names_are_short_and_carry_no_state_id_or_source(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    tn._ensure_nodes(ctl, SID)
    assert poly.names == {'t' + SID: 'Montauk Tides', 'b' + SID: 'Montauk Tide Table', 'i' + SID: 'Montauk Info'}
    assert all(len(n) <= tn.NAME_MAX for n in poly.names.values())


def test_observed_name_does_not_include_the_source_station(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    st['meta'] = dict(st['meta'], name='Noyack Bay', state='NY')
    st['obs'] = {'src': {'id': '8510560', 'name': 'Montauk', 'sensors': ['water_level']}, 'kind': 2, 'dist': 17.0}
    tn._ts(ctl)['obs_cache']['8510560'] = {'data': {'water_level': {'epoch': 1, 'v': 1.0}}, 'ts': 0}
    tn._ensure_nodes(ctl, SID)
    assert poly.names['o' + SID] == 'Noyack Bay Observed'
    assert all(len(n) <= tn.NAME_MAX for n in poly.names.values())


def test_noaa_name_with_a_comma_uses_the_part_before_it_for_every_node(tmp_path):
    # What v1.2.1 really produced on the hub for NOAA's 'Montauk, Fort Pond Bay':
    # 'Montauk, Fort Pond Tides', 'Montauk, Fort Tide Table', 'Montauk, Fort P Observed', 'Montauk, Fort Pond Info'
    names = [tn.node_name(_plain, 'Montauk, Fort Pond Bay', s) for s in SUFFIXES]
    assert names == ['Montauk Tides', 'Montauk Tide Table', 'Montauk Observed', 'Montauk Info']
    ctl, poly, st = _setup(tmp_path)
    st['meta'] = dict(st['meta'], name='Montauk, Fort Pond Bay')
    tn._ensure_nodes(ctl, SID)
    assert poly.names['t' + SID] == 'Montauk Tides' and poly.names['b' + SID] == 'Montauk Tide Table'


def test_all_nodes_of_a_station_share_one_label_and_none_is_cut_mid_word():
    for long_name in ('Shinnecock Bay Inlet Ponquogue Bridge', 'Sandy Hook Bay Point Comfort Reach',
                      'Noyack Bay', 'Port Jefferson Harbor Entrance'):
        names = [tn.node_name(_plain, long_name, s) for s in SUFFIXES]
        labels = set(n[:-len(s) - 1] for n, s in zip(names, SUFFIXES))
        assert len(labels) == 1, names
        assert all(len(n) <= tn.NAME_MAX for n in names), names
        label = labels.pop()
        assert all(w in long_name.split() for w in label.split()), label   # whole words only


def test_long_station_name_is_shortened_but_never_the_suffix():
    name = tn.node_name(_plain, 'Shinnecock Bay Inlet Ponquogue Bridge', 'Tide Table')
    assert name == 'Shinnecock Tide Table'
    assert tn.node_name(_plain, 'A', 'Info') == 'A Info'
    assert tn.node_name(_plain, 'Noyack Bay', 'Tide Table') == 'Noyack Bay Tide Table'
    # a single very long word has to be hard-cut
    assert len(tn.node_name(_plain, 'Supercalifragilistic', 'Tide Table')) <= tn.NAME_MAX


def test_name_is_cleaned_before_it_is_measured():
    strip = lambda s: s.replace('(', '').replace(')', '').replace('@', '')   # what IoX removes
    assert tn.node_name(strip, 'Port (Aux)  Pt', 'Info') == 'Port Aux Pt Info'


def test_duplicate_station_names_get_state_or_id():
    a = {'id': '1', 'name': 'Springfield', 'state': 'MA'}
    b = {'id': '2', 'name': 'springfield', 'state': 'NY'}
    c = {'id': '3', 'name': 'Springfield', 'state': 'NY'}
    solo = {'id': '9', 'name': 'Solo', 'state': 'NY'}
    assert tn.station_label(solo, [a, b, solo]) == ('Solo', '')
    assert tn.station_label(a, [a, b]) == ('Springfield', 'MA')
    assert tn.station_label(b, [a, b, c]) == ('springfield', '2')   # same state as c: use the ID
    name = tn.node_name(_plain, 'Springfield', 'Tide Table', 'MA')
    assert name.endswith(' MA Tide Table') and len(name) <= tn.NAME_MAX


def test_stations_whose_short_names_collide_are_told_apart():
    long_one = {'id': '8510560', 'name': 'Montauk, Fort Pond Bay', 'state': 'NY'}
    plain_one = {'id': '5', 'name': 'Montauk', 'state': 'NY'}
    other = {'id': '6', 'name': 'Noyack Bay', 'state': 'NY'}
    assert tn.station_label(other, [long_one, plain_one, other]) == ('Noyack Bay', '')
    assert tn.station_label(long_one, [long_one, plain_one]) == ('Montauk, Fort Pond Bay', '8510560')
    # a 7-digit ID leaves only 5 characters for the name under the 24-character cap, so the name is cut
    name = tn.node_name(_plain, 'Montauk, Fort Pond Bay', 'Tide Table', '8510560')
    assert name == 'Monta 8510560 Tide Table' and len(name) <= tn.NAME_MAX
    assert name.endswith(' 8510560 Tide Table')


def test_nodes_are_added_with_rename_requested(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    tn._ensure_nodes(ctl, SID)
    assert poly.rename_flags and all(poly.rename_flags.values())


def test_existing_nodes_are_renamed_in_place_never_deleted(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    poly.db_nodes = [{'address': 't' + SID, 'name': 'Montauk NY Tides (8510560)'},
                     {'address': 'b' + SID, 'name': 'Montauk Tide Table'},
                     {'address': 'i' + SID, 'name': 'Montauk NY Info'}]
    tn._ensure_nodes(ctl, SID)
    assert ('t' + SID, 'Montauk Tides') in poly.renamed
    assert ('i' + SID, 'Montauk Info') in poly.renamed
    assert not any(a == 'b' + SID for a, _n in poly.renamed)      # that name was already right
    assert poly.deleted == []


def test_label_change_renames_a_live_node_without_deleting_it(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    tn._ensure_nodes(ctl, SID)
    assert poly.renamed == []
    st['meta']['name'] = 'Montauk Point'
    assert tn._ensure_nodes(ctl, SID) is False
    assert ('t' + SID, 'Montauk Point Tides') in poly.renamed
    assert ('b' + SID, 'Montauk Point Tide Table') in poly.renamed
    assert poly.deleted == []
    n = len(poly.renamed)
    tn._ensure_nodes(ctl, SID)
    assert len(poly.renamed) == n                                   # nothing more to rename
