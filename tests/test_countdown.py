import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tide_logic import tn, _events  # noqa: E402
from test_tide_nodes_add import _setup, RecNode  # noqa: E402

TIDE_DRIVERS = ['ST', 'TIDELVL', 'TREND', 'NEXT_TYPE', 'NEXT_HT', 'TIMEREM', 'HIGH_HT', 'HIGH_MIN',
                'HIGH_TIME', 'LOW_HT', 'LOW_MIN', 'LOW_TIME']


def test_minutes_round_down_so_the_last_minute_reads_zero():
    assert tn._mins(1000 + 59, 1000) == 0
    assert tn._mins(1000 + 59.9, 1000) == 0
    assert tn._mins(1000 + 60, 1000) == 1
    assert tn._mins(1000 + 119, 1000) == 1
    assert tn._mins(1000 + 120, 1000) == 2
    assert tn._mins(1000, 1000) == 0
    assert tn._mins(1000 - 5, 1000) == 0          # never negative


def _run(offset, step):
    '''Poll the countdown every step seconds, starting offset seconds into a run of real predictions.'''
    ev = _events()
    hold = {}
    first = ev[0]['epoch'] + 600 + offset
    last = ev[-1]['epoch'] - 600
    now = first
    reads = []
    while now < last:
        comp = tn.compute_tides(ev, now)
        mins, _label = tn.tide_minutes(hold, 'next', comp['next'], now, 'x')
        reads.append(mins)
        now += step
    passed = [e for e in ev if first < e['epoch'] < last]
    return reads, passed


@pytest.mark.parametrize('step', (60, 61, 75, 90))
def test_every_tide_gets_exactly_one_zero_whatever_the_poll_timing(step):
    for offset in range(0, 60, 5):
        reads, passed = _run(offset, step)
        assert len(passed) >= 7
        arrivals = sum(1 for a, b in zip(reads, reads[1:]) if a > 0 and b == 0)
        assert arrivals == len(passed), (step, offset)


def test_a_zero_is_held_with_the_passed_tides_own_label_for_one_reading_only():
    ev = _events()
    e = ev[3]
    hold = {}
    assert tn.tide_minutes(hold, 'next', e, e['epoch'] - 61, 'old') == (1, 'old')
    # the 60 second window was skipped: the tide has passed and the countdown already points at the next one
    assert tn.tide_minutes(hold, 'next', ev[4], e['epoch'] + 10, 'new') == (0, 'old')
    mins, label = tn.tide_minutes(hold, 'next', ev[4], e['epoch'] + 70, 'new')
    assert label == 'new' and mins == tn._mins(ev[4]['epoch'], e['epoch'] + 70) and mins > 0


def test_no_second_zero_when_the_window_was_already_caught():
    ev = _events()
    e = ev[3]
    hold = {}
    assert tn.tide_minutes(hold, 'next', e, e['epoch'] - 30, 'old')[0] == 0        # natural zero
    mins, label = tn.tide_minutes(hold, 'next', ev[4], e['epoch'] + 30, 'new')
    assert mins > 0 and label == 'new'


def test_no_zero_is_invented_after_a_long_gap():
    ev = _events()
    e = ev[3]
    hold = {}
    tn.tide_minutes(hold, 'next', e, e['epoch'] - 300, 'old')
    later = e['epoch'] + tn.ZERO_HOLD_MAX_S + 60
    mins, label = tn.tide_minutes(hold, 'next', ev[4], later, 'new')
    assert mins == tn._mins(ev[4]['epoch'], later) and mins > 0 and label == 'new'


def test_no_zero_when_the_predictions_shift_before_the_tide():
    ev = _events()
    e = ev[3]
    hold = {}
    tn.tide_minutes(hold, 'next', e, e['epoch'] - 3600, 'a')
    moved = dict(e, epoch=e['epoch'] + 120)           # a refresh moved the tide a little; it is still ahead
    mins, label = tn.tide_minutes(hold, 'next', moved, e['epoch'] - 3000, 'b')
    assert mins > 0 and label == 'b'


def test_countdowns_for_next_high_and_next_low_are_independent():
    ev = _events()
    hold = {}
    high, low = ev[3], ev[4]
    tn.tide_minutes(hold, 'high', high, high['epoch'] - 61, 'h')
    tn.tide_minutes(hold, 'low', low, high['epoch'] - 61, 'l')
    assert tn.tide_minutes(hold, 'high', ev[5], high['epoch'] + 10, 'h2') == (0, 'h')
    mins, label = tn.tide_minutes(hold, 'low', low, high['epoch'] + 10, 'l')
    assert mins > 0 and label == 'l'


def test_push_reports_zero_for_a_passed_high_tide_even_when_the_poll_missed_the_window(tmp_path):
    ctl, poly, st = _setup(tmp_path)
    node = RecNode(TIDE_DRIVERS)
    st['nodes']['tide'] = node
    ev = st['events']
    high = ev[3]
    assert high['type'] == 'H' and ev[4]['type'] == 'L' and ev[5]['type'] == 'H'

    tn._push_station(ctl, st, high['epoch'] - 61)
    assert node.set['TIMEREM'][0] == 1 and node.set['HIGH_MIN'][0] == 1
    label_before = node.set['TIMEREM'][1]
    low_mins = node.set['LOW_MIN'][0]
    assert low_mins > 1

    tn._push_station(ctl, st, high['epoch'] + 10)            # next poll is already past the tide
    assert node.set['TIMEREM'] == (0, label_before)
    assert node.set['HIGH_MIN'] == (0, label_before)
    assert node.set['LOW_MIN'][0] > 0                         # the low tide is not affected
    # only the minutes are held; the other properties already describe the following tide
    assert node.set['NEXT_TYPE'][0] == 2
    assert node.set['HIGH_TIME'][0] == ev[5]['epoch']

    now = high['epoch'] + 70
    tn._push_station(ctl, st, now)
    assert node.set['TIMEREM'][0] == tn._mins(ev[4]['epoch'], now) > 0
    assert node.set['HIGH_MIN'][0] == tn._mins(ev[5]['epoch'], now) > 0
