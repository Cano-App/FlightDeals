# FlightDeals ✈️

Live: https://cano-app.github.io/FlightDeals/

Shared flight deal alerts. Anyone with the link can add a route (cabin, airlines, dates,
target price, or "anytime" in the next 6 months). A GitHub Actions job checks Google Flights about every 5 minutes (only on Delta, Air France, KLM, United, Southwest, Thai, Qatar, Emirates), which
covers Delta, Air France, KLM, United, Southwest, Thai, Qatar, Emirates and most others.

- **Page:** `index.html`, updates live from Firestore (`flightwatches` collection in `bananagrams-4b5f0`).
- **Checker:** `checker/check.py` via `.github/workflows/check.yml` (uses the `fast-flights` library).
- **Deals:** under your target, 15%+ below the usual price for that watch, or a new lowest price.
- **Phone alerts:** free [ntfy](https://ntfy.sh) app, channel `cano-flightdeals-x7k2`
  (or `cano-flightdeals-x7k2-<watch id>` for one route).
- **Deal board + "Hot off the press":** a city grid of best prices, plus headlines from The Flight Deal
  and Fly4free that mention Lagos, Houston, Denver or San Antonio (`flightnews` collection).
- **Firestore rules:** `firestore.rules` (shared with Bananagrams; paste the whole file).
