# Utils MCP Server

MCP server for general travel and local utility lookups:

- weather forecasts for places like `Porto, Portugal`
- events around a geocoded place
- airport/city lookup to resolve IATA codes
- flight offers and cached cheap-date ideas
- route/city demand insights from Amadeus

It uses:

- [Open-Meteo](https://open-meteo.com/en/docs) for weather and geocoding
- [Ticketmaster Discovery API](https://developer.ticketmaster.com/products-and-docs/apis/discovery-manual/v2/) for events
- [Amadeus Self-Service APIs](https://developers.amadeus.com/self-service/apis-docs/guides/developer-guides/resources/flights/) for flights and travel demand

## Tools

- `find_travel_location` (`utils_find_travel_location` via proxy) – resolve airport/city names to IATA codes
- `search_flights` (`utils_search_flights` via proxy) – search flight offers for a route and date
- `search_flight_date_deals` (`utils_search_flight_date_deals` via proxy) – cached cheap-date ideas for a route
- `get_flight_market_demand` (`utils_get_flight_market_demand` via proxy) – booked / traveled / busiest-period demand insights
- `search_events` (`utils_search_events` via proxy) – geocode a place and search nearby Ticketmaster events
- `get_weather_forecast` (`utils_get_weather_forecast` via proxy) – current weather plus forecast

## Environment variables

| Variable                         | Required   | Default                        | Description                                                             |
| -------------------------------- | ---------- | ------------------------------ | ----------------------------------------------------------------------- |
| `ENV`                            | No         | —                              | Set to `local` to use plain env vars instead of Secret Manager          |
| `API_KEY`                        | Local only | —                              | MCP gateway key for local development                                   |
| `API_KEY_SECRET_ID`              | Non-local  | `utils-mcp-api-key`            | Secret Manager secret id for MCP gateway key                            |
| `AMADEUS_API_KEY`                | Local only | —                              | Amadeus API key                                                         |
| `AMADEUS_API_SECRET`             | Local only | —                              | Amadeus API secret                                                      |
| `AMADEUS_API_KEY_SECRET_ID`      | Non-local  | `amadeus-api-key`              | Secret Manager secret id for Amadeus API key                            |
| `AMADEUS_API_SECRET_SECRET_ID`   | Non-local  | `amadeus-api-secret`           | Secret Manager secret id for Amadeus API secret                         |
| `AMADEUS_API_BASE_URL`           | No         | `https://test.api.amadeus.com` | Use `https://api.amadeus.com` with production credentials for live data |
| `TICKETMASTER_API_KEY`           | Local only | —                              | Ticketmaster Discovery API key                                          |
| `TICKETMASTER_API_KEY_SECRET_ID` | Non-local  | `ticketmaster-api-key`         | Secret Manager secret id for Ticketmaster key                           |
| `PORT`                           | No         | `8080`                         | HTTP port                                                               |

Open-Meteo does not require a key for this usage.

## Local setup

```bash
cd global/mcp/utils-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY=local-dev-key
export AMADEUS_API_KEY=your-amadeus-key
export AMADEUS_API_SECRET=your-amadeus-secret
export TICKETMASTER_API_KEY=your-ticketmaster-key

python main.py
```

Endpoints:

- MCP: `http://localhost:8080/mcp-server/mcp`
- Health: `http://localhost:8080/health`

## Production secrets

Create the MCP key plus upstream provider secrets:

```bash
MCP_API_KEY=$(openssl rand -hex 32)
echo -n "$MCP_API_KEY" | gcloud secrets versions add utils-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Create or update:

- `utils-mcp-api-key`
- `amadeus-api-key`
- `amadeus-api-secret`
- `ticketmaster-api-key`

Grant the Cloud Run runtime service account access:

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for SECRET in utils-mcp-api-key amadeus-api-key amadeus-api-secret ticketmaster-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project=it-team-hw-project \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

## Deploying to Cloud Run

```bash
cd global/mcp/utils-mcp
gcloud run deploy utils-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=utils-mcp-api-key" \
  --no-allow-unauthenticated
```

If you want live Amadeus production data, also set:

```bash
--set-env-vars="AMADEUS_API_BASE_URL=https://api.amadeus.com"
```

## Register with MCP proxy

Cloud Run is deployed without public access, so the proxy should use `cloud_run_iam` and still send the app key:

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '[{
    "id": "utils",
    "url": "https://your-utils-mcp-url/mcp-server/mcp",
    "upstream_auth": "cloud_run_iam",
    "credentials_header": "X-API-Key: <utils-mcp-api-key>",
    "enabled": true
  }]'
```

Grant the proxy service account invoker access:

```bash
gcloud run services add-iam-policy-binding utils-mcp \
  --region=europe-west1 \
  --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
  --role=roles/run.invoker
```

## Notes

- `search_events` depends on Ticketmaster coverage. Some places may return sparse results even when the geocoding step succeeds.
- `search_flight_date_deals` returns cached date ideas from Amadeus, while `search_flights` is the better tool for current route/date pricing.
- `get_flight_market_demand` is best for demand trends and route popularity, not for a real-time booking count.
