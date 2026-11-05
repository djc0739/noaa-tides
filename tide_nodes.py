'''NOAA Tides & Currents helpers and node classes for the eisy/IoX NOAA Tides plugin.

All logic lives in module-level functions so it can be unit tested. The plugin.py
override bodies are thin wrappers around the functions in this module.
Data comes from the NOAA CO-OPS Data API and Metadata API (free, no key required).
Sunrise/sunset and moon phase are calculated here in pure Python (no third-party library).
Predictions are not for navigation.
'''
import copy
import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import udi_interface

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

Node = udi_interface.Node
LOGGER = udi_interface.LOGGER

DATA_URL = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
MD_URL = 'https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi'
NOAA_MAP_URL = 'https://tidesandcurrents.noaa.gov/map/'
APP_NAME = 'eisy_noaa_tides'
STATUS_ADDRESS = 'noaa_status'
_HEADERS = {'User-Agent': 'eisy-noaa-tides/1.0 (UDI IoX plugin)'}
PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profile.json')

MIN_GAP_S = 1.2            # minimum spacing between any two NOAA requests
CATALOG_MAX_AGE_S = 30 * 86400
OBS_MAX_DIST_NM = 50.0
MIN_OBS_MIN = 6            # NOAA publishes new observations every 6 minutes
STALE_PRED_S = 48 * 3600
PRED_RETRY_S = 1800
ADD_WAIT_S = 20.0          # how long to wait for the hub to answer one addNode
ADD_RETRY_FIRST_S = 30     # first retry delay after the hub refuses a node
ADD_RETRY_MAX_S = 600      # retry delay cap
PROFILE_RETRY_S = 300      # wait before asking the hub for a node definition again after a refusal
NAME_MAX = 24              # the eisy console cuts off longer node names (observed at about 26)
LONGEST_SUFFIX = 'Tide Table'   # longest node-name suffix; the shared station label is sized for it

FT_TO_M = 0.3048
KN_TO_MPH = 1.150779
KN_TO_MS = 0.514444
MB_TO_INHG = 0.02953
NM_TO_MI = 1.150779
NM_TO_KM = 1.852

SYNODIC_DAYS = 29.530588853      # mean length of the lunar month
NEW_MOON_REF = 947182440         # 2000-01-06 18:14 UTC, a known new moon (Unix seconds)
SUN_ZENITH = 90.833              # 'official' sunrise/sunset: 90 degrees 50 minutes

OBS_PRODUCTS = ('water_level', 'water_temperature', 'air_temperature', 'wind', 'air_pressure', 'humidity')
SENSOR_MAP = {
    'Water Temperature': 'water_temperature',
    'Air Temperature': 'air_temperature',
    'Wind': 'wind',
    'Barometric Pressure': 'air_pressure',
    'Relative Humidity': 'humidity',
}
NO_OBS_WORDS = ('none', 'off', 'no')


# ---------------------------------------------------------------- units
def uoms(metric):
    return {'ht': 38 if metric else 18, 'temp': 4 if metric else 17, 'wind': 40 if metric else 48,
            'pres': 117 if metric else 23, 'dist': 83 if metric else 116}


def conv_ht(ft, metric):
    return round(ft * FT_TO_M if metric else ft, 2)


def conv_temp(deg_f, metric):
    return round((deg_f - 32.0) * 5.0 / 9.0 if metric else deg_f, 1)


def conv_wind(knots, metric):
    return round(knots * KN_TO_MS if metric else knots * KN_TO_MPH, 1)


def conv_pres(mb, metric):
    return round(mb, 1) if metric else round(mb * MB_TO_INHG, 2)


def conv_dist(nm, metric):
    return round(nm * NM_TO_KM if metric else nm * NM_TO_MI, 1)


def fmt_ht(ft, metric):
    return ('%.2f m' % (ft * FT_TO_M)) if metric else ('%.1f ft' % ft)


# ---------------------------------------------------------------- http
_http_lock = threading.Lock()
_http_state = {'last_call': 0.0, 'backoff_until': 0.0, 'fails': 0, 'rate_hits': 0, 'last_ok': 0.0}


def http_get(url, params=None, timeout=25):
    '''GET a NOAA JSON document, spaced and backed off to respect NOAA throttling.

    Returns (data, status, message). status is one of:
    ok | nodata (NOAA answered with an error message, e.g. product not offered) |
    rate (throttled or backing off) | net | http.
    '''
    if requests is None:
        return None, 'net', 'requests module is missing'
    with _http_lock:
        now = time.time()
        if now < _http_state['backoff_until']:
            return None, 'rate', 'backing off after NOAA throttling'
        gap = MIN_GAP_S - (now - _http_state['last_call'])
        if gap > 0:
            time.sleep(gap)
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=_HEADERS)
        except Exception as ex:  # noqa
            _http_state['last_call'] = time.time()
            _http_state['fails'] += 1
            return None, 'net', str(ex)
        _http_state['last_call'] = time.time()
        code = resp.status_code
        if code in (429, 503, 509):
            _http_state['fails'] += 1
            _http_state['rate_hits'] += 1
            _http_state['backoff_until'] = time.time() + min(900, 60 * (2 ** (_http_state['rate_hits'] - 1)))
            return None, 'rate', 'HTTP %s' % code
        if code != 200:
            _http_state['fails'] += 1
            return None, 'http', 'HTTP %s' % code
        try:
            data = resp.json()
        except ValueError:
            _http_state['fails'] += 1
            return None, 'http', 'invalid JSON'
        _http_state['fails'] = 0
        _http_state['rate_hits'] = 0
        _http_state['last_ok'] = time.time()
    if isinstance(data, dict) and 'error' in data:
        err = data['error']
        msg = err.get('message') if isinstance(err, dict) else str(err)
        return None, 'nodata', msg or 'error'
    return data, 'ok', ''


def noaa_status():
    '''0 Unknown, 1 Online, 2 Degraded, 3 Rate Limited, 4 Offline.'''
    s = _http_state
    if time.time() < s['backoff_until']:
        return 3
    if s['fails'] >= 3:
        return 4
    if s['fails'] >= 1:
        return 2
    return 1 if s['last_ok'] else 0


# ---------------------------------------------------------------- persistence
def _read_json(path):
    try:
        with open(path, 'r') as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json(path, obj):
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except Exception as ex:  # noqa
        LOGGER.warning('Could not write %s: %s', path, ex)


def _safe_name(text):
    return ''.join(c if c.isalnum() else '_' for c in str(text))


# ---------------------------------------------------------------- catalogs
def _slim_tide_station(s):
    try:
        return {'id': str(s['id']), 'name': str(s.get('name') or ''), 'lat': float(s['lat']),
                'lng': float(s['lng']), 'state': str(s.get('state') or ''),
                'type': str(s.get('type') or ''), 'ref': str(s.get('reference_id') or '')}
    except (KeyError, TypeError, ValueError):
        return None


def _slim_obs_station(s):
    try:
        rec = {'id': str(s['id']), 'name': str(s.get('name') or ''), 'lat': float(s['lat']),
               'lng': float(s['lng']), 'state': str(s.get('state') or '')}
    except (KeyError, TypeError, ValueError):
        return None
    sensors = ['water_level']
    block = s.get('sensors') or {}
    rows = block.get('sensors') or block.get('sensorList') or []
    for x in rows:
        if x.get('status') == 1:
            key = SENSOR_MAP.get(x.get('name'))
            if key and key not in sensors:
                sensors.append(key)
    rec['sensors'] = sensors
    return rec


def _load_catalog(path, params, slim, label, force=False):
    cached = _read_json(path)
    now = time.time()
    if cached and not force and cached.get('stations') and now - cached.get('fetched', 0) < CATALOG_MAX_AGE_S:
        return cached['stations']
    data, status, msg = http_get(MD_URL + '/stations.json', params, timeout=90)
    if isinstance(data, dict) and data.get('stations'):
        rows = [r for r in (slim(s) for s in data['stations']) if r]
        if rows:
            _write_json(path, {'fetched': now, 'stations': rows})
            return rows
    LOGGER.warning('NOAA %s station list download failed (%s: %s)%s', label, status, msg,
                   '; using cached copy' if cached else '')
    return cached.get('stations') if cached else None


def load_tide_catalog(persist_dir, force=False):
    return _load_catalog(os.path.join(persist_dir, 'catalog_tidepred.json'),
                         {'type': 'tidepredictions'}, _slim_tide_station, 'tide prediction', force)


def load_obs_catalog(persist_dir, force=False):
    return _load_catalog(os.path.join(persist_dir, 'catalog_obs.json'),
                         {'type': 'waterlevels', 'expand': 'sensors'}, _slim_obs_station, 'observation', force)


# ---------------------------------------------------------------- geo / station lookup
def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))


def _parse_latlon(q):
    parts = q.replace(',', ' ').replace(';', ' ').split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def resolve_station(query, catalog, n_near=5):
    '''Resolve a user entry to station(s).

    Returns (kind, stations, extra); kind is one of id | name | nearest | ambiguous |
    notfound | invalid. For nearest, each station carries dist_nm.
    '''
    q = (query or '').strip()
    if not q:
        return 'invalid', [], ''
    by_id = {s['id'].lower(): s for s in catalog}
    if q.lower() in by_id:
        return 'id', [by_id[q.lower()]], ''
    ll = _parse_latlon(q)
    if ll:
        lat, lon = ll
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return 'invalid', [], ''
        ranked = sorted(((haversine_nm(lat, lon, s['lat'], s['lng']), s) for s in catalog), key=lambda x: x[0])
        return 'nearest', [dict(s, dist_nm=d) for d, s in ranked[:n_near]], ''
    state = None
    name_q = q
    if ',' in q:
        left, _sep, right = q.rpartition(',')
        right = right.strip()
        if len(right) == 2 and right.isalpha():
            name_q, state = left.strip(), right.upper()
    ql = name_q.lower()
    pool = [s for s in catalog if not state or s['state'].upper() == state]
    exact = [s for s in pool if s['name'].lower() == ql]
    if len(exact) == 1:
        return 'name', exact, ''
    if len(exact) > 1:
        return 'ambiguous', sorted(exact, key=lambda s: (s['name'], s['state']))[:8], str(len(exact))
    words = ql.split()
    subs = [s for s in pool if words and all(w in s['name'].lower() for w in words)]
    if len(subs) == 1:
        return 'name', subs, ''
    if len(subs) > 1:
        return 'ambiguous', sorted(subs, key=lambda s: (s['name'], s['state']))[:8], str(len(subs))
    return 'notfound', [], ''


def choose_obs_station(meta, obs_catalog, override=''):
    '''Pick the observation source for a tide station.

    Returns (src_record_or_None, kind, dist_nm, note). kind: 0 none, 1 same station, 2 nearby.
    '''
    ov = (override or '').strip()
    if ov.lower() in NO_OBS_WORDS:
        return None, 0, 0.0, ''
    by_id = {s['id'].lower(): s for s in obs_catalog}
    note = ''
    if ov:
        s = by_id.get(ov.lower())
        if s:
            same = s['id'].lower() == meta['id'].lower()
            return s, (1 if same else 2), haversine_nm(meta['lat'], meta['lng'], s['lat'], s['lng']), ''
        note = 'Observation station [%s] is not an active NOAA water level station; using automatic selection instead.' % ov
    own = by_id.get(meta['id'].lower())
    if own:
        return own, 1, 0.0, note
    best, best_d = None, None
    for s in obs_catalog:
        d = haversine_nm(meta['lat'], meta['lng'], s['lat'], s['lng'])
        if best_d is None or d < best_d:
            best, best_d = s, d
    if best is not None and best_d <= OBS_MAX_DIST_NM:
        return best, 2, best_d, note
    return None, 0, 0.0, note


# ---------------------------------------------------------------- predictions
def _parse_ts(text):
    return datetime.strptime(text, '%Y-%m-%d %H:%M')


def build_events(gmt_preds, local_preds, fallback_off=0):
    '''Combine a GMT and a local-time (lst_ldt) high/low list into events.

    Each event: epoch (UTC seconds), v (feet), type (H/L), off (station local offset, seconds).
    The offset is found by pairing identical type/height entries, so daylight saving is exact.
    '''
    local = []
    for p in local_preds or []:
        try:
            local.append((_parse_ts(p['t']), p['type'], p['v']))
        except (KeyError, ValueError, TypeError):
            continue
    events = []
    last_off = fallback_off
    for g in gmt_preds or []:
        try:
            gdt = _parse_ts(g['t'])
            val = float(g['v'])
            typ = g['type']
        except (KeyError, ValueError, TypeError):
            continue
        best = None
        for ldt, ltype, lv in local:
            if ltype == typ and lv == g['v']:
                diff = (ldt - gdt).total_seconds()
                if abs(diff) <= 15 * 3600 and (best is None or abs(diff) < abs(best)):
                    best = diff
        if best is not None:
            last_off = int(best)
        events.append({'epoch': int(gdt.replace(tzinfo=timezone.utc).timestamp()), 'v': val,
                       'type': typ, 'off': last_off})
    events.sort(key=lambda e: e['epoch'])
    return events


def fetch_predictions(station_id, now=None, fallback_off=0):
    now = now or time.time()
    begin = datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime('%Y%m%d')
    base = {'station': station_id, 'product': 'predictions', 'datum': 'MLLW', 'units': 'english',
            'interval': 'hilo', 'begin_date': begin, 'range': 120, 'format': 'json', 'application': APP_NAME}
    gmt, status, msg = http_get(DATA_URL, dict(base, time_zone='gmt'))
    if gmt is None:
        return None, status, msg
    loc, _s2, _m2 = http_get(DATA_URL, dict(base, time_zone='lst_ldt'))
    events = build_events(gmt.get('predictions') or [], (loc or {}).get('predictions') or [], fallback_off)
    if not events:
        return None, 'nodata', 'NOAA returned no predictions'
    return events, 'ok', ''


def preds_need_refresh(st, now):
    ev = st.get('events') or []
    if not ev:
        return True
    if not any(e['epoch'] > now for e in ev):
        return True
    if max(e['epoch'] for e in ev) - now < 36 * 3600:
        return True
    return now - st.get('fetched', 0) > 23 * 3600


def pred_status(st, now):
    '''0 No Data, 1 Current, 2 Stale, 3 Error.'''
    ev = st.get('events') or []
    if not ev:
        return 3 if st.get('pred_err') else 0
    if not any(e['epoch'] > now for e in ev):
        return 2
    if now - st.get('fetched', 0) > STALE_PRED_S:
        return 2
    return 1


# ---------------------------------------------------------------- tide math
def local_dt(epoch, off):
    return datetime.fromtimestamp(epoch + off, tz=timezone.utc)


def fmt_clock(epoch, off):
    d = local_dt(epoch, off)
    return '%d:%02d %s' % (d.hour % 12 or 12, d.minute, 'AM' if d.hour < 12 else 'PM')


def fmt_us(epoch, off):
    '''US-style date and time in the station's local time: MM/DD/YYYY h:MM AM/PM.'''
    d = local_dt(epoch, off)
    return '%02d/%02d/%04d %s' % (d.month, d.day, d.year, fmt_clock(epoch, off))


def fmt_when(epoch, off, today):
    d = local_dt(epoch, off)
    clock = fmt_clock(epoch, off)
    if today is None:
        return clock
    delta = (d.date() - today).days
    if delta == 0:
        return clock
    if delta == 1:
        return 'Tomorrow ' + clock
    return '%s %s' % (d.strftime('%a'), clock)


def interp_height(events, when):
    '''Cosine interpolation between the surrounding high/low predictions (an estimate).'''
    prev = nxt = None
    for e in events:
        if e['epoch'] <= when:
            prev = e
        else:
            nxt = e
            break
    if prev is None or nxt is None:
        return None
    span = nxt['epoch'] - prev['epoch']
    if span <= 0:
        return prev['v']
    frac = (when - prev['epoch']) / float(span)
    return prev['v'] + (nxt['v'] - prev['v']) * (1 - math.cos(math.pi * frac)) / 2.0


def compute_tides(events, now):
    res = {'est': None, 'trend': 0, 'next': None, 'high': None, 'low': None,
           'today': [], 'tomorrow': [], 'off': 0, 'today_date': None}
    if not events:
        return res
    prev = nxt = None
    for e in events:
        if e['epoch'] <= now:
            prev = e
        elif nxt is None:
            nxt = e
    ref = prev or nxt
    off = ref['off']
    res['off'] = off
    today = datetime.fromtimestamp(now + off, tz=timezone.utc).date()
    res['today_date'] = today
    if nxt:
        res['next'] = nxt
        res['trend'] = 1 if nxt['type'] == 'H' else 2
        res['est'] = interp_height(events, now)
    for e in events:
        if e['epoch'] > now:
            if e['type'] == 'H' and res['high'] is None:
                res['high'] = e
            if e['type'] == 'L' and res['low'] is None:
                res['low'] = e
    tomorrow = today + timedelta(days=1)
    for e in events:
        d = local_dt(e['epoch'], e['off']).date()
        if d == today:
            res['today'].append(e)
        elif d == tomorrow:
            res['tomorrow'].append(e)
    return res


def tide_range(day_events):
    if len(day_events) < 2:
        return None
    vals = [e['v'] for e in day_events]
    return max(vals) - min(vals)


def slot_values(day_events, n, metric):
    out = []
    for i in range(n):
        if i < len(day_events):
            e = day_events[i]
            out.append((1 if e['type'] == 'H' else 2,
                        '%s, %s' % (fmt_clock(e['epoch'], e['off']), fmt_ht(e['v'], metric))))
        else:
            out.append((0, '--'))
    return out


def table_slots(events):
    '''How many tide slots per day this station needs (2 for diurnal, usually 4 for semidiurnal).'''
    counts = {}
    for e in events:
        d = local_dt(e['epoch'], e['off']).date()
        counts[d] = counts.get(d, 0) + 1
    if len(counts) > 2:
        days = sorted(counts)[1:-1]
        best = max(counts[d] for d in days)
    elif counts:
        best = max(counts.values())
    else:
        best = 2
    return max(2, min(4, best))


def table_keep(n):
    keep = {'ST', 'TMRW_RANGE'}
    for i in range(1, n + 1):
        keep.add('TODAY_%d' % i)
        keep.add('TMRW_%d' % i)
    return keep


# ---------------------------------------------------------------- observations
def _obs_epoch(text):
    return int(_parse_ts(text).replace(tzinfo=timezone.utc).timestamp())


def parse_obs(product, rows):
    '''Newest usable reading from a NOAA observation product (requested in GMT).'''
    for row in reversed(rows or []):
        try:
            ep = _obs_epoch(row['t'])
        except (KeyError, ValueError, TypeError):
            continue

        def num(key):
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None
        if product == 'wind':
            spd = num('s')
            if spd is None:
                continue
            return {'epoch': ep, 'spd': spd, 'dir': num('d'), 'dr': row.get('dr') or '', 'gust': num('g')}
        val = num('v')
        if val is None:
            continue
        return {'epoch': ep, 'v': val}
    return None


def fetch_obs(station_id, sensors):
    '''One request per available product. Returns (data dict, list of statuses).'''
    out = {}
    statuses = []
    for prod in OBS_PRODUCTS:
        if prod not in sensors:
            continue
        params = {'station': station_id, 'product': prod, 'range': 1, 'units': 'english',
                  'time_zone': 'gmt', 'format': 'json', 'application': APP_NAME}
        if prod == 'water_level':
            params['datum'] = 'MLLW'
        data, status, _msg = http_get(DATA_URL, params)
        statuses.append(status)
        if status == 'rate':
            break
        if isinstance(data, dict):
            rec = parse_obs(prod, data.get('data'))
            if rec:
                out[prod] = rec
    return out, statuses


def obs_keys(t, st):
    '''Driver ids the Observed node should carry, based on what the source really returned.'''
    ob = st.get('obs') or {}
    src = ob.get('src')
    if not src:
        return None
    cache = t['obs_cache'].get(src['id'])
    if not cache or 'water_level' not in cache['data']:
        return None
    d = cache['data']
    keys = {'ST', 'TIDELVL', 'OBS_AGE'}
    if ob.get('kind') == 1 and st.get('events'):
        keys.add('SURGE')
    if 'water_temperature' in d:
        keys.add('WATERT')
    if 'air_temperature' in d:
        keys.add('TEMPOUT')
    if 'wind' in d:
        keys.add('SPEED')
        if d['wind'].get('dir') is not None:
            keys.add('WINDDIR')
        if d['wind'].get('gust') is not None:
            keys.add('GUST')
    if 'air_pressure' in d:
        keys.add('BARPRES')
    if 'humidity' in d:
        keys.add('CLIHUM')
    return keys


def info_keep(ob):
    '''Driver ids of the Info node: everything except the observation distance, which only exists
    when the observations come from a different (nearby) station.'''
    keep = {'ST', 'LATITUDE', 'LONGITUDE', 'OBS_SRC', 'SUNRISE', 'SUNSET', 'MOON', 'MOON_ILLUM'}
    if ob.get('src') and ob.get('kind') == 2:
        keep.add('DISTANC')
    return keep


def obs_status(t, st, now):
    '''Returns (0 No Data | 1 Current | 2 Stale, age_seconds_or_None).'''
    ob = st.get('obs') or {}
    src = ob.get('src')
    if not src:
        return 0, None
    cache = t['obs_cache'].get(src['id'])
    wl = cache['data'].get('water_level') if cache else None
    if not wl:
        return 0, None
    age = max(0, now - wl['epoch'])
    stale_after = max(1800, 3 * t['cfg']['obs_min'] * 60)
    return (1 if age <= stale_after else 2), age


# ---------------------------------------------------------------- sun / moon (pure Python)
def _sun_hour_ut(lat, lon, day, rising):
    '''UT hour of sunrise or sunset on a calendar day, or None if the sun does not rise/set that day.

    Almanac for Computers (1990), Nautical Almanac Office, US Naval Observatory. Longitude is positive
    east. Accurate to about a minute at ordinary latitudes (worse above 60 degrees).
    '''
    n1 = math.floor(275 * day.month / 9)
    n2 = math.floor((day.month + 9) / 12)
    n3 = 1 + math.floor((day.year - 4 * math.floor(day.year / 4) + 2) / 3)
    n = n1 - n2 * n3 + day.day - 30
    lng_hour = lon / 15.0
    t = n + ((6 - lng_hour) / 24.0 if rising else (18 - lng_hour) / 24.0)
    m = 0.9856 * t - 3.289
    mr = math.radians(m)
    lsun = (m + 1.916 * math.sin(mr) + 0.020 * math.sin(2 * mr) + 282.634) % 360.0
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(lsun)))) % 360.0
    ra += math.floor(lsun / 90.0) * 90.0 - math.floor(ra / 90.0) * 90.0
    ra /= 15.0
    sin_dec = 0.39782 * math.sin(math.radians(lsun))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = ((math.cos(math.radians(SUN_ZENITH)) - sin_dec * math.sin(math.radians(lat)))
             / (cos_dec * math.cos(math.radians(lat))))
    if cos_h > 1 or cos_h < -1:
        return None
    h = math.degrees(math.acos(cos_h))
    h = (360.0 - h if rising else h) / 15.0
    local_mean = h + ra - 0.06571 * t - 6.622
    return (local_mean - lng_hour) % 24.0


def sun_events(lat, lon, local_date, off):
    '''(sunrise_epoch, sunset_epoch) for a station-local calendar day; either is None in polar day/night.'''
    midnight = datetime(local_date.year, local_date.month, local_date.day,
                        tzinfo=timezone.utc).timestamp() - off     # local midnight, as a UTC epoch
    out = []
    for rising in (True, False):
        ut = _sun_hour_ut(lat, lon, local_date, rising)
        if ut is None:
            out.append(None)
            continue
        out.append(int(midnight + (ut * 3600.0 + off) % 86400.0))
    return tuple(out)


def moon_for_epoch(epoch):
    '''(phase index 0-7 matching the moon_phase editor, illumination percent) from the mean lunar month.'''
    age = ((epoch - NEW_MOON_REF) / 86400.0) % SYNODIC_DAYS
    idx = int(age / SYNODIC_DAYS * 8 + 0.5) % 8
    pct = int(round((1 - math.cos(2 * math.pi * age / SYNODIC_DAYS)) / 2 * 100))
    return idx, pct


def astro_for(lat, lon, local_date, off):
    '''Sunrise/sunset (epoch) and moon phase for the station-local date. Keys are left out when unknown.'''
    out = {}
    try:
        rise, sset = sun_events(lat, lon, local_date, off)
        if rise is not None:
            out['sunrise'] = rise
        if sset is not None:
            out['sunset'] = sset
    except Exception as ex:  # noqa
        LOGGER.debug('sun calc failed: %s', ex)
    try:
        noon = datetime(local_date.year, local_date.month, local_date.day, 12,
                        tzinfo=timezone.utc).timestamp() - off
        out['moon_idx'], out['moon_pct'] = moon_for_epoch(noon)
    except Exception as ex:  # noqa
        LOGGER.debug('moon calc failed: %s', ex)
    return out


# ---------------------------------------------------------------- configuration
def _truthy(val):
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


def parse_config(raw):
    cfg = {'metric': False, 'obs_min': 10, 'stations': []}
    if not isinstance(raw, dict):
        return cfg
    cfg['metric'] = _truthy(raw.get('useMetric', False))
    try:
        n = int(float(raw.get('obsPollMinutes', 10)))
    except (TypeError, ValueError):
        n = 10
    cfg['obs_min'] = max(MIN_OBS_MIN, min(120, n))
    lst = raw.get('stations') or []
    if isinstance(lst, dict):
        lst = list(lst.values())
    for item in lst:
        if isinstance(item, dict):
            q = str(item.get('station') or '').strip()
            ov = str(item.get('obsStation') or '').strip()
        else:
            q = str(item or '').strip()
            ov = ''
        if q:
            cfg['stations'].append({'query': q, 'obs': ov})
    return cfg


# ---------------------------------------------------------------- node definitions (profile variants)
_base_defs = {}


def base_nodedef(node_id):
    '''The full nodedef for a node class, read once from this plugin's own profile.json.'''
    if not _base_defs:
        doc = _read_json(PROFILE_FILE) or {}
        for nd in doc.get('nodedefs') or []:
            _base_defs[nd['id']] = nd
    return _base_defs.get(node_id)


def variant_id(base_id, prop_ids, kept):
    '''Nodedef id for a node that reports only kept (a subset of the base nodedef's prop_ids).

    The full set keeps the base id. Any smaller set gets base id + a short stable hash of the set, which
    stays alphanumeric/underscore and well under the 31 character limit.
    '''
    if not kept or len(kept) >= len(prop_ids):
        return base_id
    digest = hashlib.sha1(','.join(sorted(kept)).encode('utf-8')).hexdigest()[:6]
    return '%s_%s' % (base_id, digest)


def variant_nodedef(base, vid, kept):
    '''A copy of the base nodedef under a new id that lists only the kept properties, in profile order.'''
    nd = copy.deepcopy(base)
    nd['id'] = vid
    nd['properties'] = [p for p in base.get('properties') or [] if p['id'] in kept]
    return nd


def _nodedef_for(ctl, cls, keep):
    '''Make sure the hub knows a nodedef listing exactly the properties this node reports.

    Returns the nodedef id the node must use. Falls back to the base id (the node still works, but
    properties it never reports show as empty rows) if the hub refuses the definition; the request is
    not repeated until PROFILE_RETRY_S has passed.
    '''
    base = base_nodedef(cls.id)
    if base is None or not keep:
        return cls.id
    prop_ids = [p['id'] for p in base.get('properties') or []]
    kept = [p for p in prop_ids if p in keep]
    vid = variant_id(cls.id, prop_ids, kept)
    if vid == cls.id:
        return vid
    t = _ts(ctl)
    if vid in t['profiles']:
        return vid
    if time.time() < t['profile_retry']:
        return cls.id
    update = getattr(ctl.poly, 'updateJsonProfile', None)
    if update is None:
        return cls.id
    try:
        resp = update({'nodedefs': [variant_nodedef(base, vid, kept)]}, {'waitResponse': True})
        if isinstance(resp, dict) and resp.get('error'):
            raise RuntimeError(str(resp['error']))
    except Exception as ex:  # noqa
        t['profile_retry'] = time.time() + PROFILE_RETRY_S
        LOGGER.warning('The hub did not accept node definition %s: %s. Using the standard definition for now; '
                       'will retry in %d seconds.', vid, ex, PROFILE_RETRY_S)
        return cls.id
    t['profiles'].add(vid)
    LOGGER.info('Registered node definition %s (%d of %d properties)', vid, len(kept), len(prop_ids))
    return vid


# ---------------------------------------------------------------- node classes
def _d(driver, uom, name):
    return {'driver': driver, 'value': 0, 'uom': uom, 'name': name}


def _keep_drivers(node, keep):
    if keep:
        node.drivers = [d for d in node.drivers if d['driver'] in keep]


class NoaaStatusNode(Node):
    id = 'noaa_status'
    commands = {}
    drivers = [_d('ST', 25, 'NOAA Service'), _d('LAST_OK', 151, 'Last Successful Update'),
               _d('STN_COUNT', 56, 'Stations'), _d('STALE_COUNT', 56, 'Stations With Stale Data')]

    def __init__(self, poly, primary, address, name, keep=None, nodedef_id=None):
        super().__init__(poly, primary, address, name)
        _keep_drivers(self, keep)
        if nodedef_id:
            self.id = nodedef_id


class TideStationNode(Node):
    id = 'tide_station'
    commands = {}
    drivers = [_d('ST', 25, 'Prediction Data'), _d('TIDELVL', 18, 'Estimated Height'),
               _d('TREND', 25, 'Tide Trend'), _d('NEXT_TYPE', 25, 'Next Tide'),
               _d('NEXT_HT', 18, 'Next Tide Height'), _d('TIMEREM', 45, 'Next Tide In'),
               _d('HIGH_HT', 18, 'Next High Height'), _d('HIGH_MIN', 45, 'Next High In'),
               _d('HIGH_TIME', 151, 'Next High At'),
               _d('LOW_HT', 18, 'Next Low Height'), _d('LOW_MIN', 45, 'Next Low In'),
               _d('LOW_TIME', 151, 'Next Low At')]

    def __init__(self, poly, primary, address, name, keep=None, nodedef_id=None):
        super().__init__(poly, primary, address, name)
        _keep_drivers(self, keep)
        if nodedef_id:
            self.id = nodedef_id


class TideTableNode(Node):
    id = 'tide_table'
    commands = {}
    drivers = [_d('ST', 18, 'Todays Tide Range'), _d('TMRW_RANGE', 18, 'Tomorrows Tide Range'),
               _d('TODAY_1', 25, 'Today - Tide 1'), _d('TODAY_2', 25, 'Today - Tide 2'),
               _d('TODAY_3', 25, 'Today - Tide 3'), _d('TODAY_4', 25, 'Today - Tide 4'),
               _d('TMRW_1', 25, 'Tomorrow - Tide 1'), _d('TMRW_2', 25, 'Tomorrow - Tide 2'),
               _d('TMRW_3', 25, 'Tomorrow - Tide 3'), _d('TMRW_4', 25, 'Tomorrow - Tide 4')]

    def __init__(self, poly, primary, address, name, keep=None, nodedef_id=None):
        super().__init__(poly, primary, address, name)
        _keep_drivers(self, keep)
        if nodedef_id:
            self.id = nodedef_id


class ObservedNode(Node):
    id = 'tide_observed'
    commands = {}
    drivers = [_d('ST', 25, 'Observation Data'), _d('TIDELVL', 18, 'Observed Water Level'),
               _d('SURGE', 18, 'Water Level vs Predicted'), _d('WATERT', 17, 'Water Temperature'),
               _d('TEMPOUT', 17, 'Air Temperature'), _d('SPEED', 48, 'Wind Speed'),
               _d('GUST', 48, 'Wind Gust'), _d('WINDDIR', 76, 'Wind Direction'),
               _d('BARPRES', 23, 'Barometric Pressure'), _d('CLIHUM', 51, 'Humidity'),
               _d('OBS_AGE', 45, 'Reading Age')]

    def __init__(self, poly, primary, address, name, keep=None, nodedef_id=None):
        super().__init__(poly, primary, address, name)
        _keep_drivers(self, keep)
        if nodedef_id:
            self.id = nodedef_id


class TideInfoNode(Node):
    id = 'tide_info'
    commands = {}
    drivers = [_d('ST', 25, 'Station Type'), _d('LATITUDE', 14, 'Latitude'), _d('LONGITUDE', 14, 'Longitude'),
               _d('OBS_SRC', 25, 'Observation Source'), _d('DISTANC', 116, 'Observation Distance'),
               _d('SUNRISE', 151, 'Sunrise'), _d('SUNSET', 151, 'Sunset'),
               _d('MOON', 25, 'Moon Phase'), _d('MOON_ILLUM', 51, 'Moon Illumination')]

    def __init__(self, poly, primary, address, name, keep=None, nodedef_id=None):
        super().__init__(poly, primary, address, name)
        _keep_drivers(self, keep)
        if nodedef_id:
            self.id = nodedef_id


# ---------------------------------------------------------------- controller-side state
def _ts(ctl):
    t = getattr(ctl, '_tide', None)
    if t is None:
        t = {'cfg': None, 'started': False, 'work': threading.Lock(), 'stations': {}, 'catalog': None,
             'obs_catalog': None, 'obs_cache': {}, 'next_obs': 0.0, 'force_obs': False,
             'pending_catalog': False, 'sigs': {}, 'status_node': None,
             'adds': {}, 'add_lock': threading.Lock(), 'add_sub': False,
             'profiles': set(), 'profile_retry': 0.0}
        try:
            saved = _read_json(os.path.join(ctl.persist_dir, 'state.json')) or {}
            t['sigs'] = dict(saved.get('sigs') or {})
        except Exception:  # noqa
            pass
        ctl._tide = t
    return t


def _save_state(ctl):
    t = _ts(ctl)
    try:
        _write_json(os.path.join(ctl.persist_dir, 'state.json'), {'sigs': t['sigs']})
    except Exception:  # noqa
        pass


def _set(node, drv, value, uom=None, text=None, force=False):
    if node is None:
        return
    try:
        if not any(d.get('driver') == drv for d in node.drivers):
            return
        node.setDriver(drv, value, report=True, force=force, uom=uom, text=text)
    except Exception:  # noqa
        LOGGER.exception('setDriver %s failed', drv)


def _mins(epoch, now):
    return max(0, int(round((epoch - now) / 60.0)))


def _db_nodes(poly):
    try:
        return list(poly.getNodesFromDb() or [])
    except Exception:  # noqa
        return []


def _db_addresses(poly):
    return set(n.get('address') for n in _db_nodes(poly) if isinstance(n, dict))


def _db_name_map(poly):
    '''address -> the name the hub currently stores for it (None when the record has no name).'''
    return dict((n.get('address'), n.get('name')) for n in _db_nodes(poly) if isinstance(n, dict))


def _station_addrs(poly, sid):
    base = sid.lower()
    return {'tide': poly.getValidAddress('t' + base), 'table': poly.getValidAddress('b' + base),
            'obs': poly.getValidAddress('o' + base), 'info': poly.getValidAddress('i' + base)}


def short_label(clean, name, room):
    '''Shorten a NOAA station name to at most room characters, readably.

    clean is poly.getValidName (removes the characters IoX does not allow). Order of attempts: the
    whole name; the part before the first comma (NOAA's 'Montauk, Fort Pond Bay' becomes 'Montauk');
    a cut at the last word boundary that fits; and only then a hard cut.
    '''
    label = ' '.join(clean(name).split())
    if len(label) <= room:
        return label
    head = label.split(',')[0].strip()
    if head:
        label = head
    if len(label) <= room:
        return label
    cut = label[:room + 1]
    i = cut.rfind(' ')
    label = label[:i] if i >= 4 else label[:room]
    return label.rstrip(' ,.-')


def station_label(meta, metas):
    '''Returns (base, tail) for a station's node names.

    base is the NOAA station name. tail is empty unless another configured station would end up with the
    same SHORT label; then it is the state, or the station ID when the state does not tell them apart.
    '''
    base = (meta.get('name') or meta['id']).strip()
    room = NAME_MAX - len(' ' + LONGEST_SUFFIX)
    plain = lambda s: s
    mine = short_label(plain, base, room).lower()
    same = [m for m in metas if m['id'] != meta['id']
            and short_label(plain, (m.get('name') or m['id']).strip(), room).lower() == mine]
    if not same:
        return base, ''
    state = (meta.get('state') or '').strip()
    if state and not any((m.get('state') or '').strip().upper() == state.upper() for m in same):
        return base, state
    return base, meta['id']


def node_name(clean, base, suffix, tail=''):
    '''Station label plus a suffix (Tides, Tide Table, Observed, Info), at most NAME_MAX characters.

    The station label is sized for the longest suffix, so all of one station's nodes share the very
    same label; only the label is ever shortened, the suffix (and the optional tail) stay whole.
    '''
    tail_s = (' ' + tail) if tail else ''
    room = max(4, NAME_MAX - len(' ' + LONGEST_SUFFIX) - len(tail_s))
    return short_label(clean, base, room) + tail_s + ' ' + suffix


def _cands(found, with_dist=False):
    parts = []
    for s in found:
        label = ('%s %s [%s]' % (s['name'], s['state'], s['id'])).replace('  ', ' ')
        if with_dist and 'dist_nm' in s:
            label += ' %.0f nm' % s['dist_nm']
        parts.append(label)
    return '; '.join(parts)


def _apply_notices(poly, notices):
    try:
        poly.Notices.clear()
        for key, text in notices.items():
            poly.Notices[key] = text
    except Exception:  # noqa
        LOGGER.exception('could not update notices')


# ---------------------------------------------------------------- refresh helpers
def _pred_path(ctl, sid):
    return os.path.join(ctl.persist_dir, 'pred_%s.json' % _safe_name(sid))


def _load_pred_cache(ctl, st):
    cached = _read_json(_pred_path(ctl, st['meta']['id']))
    if cached and cached.get('events'):
        st['events'] = cached['events']
        st['fetched'] = cached.get('fetched', 0)


def _refresh_preds(ctl, st):
    sid = st['meta']['id']
    now = time.time()
    fallback = st['events'][-1]['off'] if st['events'] else 0
    events, status, msg = fetch_predictions(sid, now, fallback)
    if events:
        st['events'] = events
        st['fetched'] = now
        st['pred_err'] = ''
        _write_json(_pred_path(ctl, sid), {'fetched': now, 'events': events})
        LOGGER.info('Predictions refreshed for %s (%d events)', sid, len(events))
    else:
        st['pred_err'] = '%s: %s' % (status, msg)
        st['next_pred_try'] = now + PRED_RETRY_S
        LOGGER.warning('Prediction fetch failed for %s: %s', sid, st['pred_err'])


def _refresh_preds_if_needed(ctl):
    t = _ts(ctl)
    now = time.time()
    for st in t['stations'].values():
        if preds_need_refresh(st, now) and now >= st.get('next_pred_try', 0):
            _refresh_preds(ctl, st)


def _refresh_obs(ctl):
    '''One fetch per distinct observation source, however many stations share it.'''
    t = _ts(ctl)
    srcs = {}
    for st in t['stations'].values():
        src = (st.get('obs') or {}).get('src')
        if src:
            srcs[src['id']] = src
    for oid, src in srcs.items():
        data, statuses = fetch_obs(oid, src.get('sensors') or ['water_level'])
        cache = t['obs_cache'].setdefault(oid, {'data': {}, 'ts': 0})
        cache['data'].update(data)
        cache['ts'] = time.time()
        if 'rate' in statuses:
            LOGGER.warning('NOAA throttling observed while fetching %s', oid)
            break
    t['next_obs'] = time.time() + t['cfg']['obs_min'] * 60


# ---------------------------------------------------------------- hub add-node confirmation
def on_add_node_done(ctl, data):
    '''ADDNODEDONE listener. The hub answers every addNode with a dict carrying the node's
    address; a refusal carries an 'error' key (e.g. 'Request failed with status code 400').'''
    try:
        if not isinstance(data, dict):
            return
        address = data.get('address')
        t = _ts(ctl)
        with t['add_lock']:
            rec = t['adds'].get(address)
        if rec is None:
            return
        err = data.get('error')
        rec['ok'] = not err
        rec['err'] = str(err) if err else ''
        rec['ev'].set()
    except Exception:  # noqa
        LOGGER.exception('add-node answer could not be processed')


def _subscribe_add_done(ctl):
    t = _ts(ctl)
    if t['add_sub']:
        return
    ctl.poly.subscribe(ctl.poly.ADDNODEDONE, lambda data: on_add_node_done(ctl, data))
    t['add_sub'] = True


def _add_confirmed(ctl, node, wait=None):
    '''Send addNode and block until the hub answers for this address.

    rename=True asks the hub to take the node's current name if the node already exists.
    Returns (ok, reason). Must be called from a worker thread, never from an event callback.
    '''
    wait = ADD_WAIT_S if wait is None else wait
    _subscribe_add_done(ctl)
    t = _ts(ctl)
    rec = {'ev': threading.Event(), 'ok': False, 'err': ''}
    with t['add_lock']:
        t['adds'][node.address] = rec
    try:
        ctl.poly.addNode(node, rename=True)
        if not rec['ev'].wait(wait):
            return False, 'no answer from the hub within %d seconds' % int(wait)
        return rec['ok'], rec['err']
    finally:
        with t['add_lock']:
            if t['adds'].get(node.address) is rec:
                t['adds'].pop(node.address, None)


# ---------------------------------------------------------------- node management
def _drop_node(ctl, st, key, address):
    t = _ts(ctl)
    try:
        ctl.poly.delNode(address)
    except Exception:  # noqa
        LOGGER.exception('delNode %s failed', address)
    st['nodes'].pop(key, None)
    st['sig'].pop(key, None)
    st.get('names', {}).pop(key, None)
    t['sigs'].pop(address, None)


def _remove_station(ctl, sid):
    t = _ts(ctl)
    poly = ctl.poly
    base = sid.lower()
    for prefix in ('i', 'o', 'b', 't'):
        address = poly.getValidAddress(prefix + base)
        try:
            poly.delNode(address)
        except Exception:  # noqa
            LOGGER.exception('delNode %s failed', address)
        t['sigs'].pop(address, None)
    t['stations'].pop(sid, None)
    _save_state(ctl)


def _cleanup_orphans(ctl, desired):
    poly = ctl.poly
    keep = set([ctl.address, STATUS_ADDRESS])
    for sid in desired:
        keep.update(_station_addrs(poly, sid).values())
    for n in _db_nodes(poly):
        address = n.get('address') if isinstance(n, dict) else None
        if address and address not in keep:
            LOGGER.info('Removing orphaned node %s', address)
            try:
                poly.delNode(address)
            except Exception:  # noqa
                LOGGER.exception('delNode %s failed', address)
            _ts(ctl)['sigs'].pop(address, None)


def _ensure_status_node(ctl):
    t = _ts(ctl)
    if t['status_node'] is None:
        node = NoaaStatusNode(ctl.poly, ctl.address, STATUS_ADDRESS, 'NOAA Service Status')
        ok, why = _add_confirmed(ctl, node)
        if ok:
            t['status_node'] = node
        else:
            LOGGER.warning('The hub did not accept node %s: %s. Will retry.', STATUS_ADDRESS, why)


def _rename_in_place(ctl, address, name):
    '''Give an existing node a new name without deleting it (programs keep working).'''
    try:
        ctl.poly.renameNode(address, name)
        return True
    except Exception:  # noqa
        LOGGER.exception('renameNode %s failed', address)
        return False


def _ensure_nodes(ctl, sid, force=False):
    '''Create missing nodes / rebuild nodes whose property set changed. Returns True if anything changed.

    Every node is added with the controller as its primary (a flat layout): the hub refused nodes
    nested under the Tides node (three levels) with an HTTP 400, while nodes whose primary is the
    controller are accepted. A node is recorded as created only once the hub confirms it; a refused
    node is retried later with backoff (force=True ignores the backoff).

    Each node uses a nodedef that lists exactly the properties it reports (see _nodedef_for), so the
    console never shows an empty row for a property the node does not have. That definition is
    registered with the hub before the node is added.

    Names are short (NAME_MAX) and share one station label. A node that already exists under a
    different name is renamed in place, never deleted: only a changed nodedef/property set causes a
    delete and re-add.
    '''
    t = _ts(ctl)
    st = t['stations'].get(sid)
    poly = ctl.poly
    if st is None or not st['events']:
        return False
    meta = st['meta']
    addrs = _station_addrs(poly, sid)
    base, tail = station_label(meta, [s['meta'] for s in t['stations'].values()])

    def nm(suffix):
        return node_name(poly.getValidName, base, suffix, tail)

    dbmap = _db_name_map(poly)
    db = set(dbmap)
    ob = st.get('obs') or {}
    src = ob.get('src')
    fails = st.setdefault('fails', {})
    names = st.setdefault('names', {})
    primary = ctl.address
    specs = [('tide', TideStationNode, nm('Tides'), primary, None, '')]
    specs.append(('table', TideTableNode, nm('Tide Table'), primary,
                  table_keep(table_slots(st['events'])), ''))
    okeys = obs_keys(t, st)
    if okeys:
        specs.append(('obs', ObservedNode, nm('Observed'), primary, okeys, src['id']))
    elif 'obs' in st['nodes']:
        _drop_node(ctl, st, 'obs', addrs['obs'])
    specs.append(('info', TideInfoNode, nm('Info'), primary, info_keep(ob), ''))
    changed = False
    for key, cls, name, node_primary, keep, extra in specs:
        nid = _nodedef_for(ctl, cls, keep)
        sig = '%s|%s|%s' % (nid, extra, ','.join(sorted(keep)) if keep else '*')
        address = addrs[key]
        cur = st['nodes'].get(key)
        if cur is not None and st['sig'].get(key) == sig:
            if names.get(key) != name and _rename_in_place(ctl, address, name):
                names[key] = name
            continue
        fail = fails.get(key)
        if fail and not force and time.time() < fail['next']:
            continue
        old = t['sigs'].get(address)
        if cur is not None or (address in db and old is not None and old != sig):
            try:
                poly.delNode(address)
                time.sleep(0.5)
            except Exception:  # noqa
                LOGGER.exception('delNode %s failed', address)
        node = cls(poly, node_primary, address, poly.getValidName(name), keep, nid)
        ok, why = _add_confirmed(ctl, node)
        if ok:
            st['nodes'][key] = node
            st['sig'][key] = sig
            t['sigs'][address] = sig
            names[key] = name
            fails.pop(key, None)
            changed = True
            if address in dbmap and dbmap.get(address) != node.name:
                _rename_in_place(ctl, address, node.name)
        else:
            if cur is not None:  # the old copy was deleted above and its replacement was refused
                st['nodes'].pop(key, None)
                st['sig'].pop(key, None)
                names.pop(key, None)
                t['sigs'].pop(address, None)
            n = (fail['n'] if fail else 0) + 1
            delay = min(ADD_RETRY_MAX_S, ADD_RETRY_FIRST_S * (2 ** (n - 1)))
            fails[key] = {'n': n, 'next': time.time() + delay}
            LOGGER.warning('The hub did not accept node %s (%s): %s. Will retry in %d seconds.',
                           address, name, why, delay)
    if changed:
        _save_state(ctl)
    return changed


# ---------------------------------------------------------------- pushing values to nodes
def _push_station(ctl, st, now, force=False):
    t = _ts(ctl)
    metric = t['cfg']['metric']
    u = uoms(metric)
    comp = compute_tides(st['events'], now)
    nodes = st['nodes']
    today = comp['today_date']

    n = nodes.get('tide')
    if n is not None:
        _set(n, 'ST', pred_status(st, now), force=force)
        if comp['est'] is not None:
            _set(n, 'TIDELVL', conv_ht(comp['est'], metric), uom=u['ht'], force=force)
        _set(n, 'TREND', comp['trend'], force=force)
        nx = comp['next']
        if nx:
            _set(n, 'NEXT_TYPE', 1 if nx['type'] == 'H' else 2, force=force)
            _set(n, 'NEXT_HT', conv_ht(nx['v'], metric), uom=u['ht'], force=force)
            _set(n, 'TIMEREM', _mins(nx['epoch'], now), text=fmt_when(nx['epoch'], nx['off'], today), force=force)
        for key, hkey, mkey, tkey in (('high', 'HIGH_HT', 'HIGH_MIN', 'HIGH_TIME'),
                                      ('low', 'LOW_HT', 'LOW_MIN', 'LOW_TIME')):
            ev = comp[key]
            if ev:
                _set(n, hkey, conv_ht(ev['v'], metric), uom=u['ht'], force=force)
                _set(n, mkey, _mins(ev['epoch'], now), text=fmt_when(ev['epoch'], ev['off'], today), force=force)
                # Time of the tide: the predicted event's own timestamp (what now + minutes*60 rounds to),
                # with a US-format MM/DD/YYYY h:MM AM/PM label in the station's local time.
                _set(n, tkey, int(ev['epoch']), text=fmt_us(ev['epoch'], ev['off']), force=force)

    n = nodes.get('table')
    if n is not None:
        slots = len([d for d in n.drivers if d['driver'].startswith('TODAY_')])
        for prefix, evs in (('TODAY_', comp['today']), ('TMRW_', comp['tomorrow'])):
            for i, (enum, text) in enumerate(slot_values(evs, slots, metric)):
                _set(n, '%s%d' % (prefix, i + 1), enum, text=text, force=force)
        rng = tide_range(comp['today'])
        if rng is not None:
            _set(n, 'ST', conv_ht(rng, metric), uom=u['ht'], force=force)
        rng = tide_range(comp['tomorrow'])
        if rng is not None:
            _set(n, 'TMRW_RANGE', conv_ht(rng, metric), uom=u['ht'], force=force)

    ob = st.get('obs') or {}
    src = ob.get('src')
    n = nodes.get('info')
    if n is not None:
        meta = st['meta']
        _set(n, 'ST', 1 if meta.get('type') == 'S' else 0, force=force)
        _set(n, 'LATITUDE', round(meta['lat'], 4), force=force)
        _set(n, 'LONGITUDE', round(meta['lng'], 4), force=force)
        label = ('%s (%s)' % (src['name'], src['id'])) if src else 'None'
        _set(n, 'OBS_SRC', ob.get('kind', 0) if src else 0, text=label, force=force)
        if src and ob.get('kind') == 2:
            _set(n, 'DISTANC', conv_dist(ob['dist'], metric), uom=u['dist'], force=force)
        if today is not None:
            if st.get('astro_key') != today or force:
                st['astro'] = astro_for(meta['lat'], meta['lng'], today, comp['off'])
                st['astro_key'] = today
            a = st.get('astro') or {}
            if 'sunrise' in a:
                _set(n, 'SUNRISE', a['sunrise'], text=fmt_clock(a['sunrise'], comp['off']), force=force)
            if 'sunset' in a:
                _set(n, 'SUNSET', a['sunset'], text=fmt_clock(a['sunset'], comp['off']), force=force)
            if 'moon_idx' in a:
                _set(n, 'MOON', a['moon_idx'], force=force)
                _set(n, 'MOON_ILLUM', a['moon_pct'], force=force)

    n = nodes.get('obs')
    if n is not None and src:
        cache = t['obs_cache'].get(src['id'])
        d = cache['data'] if cache else {}
        status, age = obs_status(t, st, now)
        _set(n, 'ST', status, force=force)
        wl = d.get('water_level')
        if wl:
            _set(n, 'TIDELVL', conv_ht(wl['v'], metric), uom=u['ht'], force=force)
            _set(n, 'OBS_AGE', int(age // 60), force=force)
            if ob.get('kind') == 1:
                pred = interp_height(st['events'], wl['epoch'])
                if pred is not None:
                    _set(n, 'SURGE', conv_ht(wl['v'] - pred, metric), uom=u['ht'], force=force)
        if 'water_temperature' in d:
            _set(n, 'WATERT', conv_temp(d['water_temperature']['v'], metric), uom=u['temp'], force=force)
        if 'air_temperature' in d:
            _set(n, 'TEMPOUT', conv_temp(d['air_temperature']['v'], metric), uom=u['temp'], force=force)
        w = d.get('wind')
        if w:
            _set(n, 'SPEED', conv_wind(w['spd'], metric), uom=u['wind'], force=force)
            if w.get('gust') is not None:
                _set(n, 'GUST', conv_wind(w['gust'], metric), uom=u['wind'], force=force)
            if w.get('dir') is not None:
                _set(n, 'WINDDIR', int(round(w['dir'])), text=w.get('dr') or None, force=force)
        if 'air_pressure' in d:
            _set(n, 'BARPRES', conv_pres(d['air_pressure']['v'], metric), uom=u['pres'], force=force)
        if 'humidity' in d:
            _set(n, 'CLIHUM', int(round(d['humidity']['v'])), force=force)


def _push_status(ctl, force=False):
    t = _ts(ctl)
    n = t.get('status_node')
    if n is None:
        return
    now = time.time()
    _set(n, 'ST', noaa_status(), force=force)
    ok = _http_state['last_ok']
    if ok:
        _set(n, 'LAST_OK', int(ok), text=time.strftime('%Y-%m-%d %H:%M', time.localtime(ok)), force=force)
    stations = [s for s in t['stations'].values() if s['events']]
    _set(n, 'STN_COUNT', len(stations), force=force)
    stale = 0
    for st in stations:
        if pred_status(st, now) == 2 or obs_status(t, st, now)[0] == 2:
            stale += 1
    _set(n, 'STALE_COUNT', stale, force=force)


def push_all(ctl, force=False):
    t = _ts(ctl)
    if t['cfg'] is None:
        return
    now = time.time()
    for st in t['stations'].values():
        if st['events'] and st['nodes']:
            try:
                _push_station(ctl, st, now, force)
            except Exception:  # noqa
                LOGGER.exception('update failed for station %s', st['meta'].get('id'))
    _push_status(ctl, force)


# ---------------------------------------------------------------- reconcile (config -> nodes)
def _reconcile_once(ctl):
    t = _ts(ctl)
    cfg = t['cfg']
    poly = ctl.poly
    if cfg is None:
        return
    now = time.time()
    notices = {}
    _ensure_status_node(ctl)
    catalog = load_tide_catalog(ctl.persist_dir, force=t.pop('force_catalog', False))
    if not catalog:
        t['pending_catalog'] = True
        _apply_notices(poly, {'noaa': 'Unable to download the NOAA station list yet. The plugin will retry automatically.'})
        _push_status(ctl)
        return
    t['catalog'] = catalog
    t['pending_catalog'] = False
    if not cfg['stations']:
        notices['config'] = ('No tide stations configured. Open the plugin configuration and add a station ID, '
                             'name, or lat,lon. Find station IDs on the NOAA map: %s' % NOAA_MAP_URL)
    desired = {}
    for i, ent in enumerate(cfg['stations']):
        kind, found, extra = resolve_station(ent['query'], catalog)
        key = 'stn%d' % i
        if kind in ('id', 'name'):
            desired.setdefault(found[0]['id'], (found[0], ent['obs']))
        elif kind == 'nearest':
            desired.setdefault(found[0]['id'], (found[0], ent['obs']))
            notices[key] = ('[%s] resolved to the nearest tide station %s at %.0f nm. Other nearby stations: %s. '
                            'To use a different one, enter its station ID.'
                            % (ent['query'], _cands(found[:1]), found[0]['dist_nm'], _cands(found[1:], True)))
        elif kind == 'ambiguous':
            notices[key] = ('[%s] matches %s stations, so none was added. Enter one of these station IDs: %s'
                            % (ent['query'], extra, _cands(found)))
        elif kind == 'invalid':
            notices[key] = '[%s] is not a valid station ID, station name, or lat,lon position.' % ent['query']
        else:
            notices[key] = ('No NOAA tide station found for [%s]. Try the station ID, or lat,lon to find the nearest. '
                            'NOAA station map: %s' % (ent['query'], NOAA_MAP_URL))
    states = t['stations']
    for sid in list(states):
        if sid not in desired:
            _remove_station(ctl, sid)
    need_obs = any((ov or '').strip().lower() not in NO_OBS_WORDS for _m, ov in desired.values())
    obs_catalog = None
    if need_obs:
        obs_catalog = load_obs_catalog(ctl.persist_dir)
        if not obs_catalog:
            t['pending_catalog'] = True
            notices['noaa_obs'] = 'Unable to download the NOAA observation station list yet; observed data will be added once it loads.'
        t['obs_catalog'] = obs_catalog
    no_pred = []
    for sid, (meta, ov) in desired.items():
        st = states.get(sid)
        if st is None:
            st = {'meta': meta, 'events': [], 'fetched': 0, 'next_pred_try': 0, 'pred_err': '',
                  'obs': None, 'nodes': {}, 'sig': {}, 'astro_key': None}
            states[sid] = st
        st['meta'] = meta
        if not st['events']:
            _load_pred_cache(ctl, st)
        if preds_need_refresh(st, now):
            _refresh_preds(ctl, st)
        if not st['events']:
            no_pred.append('%s [%s]' % (meta['name'], sid))
        if obs_catalog:
            src, kind, dist, note = choose_obs_station(meta, obs_catalog, ov)
            st['obs'] = {'src': src, 'kind': kind, 'dist': dist}
            if note:
                notices['obs_%s' % sid] = '%s [%s]: %s' % (meta['name'], sid, note)
        elif not need_obs:
            st['obs'] = {'src': None, 'kind': 0, 'dist': 0.0}
        elif st.get('obs') is None:
            st['obs'] = {'src': None, 'kind': 0, 'dist': 0.0}
    if no_pred:
        notices['noaa'] = 'No prediction data available yet for: %s. Will retry automatically.' % ', '.join(no_pred)
    _refresh_obs(ctl)
    _cleanup_orphans(ctl, desired)
    for sid in desired:
        _ensure_nodes(ctl, sid, force=True)
    push_all(ctl, force=True)
    _apply_notices(poly, notices)
    LOGGER.info('Reconcile complete: %d station(s)', len(desired))


def reconcile(ctl):
    t = _ts(ctl)
    with t['work']:
        try:
            _reconcile_once(ctl)
        except Exception:  # noqa
            LOGGER.exception('reconcile failed')


def request_reconcile(ctl):
    threading.Thread(target=reconcile, args=(ctl,), daemon=True).start()


# ---------------------------------------------------------------- polling
def _tick_work(ctl, long_poll):
    t = _ts(ctl)
    if t['cfg'] is None:
        return
    if long_poll and t['pending_catalog']:
        _reconcile_once(ctl)
        return
    now = time.time()
    _refresh_preds_if_needed(ctl)
    if now >= t['next_obs'] or t['force_obs']:
        t['force_obs'] = False
        _refresh_obs(ctl)
    _ensure_status_node(ctl)
    changed = False
    for sid in list(t['stations']):
        if _ensure_nodes(ctl, sid):
            changed = True
    push_all(ctl, force=changed)


def tick(ctl, long_poll=False):
    t = _ts(ctl)
    if not t['started'] or t['cfg'] is None:
        return

    def runner():
        if not t['work'].acquire(False):
            return
        try:
            _tick_work(ctl, long_poll)
        except Exception:  # noqa
            LOGGER.exception('poll failed')
        finally:
            t['work'].release()
    threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- entry points used by plugin.py
def on_typed_config(ctl, raw):
    t = _ts(ctl)
    t['cfg'] = parse_config(raw)
    if t['started']:
        request_reconcile(ctl)


def start(ctl):
    t = _ts(ctl)
    _subscribe_add_done(ctl)
    t['started'] = True
    if t['cfg'] is None:
        try:
            raw = dict(ctl.CustomTypedData)
        except Exception:  # noqa
            raw = {}
        t['cfg'] = parse_config(raw)
    request_reconcile(ctl)


def stop(ctl):
    _ts(ctl)['started'] = False


def on_discover(ctl):
    _ts(ctl)['force_catalog'] = True
    request_reconcile(ctl)


def on_update_now(ctl):
    t = _ts(ctl)
    t['force_obs'] = True
    for st in t['stations'].values():
        st['fetched'] = 0
        st['next_pred_try'] = 0
    tick(ctl, False)
