# Oscars Watch

The 2027 Oscars death race: every film with a real shot at a nomination, Gold Derby's combined odds, daily odds history and where to watch each film in the US.

Live at https://oscars.raviudeshi.com

## How it updates

A GitHub Actions workflow (`.github/workflows/daily.yml`) runs every morning at 5:17 AM Pacific. It runs `builder/build.py --public`, which fetches Gold Derby's odds, JustWatch, The Numbers, Wikidata, Wikipedia and The Movie Spoiler, adds the day to `builder/history.json`, and commits the refreshed caches. `builder/public.py` then makes the Oscars-only page and the workflow deploys it to GitHub Pages. Run it by hand from the Actions tab (Daily odds refresh, Run workflow).

Watch marks you set on the site stay in your own browser.
