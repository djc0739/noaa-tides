# NOAA Tides

Tide predictions and observed marine conditions for one or more NOAA tide stations, straight from the free
NOAA CO-OPS Tides & Currents API (https://tidesandcurrents.noaa.gov). No account, API key or subscription is needed.

> **Not for navigation.** Tide values are predictions from NOAA harmonic data, and the "estimated height" between
> high and low tide is an interpolation. Always use official NOAA products and good seamanship for real decisions.

**New here? Jump straight to [Setup](#setup).** Unfamiliar tide terms are explained in the [Glossary](#glossary), and
notification and Program variables are listed in [Using the values in notifications](#using-the-values-in-notifications).

## What you get

Each station you add creates a small set of themed nodes, all listed directly under the NOAA Tides controller. Node
names start with the station name (shown here as *Station*):

| Node | What it shows |
|------|---------------|
| ***Station* Tides** | Prediction data status (Current / Stale / Error), estimated current height, rising or falling, next tide (type, height, minutes until, time), next high and next low (height, minutes until, date and time) |
| ***Station* Tide Table** | Today's and tomorrow's tides in order (High / Low with time and height), plus today's and tomorrow's tide range |
| ***Station* Observed** | Only if observation data exists: observed water level, water level vs predicted (only on stations with their own sensors), water and air temperature, wind speed / gust / direction, barometric pressure, humidity, and reading age. Only sensors that actually report are listed |
| ***Station* Info** | Reference or subordinate station, latitude and longitude, where observations come from (and how far away, when it is another station), sunrise, sunset, moon phase and illumination |
| **NOAA Service Status** | NOAA connection (Online / Degraded / Rate Limited / Offline), last successful update, station count, number of stations with stale data |

Everything is a normal IoX property, so it can be used in Programs and notifications (for example: "Next Tide In is
below 60", "Tide Trend is Falling", "NOAA Service is not Online", "Prediction Data is Stale"). Predicted values are
labeled **Predicted** and observed values are labeled **Observed**.

The plugin's own connection status is the Controller's built-in status.

### Which properties each node shows

A node lists only the properties it really reports, so you never see an empty row for a sensor a station does not
have. For example, a station without a humidity sensor has an Observed node with no Humidity row, and **Water Level
vs Predicted** only appears when the observations come from the station itself. Behind the scenes the plugin
registers a matching node definition for each combination of properties it needs (their ids look like
`tide_observed_1a2b3c`), so the eisy console, Programs and Scenes all see exactly the same list.

Two things are deliberate:

* **Tide Table** has four tide slots per day on stations with two high and two low tides. On a day with only three
  tides, **Today - Tide 4** shows `--`. That is a real value ("no fourth tide today"), not a missing one, and keeping
  the row means the node does not have to be rebuilt every time the number of tides changes.
* If the sun does not rise or set at all on a given day (polar day or polar night, mainly Alaska in midsummer and
  midwinter), Sunrise and Sunset are not updated that day.

If the number of properties a node reports changes (for example a station gains a humidity sensor), the plugin
rebuilds that one node the next time it starts or the configuration is saved. The node keeps its name and address,
but because IoX treats it as a new node, check any Program or Scene that uses it.

### Node names

Node names are kept to **24 characters or fewer**, because the eisy console cuts off longer names. All four nodes of
a station share the same short station name. For example, a station called Montauk gets `Montauk Tides`,
`Montauk Tide Table`, `Montauk Observed` and `Montauk Info`.

If NOAA's station name is too long to fit, the plugin first uses the part before the first comma (NOAA's
`Montauk, Fort Pond Bay` becomes `Montauk`), and if that is still too long it cuts the name at a word boundary. If two
of your stations end up with the same short name, the state (or, failing that, the station ID) is added to tell them
apart. The observation source is shown on the Info node (Observation Source), not in the node name. IoX removes
characters such as `@`, `(` and `)` from node names, so the plugin does not use them.

### Time of the next high and low tide

Besides "minutes until", the Tides node has **Next High At** and **Next Low At**. They hold the exact time of the next
predicted high and low tide as a Unix timestamp (seconds since 1/1/1970 UTC), which is what makes them usable in
Programs. IoX itself decides how a Unix timestamp is displayed: the eisy console shows it as an ISO 8601 date and
time in the eisy's own time zone, for example `2026-11-04T00:48:00-05:00`. The part after the time, `-05:00`, is the
UTC offset: the local time shown is 5 hours behind UTC (US Eastern Standard Time; Eastern Daylight Time is `-04:00`).

The plugin also sends a US-format text label, `MM/DD/YYYY h:MM AM/PM` in the station's local time (for example
`11/04/2026 12:48 AM`), with the timestamp. The eisy console does not show that label for a timestamp property (it
shows the ISO text above), but other screens may. The values are calculated from the same predicted event the
minutes values use, so they always agree with "Next High In" / "Next Low In", and they only change when the tide
itself rolls over to the next one.

### Sunrise, sunset and moon phase

These are calculated by the plugin itself from the station's latitude and longitude, with no extra software and no
internet request. Sunrise and sunset use the US Naval Observatory's *Almanac for Computers* (1990) method and are
normally within about two minutes of published tables. Moon phase and illumination use the average length of the
lunar month and are good to about a day, which is plenty for the eight named phases. Sunrise and sunset are Unix
timestamps like the tide times above.

## Setup

**Nothing you type on the configuration page takes effect until you click Save.** The button is labeled **Save** or
**Save Changes**, depending on the section of the page; click the one under the section you edited. After saving,
the nodes for a new station appear within about a minute. If a station could not be found, or a name matched several
stations, the reason is shown in the notices at the top of the plugin's page. If you use the Admin Console, close and
reopen it to see new nodes.

1. Open the plugin's **Configuration** page in eisy-ui.
2. Set the options you want, then click **Save**:
   * **Use metric units** - off = feet, F, mph, inHg, miles; on = meters, C, m/s, millibars, km. NOAA data is fetched
     once in English units and converted locally, so changing this never causes extra NOAA requests.
   * **Observation poll interval (minutes)** - see "Polling and NOAA limits" below. Default 10, minimum 6.
3. Under **Tide stations** the list starts empty (no station is pre-selected). Add one row per station. In
   **Station ID, name, or lat,lon** enter any ONE of the following:

   | You know... | Type this | Example |
   |-------------|-----------|---------|
   | the station ID (the number on the station's page on the NOAA map) | the 7-digit ID | `8510560` |
   | the station name | the full NOAA name, or enough of it to match only one station. Add a comma and the 2-letter state to narrow it down | `Montauk, Fort Pond Bay` |
   | your position | `latitude,longitude` in decimal degrees: latitude first, then longitude, separated by a comma. Use a **minus sign** for west longitude (all of the US) and south latitude | `41.05,-71.96` (Montauk, New York) |

   With a position, the nearest tide station is used. Leaving out the minus sign on a US longitude puts the point on
   the wrong side of the world, and no nearby station will be found. A name that is too short to be unique adds
   nothing: `Montauk` alone matches Lake Montauk, Montauk Harbor entrance and Montauk, Fort Pond Bay, so the notices
   list those three station IDs and you enter the one you want.
4. Click **Save**.

**Removing a station:** delete its row, then click **Save**. All of its nodes are removed from the eisy. Any Program
that uses one of those nodes will need to be updated.

### Finding the closest station

The plugin's own configuration does not accept zip codes (that would need an extra third-party lookup service), but
NOAA's website does, and it is the easiest way to find a station:

1. **Search on NOAA's site.** Go to https://tidesandcurrents.noaa.gov and type your zip code, city, or state in the
   **Search** box (its hint reads Station ID / City / State / Zip). Choose a station from the results, copy its
   **Station ID**, paste it into the plugin's **Station ID, name, or lat,lon** field, and click **Save**.
2. **Or use your position.** Enter your dock or boat position as `lat,lon` (right-click any point in Google or Apple
   Maps to copy it) and click **Save**. The nearest station is added, and the plugin's notices list the next-nearest
   stations with their IDs and distances.
3. **Or browse the NOAA station map** at https://tidesandcurrents.noaa.gov/map/ , click a station, copy its Station ID
   into the field, and click **Save**.
4. If a name matches several stations, none is added and the notices list the matching IDs. Enter the one you want and
   click **Save**.

Reference stations have the most data. **Subordinate** stations (most of NOAA's ~3,500 tide stations) only have
high/low predictions and usually no sensors of their own.

## Observation source (automatic nearest station)

Many tide stations only have predictions and no sensors. To still give you real, observed conditions, the plugin
chooses an observation source for each station like this:

1. If the tide station has its own water level sensors, it uses them.
2. Otherwise it uses the **nearest NOAA water level station within 50 nautical miles** and says so clearly: the
   Info node shows the source station and its distance.
3. If nothing is within 50 nm, no Observed node is created (you never see empty nodes).

**Water Level vs Predicted** is only shown when the observations come from the station itself, because comparing
against predictions for a different place would be misleading. It is the observed water level minus the height the
plugin estimates for that moment from the predicted high and low tides, so small differences are not meaningful.

To override, fill in **Observation station (optional)** on a station row: enter a specific station ID, or `none` to
turn observed data off for that station. Click **Save** afterwards. Note that conditions at a nearby station (water temperature, wind, pressure)
can differ from your exact location, especially for wind and water temperature.

## Polling and NOAA limits

NOAA publishes no fixed request quota, but it throttles heavy users and asks apps to space out requests and
request only the data they need. The plugin is built to stay well inside that:

* **Predictions** (4 days of high/low tides) are fetched about **once a day per station** (two requests) and cached on
  disk, so a restart does not re-download them. Tide times never change minute to minute.
* **Observations** are requested every **10 minutes by default**. NOAA's sensors only publish a new reading every
  6 minutes, so polling faster than 6 minutes cannot return new data and would only add load; that is why 6 is the
  minimum. 10 minutes keeps data fresh while keeping request volume low (at most one request per available sensor per
  poll, and stations sharing an observation source share the same requests). Increase it if you have many stations.
* The station catalogs (about 3,500 tide stations and 300 water level stations) are downloaded once and refreshed
  monthly.
* Every NOAA request is spaced at least 1.2 seconds apart, identified with an application name, and on throttling
  (HTTP 429/503) the plugin backs off for 1 to 15 minutes and shows **Rate Limited** on the status node.
* Estimated height, minutes-until values, next-tide times and the tide table are recalculated locally every minute with
  no NOAA traffic.

## Status and stale data

* **NOAA Service Status** node: Online, Degraded (some recent failures), Rate Limited (backing off), Offline
  (repeated failures), plus the time of the last successful NOAA response.
* **Prediction Data** on each station: Current, Stale (cache older than 48 hours or no future tides), No Data or Error.
* **Observation Data** on the Observed node: Current, Stale (no new reading for 30 minutes, or 3 poll intervals), No Data.
  **Reading Age** shows the age of the newest reading in minutes.

Controller commands: **Reload Stations** (re-read the configuration and refresh the station lists) and **Update Now**
(refresh observations and predictions immediately).

## How nodes are created

For each station the plugin adds the Tides node, then the Tide Table, Observed and Info nodes. All of them are added
directly under the controller: the eisy refused nodes nested one level deeper (under the Tides node) with an HTTP 400
error, so the plugin uses the flat layout the eisy accepts. A node is only treated as created once the eisy confirms it;
if the eisy refuses one, a warning with the eisy's reason is written to the plugin log and the plugin retries
automatically (after 30 seconds, backing off to at most 10 minutes between tries; saving the configuration retries
immediately). If a node already exists with an older name, it is renamed in place, never deleted and re-added.

Before a node is added, the plugin sends the eisy the node definition that lists exactly the properties that node
reports (see "Which properties each node shows"). If the eisy refuses that definition, the plugin falls back to the
standard definition, so the node still works but may show an empty row, writes a warning to the log, and tries again
after five minutes.

## Notes and limitations

* All heights are measured from the **MLLW** datum (Mean Lower Low Water, the nautical chart datum; see the
  [Glossary](#glossary)). A height of 0 is the average lower-low-water level and a negative height is below it. MLLW
  is the only datum NOAA allows for subordinate stations and the only one this plugin uses, so it is not shown as a
  property.
* NOAA reports wind in knots, but IoX has no knots unit. English units show wind in **mph** and metric shows **m/s**.
  Observation distance is shown in statute miles (English) or kilometers (metric).
* If a station gains or loses a sensor, the Observed node is rebuilt the next time the plugin starts or the
  configuration is saved.
* Tidal **currents** (speed and direction) use separate station IDs and depth bins and are not included in this
  version.

## Using the values in notifications

Every property can be used in Programs, and in notification and email text with IoX substitution variables:
`${sys.node.<node address>.<property id>}`. The address starts with the plugin's slot number (`n012` on the eisy this
was written on; the node's page in eisy-ui shows the full address on yours, and `${sys.node.#.name}` can be used
for the node that triggered a Program).

| Node | Address | Example |
|------|---------|---------|
| *Station* Tides | `n012_t<station ID>` | `${sys.node.n012_t8510560.TIDELVL}` |
| *Station* Tide Table | `n012_b<station ID>` | `${sys.node.n012_b8510560.TODAY_1}` |
| *Station* Observed | `n012_o<station ID>` | `${sys.node.n012_o8510560.WATERT}` |
| *Station* Info | `n012_i<station ID>` | `${sys.node.n012_i8510560.SUNSET}` |
| NOAA Service Status | `n012_noaa_status` | `${sys.node.n012_noaa_status.ST}` |

Values are in the units chosen by **Use metric units**. In a notification, a property that is a choice shows its
name (for example Rising or High) and a number shows its unit. For example, the text
`Trend: ${sys.node.n012_t8510560.TREND} Next: ${sys.node.n012_t8510560.NEXT_TYPE} Height: ${sys.node.n012_t8510560.TIDELVL}`
sent `Trend: Rising Next: High Height: 0.26 feet`, both from a Program's Run Then and from a time condition. The
Values column below lists the number behind each choice. "In" values are minutes and "At" values are Unix timestamps.

| Node | Property id | Shows | Values |
|------|-------------|-------|--------|
| Tides | `ST` | Prediction Data | 0 No Data, 1 Current, 2 Stale, 3 Error |
| Tides | `TIDELVL` | Estimated Height | height |
| Tides | `TREND` | Tide Trend | 0 Unknown, 1 Rising, 2 Falling |
| Tides | `NEXT_TYPE` | Next Tide | 0 none, 1 High, 2 Low |
| Tides | `NEXT_HT` / `TIMEREM` | Next Tide Height / In | height / minutes |
| Tides | `HIGH_HT` / `HIGH_MIN` / `HIGH_TIME` | Next High Height / In / At | height / minutes / timestamp |
| Tides | `LOW_HT` / `LOW_MIN` / `LOW_TIME` | Next Low Height / In / At | height / minutes / timestamp |
| Tide Table | `ST` / `TMRW_RANGE` | Today's / Tomorrow's Tide Range | height |
| Tide Table | `TODAY_1`-`TODAY_4`, `TMRW_1`-`TMRW_4` | Tide 1 to 4, today and tomorrow | 0 none, 1 High, 2 Low |
| Observed | `ST` | Observation Data | 0 No Data, 1 Current, 2 Stale |
| Observed | `TIDELVL` / `SURGE` | Observed Water Level / Water Level vs Predicted | height |
| Observed | `WATERT` / `TEMPOUT` | Water / Air Temperature | degrees |
| Observed | `SPEED` / `GUST` / `WINDDIR` | Wind Speed / Gust / Direction | speed / speed / degrees |
| Observed | `BARPRES` / `CLIHUM` / `OBS_AGE` | Pressure / Humidity / Reading Age | pressure / % / minutes |
| Info | `ST` | Station Type | 0 Reference, 1 Subordinate |
| Info | `LATITUDE` / `LONGITUDE` | Position | degrees |
| Info | `OBS_SRC` / `DISTANC` | Observation Source / Distance | 0 None, 1 This Station, 2 Nearby Station / distance |
| Info | `SUNRISE` / `SUNSET` | Sunrise / Sunset | timestamp |
| Info | `MOON` / `MOON_ILLUM` | Moon Phase / Illumination | 0-7 phase (0 New Moon, 4 Full Moon) / % |
| NOAA Service Status | `ST` | NOAA Service | 0 Unknown, 1 Online, 2 Degraded, 3 Rate Limited, 4 Offline |
| NOAA Service Status | `LAST_OK` / `STN_COUNT` / `STALE_COUNT` | Last Update / Stations / Stale Stations | timestamp / count / count |

## Glossary

Definitions marked (NOAA) are shortened from the
[NOAA Tides and Currents glossary](https://tidesandcurrents.noaa.gov/glossary.html), which has many more terms.

* **Datum** (NOAA): a base elevation used as a reference for measuring water heights or depths.
* **MLLW, Mean Lower Low Water** (NOAA): a tidal datum, the average of the lower of each day's low waters over NOAA's
  National Tidal Datum Epoch. It is the chart datum for US marine waters, so 0 is the average lower-low-water level.
* **High water (HW)** (NOAA): the maximum height reached by a rising tide.
* **Low water (LW)** (NOAA): the minimum height reached by a falling tide.
* **Higher high water / Lower low water** (NOAA): the highest of the high waters, or the lowest of the low waters, of a
  tidal day.
* **Range of tide** (NOAA): the difference in height between consecutive high and low waters.
* **Tide** (NOAA): the periodic rise and fall of the water caused by the gravitational pull of the Sun, the Moon and
  the Earth.
* **Reference station** (NOAA): a station that has its own independent daily predictions; predictions for subordinate
  stations are worked out from it.
* **Subordinate station** (NOAA): a station whose predictions are worked out from a reference station's. In this plugin
  these have high and low tide predictions only.
* **Water level**: the height of the water relative to a datum (this plugin uses MLLW).
* **Predicted**: calculated in advance from NOAA's harmonic data. It does not include weather.
* **Observed**: measured by a sensor, so it includes the effect of weather.
* **Estimated height**: this plugin's own interpolation of the height between the predicted high and low tides.

## Version history

* **1.3.1** - Documentation only: step-by-step setup with Save reminders, finding a station with NOAA's zip-code
  search, a glossary, notification variable tables, and a plain MIT license. The plugin's behavior is unchanged.
* **1.3.0** - Nodes now list only the properties they really report, so Humidity, Water Level vs Predicted and similar
  rows no longer appear empty. Sunrise, Sunset, Moon Phase and Moon Illumination now work on every install: they are
  calculated by the plugin itself instead of by an add-on library that was not available to the running plugin. The
  `astral` requirement is gone. Existing Info and Observed nodes are rebuilt once (same names and addresses).
* **1.2.2** - All four nodes of a station share one short station name (the part of NOAA's name before the first
  comma, cut at a word boundary if needed).
* **1.2.1** - Shorter node names (24 characters or fewer) that no longer carry the state, station ID or observation
  source; existing nodes are renamed in place.
* **1.2.0** - Flat node layout (every node directly under the controller) after the eisy refused nested nodes;
  nodes are only treated as created once the eisy confirms them; US-format date label for Next High / Next Low At.
* **1.1.0** - Added Next High At and Next Low At.
* **1.0.0** - Initial version.

## Data source

Data is provided by NOAA/NOS Center for Operational Oceanographic Products and Services (CO-OPS). This plugin is not
affiliated with or endorsed by NOAA.

## Acknowledgements

* Tide, water level and weather data: NOAA/NOS Center for Operational Oceanographic Products and Services (CO-OPS).
* Written from NOAA's public API documentation and the udi_interface documentation.
* Sunrise and sunset use the published method from the US Naval Observatory's *Almanac for Computers* (1990), written
  out in Python for this plugin. Its published constants are the same in every implementation of the method. Moon
  phase uses the average length of the lunar month.
* Developed with eisy-ai.

Released under the MIT License (see LICENSE.md).
