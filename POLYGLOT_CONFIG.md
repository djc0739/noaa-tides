# NOAA Tides

Tide predictions and observed conditions for one or more NOAA tide stations. Free NOAA data, no account or API key needed.

## Quick setup

1. Under **Tide stations**, add a row for each station.
2. In **Station ID, name, or lat,lon** enter ONE of:
   * a station ID, for example `8510560`
   * the full station name, optionally followed by a comma and the 2-letter state, for example `Montauk, Fort Pond Bay`
   * your position as `latitude,longitude` in decimal degrees, with a minus sign for west longitude, for example `41.05,-71.96`
3. Click **Save** (the button may be labeled **Save** or **Save Changes**). New nodes appear within about a minute. Problems, such as a station that was not found, are shown in the notices at the top of this page.

Changes to **Use metric units** or **Observation poll interval (minutes)** also need **Save**. To remove a station, delete its row and click **Save**.

## Finding a station

Zip codes are not accepted here, but NOAA's website accepts them. Go to [https://tidesandcurrents.noaa.gov](https://tidesandcurrents.noaa.gov) and enter your zip code, city or state in the Search box. Pick a station, copy its Station ID into the field above, and click **Save**.

## More help

The full documentation covers the nodes and properties, notification variables, polling limits and a glossary of tide terms: [https://github.com/djc0739/noaa-tides/blob/main/README.md](https://github.com/djc0739/noaa-tides/blob/main/README.md)
