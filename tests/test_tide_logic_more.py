import os
import sys
import time
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tide_logic import tn, _events, _utc  # noqa: E402

NOW = 1000000.0


def _cache(ahead_s, age_s):
    return {'events': [{'epoch': NOW + ahead_s}], 'fetched': NOW - age_s}


def test_build_events_pairs_local_offsets_across_dst_change():
    ev = _events()
    assert len(ev) == 10
    assert [e['off'] for e in ev] == [-14400] * 5 + [-18000] * 5
    assert ev[0]['epoch'] < ev[-1]['epoch']
    e = [x for x in ev if x['epoch'] == int(_utc(2026, 11, 1, 15, 2))][0]
    assert tn.fmt_clock(e['epoch'], e['off']) == '10:02 AM'
    e = [x for x in ev if x['epoch'] == int(_utc(2026, 11, 1, 21, 29))][0]
    assert tn.fmt_clock(e['epoch'], e['off']) == '4:29 PM'


def test_compute_tides_midday():
    ev = _events()
    now = _utc(2026, 11, 1, 17, 0)   # 12:00 local standard time
    comp = tn.compute_tides(ev, now)
    assert comp['trend'] == 1 and comp['next']['type'] == 'H'
    assert comp['today_date'] == date(2026, 11, 1)
    assert 0.8 < comp['est'] < 1.1
    assert comp['high']['epoch'] == int(_utc(2026, 11, 1, 21, 29))
    assert comp['low']['v'] == 0.197
    assert len(comp['today']) == 4 and len(comp['tomorrow']) == 1
    assert tn.fmt_when(comp['next']['epoch'], comp['next']['off'], comp['today_date']) == '4:29 PM'


def test_interp_exact_at_event_and_none_outside():
    ev = _events()
    e = ev[3]
    assert tn.interp_height(ev, e['epoch']) == pytest.approx(e['v'])
    assert tn.interp_height(ev, ev[0]['epoch'] - 10) is None
    assert tn.interp_height(ev, ev[-1]['epoch'] + 10) is None


def test_slots_and_range():
    ev = _events()
    comp = tn.compute_tides(ev, _utc(2026, 11, 1, 17, 0))
    today = tn.slot_values(comp['today'], 4, False)
    assert today[0] == (1, '3:56 AM, 2.2 ft')
    assert today[3] == (2, '10:54 PM, 0.2 ft')
    tomorrow = tn.slot_values(comp['tomorrow'], 4, False)
    assert tomorrow[1] == (0, '--')
    assert tn.tide_range(comp['today']) == pytest.approx(2.653 - 0.197)
    assert tn.tide_range(comp['tomorrow']) is None
    assert tn.slot_values(comp['today'], 1, True)[0][1] == '3:56 AM, 0.67 m'


def test_table_slots_semidiurnal_and_diurnal():
    assert tn.table_slots(_events()) == 4
    diurnal = [{'epoch': 1000000 + i * 44640, 'v': 1.0, 'type': 'H' if i % 2 == 0 else 'L', 'off': 0} for i in range(10)]
    assert tn.table_slots(diurnal) == 2
    assert tn.table_keep(2) == {'ST', 'TMRW_RANGE', 'TODAY_1', 'TODAY_2', 'TMRW_1', 'TMRW_2'}


def test_refresh_when_there_are_no_events():
    assert tn.preds_need_refresh({'events': [], 'fetched': 0}, NOW)


def test_no_refresh_when_cache_is_recent_and_long():
    assert not tn.preds_need_refresh(_cache(3 * 86400, 3600), NOW)


def test_refresh_when_cache_is_a_day_old():
    assert tn.preds_need_refresh(_cache(3 * 86400, 24 * 3600), NOW)


def test_refresh_when_cache_runs_out_soon():
    assert tn.preds_need_refresh(_cache(20 * 3600, 3600), NOW)


def test_parse_obs_skips_blank_latest_row():
    rows = [{'t': '2026-11-01 15:30', 'v': '59.2', 'f': '0,0,0'}, {'t': '2026-11-01 15:36', 'v': '', 'f': '1,0,0'}]
    rec = tn.parse_obs('water_temperature', rows)
    assert rec['v'] == 59.2 and rec['epoch'] == int(_utc(2026, 11, 1, 15, 30))
    assert tn.parse_obs('water_level', []) is None
    assert tn.parse_obs('water_level', [{'t': '2026-11-01 15:36', 'v': ''}]) is None


def test_obs_keys_none_cases():
    t = {'obs_cache': {}}
    st = {'obs': {'src': {'id': '8510560'}, 'kind': 2}, 'events': [1]}
    assert tn.obs_keys(t, st) is None
    assert tn.obs_keys(t, {'obs': {'src': None}}) is None


def test_obs_status_current_and_stale():
    now = 1000000.0
    t = {'cfg': {'obs_min': 10}, 'obs_cache': {'a': {'data': {'water_level': {'epoch': now - 600, 'v': 1.0}}}}}
    st = {'obs': {'src': {'id': 'a'}}}
    assert tn.obs_status(t, st, now)[0] == 1
    t['obs_cache']['a']['data']['water_level']['epoch'] = now - 7200
    assert tn.obs_status(t, st, now)[0] == 2
    assert tn.obs_status(t, {'obs': {'src': None}}, now)[0] == 0


def test_noaa_status_transitions():
    s = tn._http_state
    saved = dict(s)
    try:
        s.update({'fails': 0, 'last_ok': 0.0, 'backoff_until': 0.0})
        assert tn.noaa_status() == 0
        s['last_ok'] = time.time()
        assert tn.noaa_status() == 1
        s['fails'] = 1
        assert tn.noaa_status() == 2
        s['fails'] = 3
        assert tn.noaa_status() == 4
        s['backoff_until'] = time.time() + 100
        assert tn.noaa_status() == 3
    finally:
        s.update(saved)


def test_node_ids_match_profile():
    assert tn.NoaaStatusNode.id == 'noaa_status'
    assert tn.TideStationNode.id == 'tide_station'
    assert tn.TideTableNode.id == 'tide_table'
    assert tn.ObservedNode.id == 'tide_observed'
    assert tn.TideInfoNode.id == 'tide_info'


def test_astro_sun_and_moon():
    a = tn.astro_for(40.995, -72.33, date(2026, 11, 1), -18000)
    assert a['sunrise'] < a['sunset']
    sunrise_hr = ((a['sunrise'] - 18000) % 86400) / 3600.0
    sunset_hr = ((a['sunset'] - 18000) % 86400) / 3600.0
    assert 5.5 < sunrise_hr < 7.5 and 15.5 < sunset_hr < 17.5
    assert 0 <= a['moon_idx'] <= 7 and 0 <= a['moon_pct'] <= 100
