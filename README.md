# Entsodata

A Flask-based proxy server for the ENTSO-E Transparency Platform API. Provides a simple REST API for fetching day-ahead electricity spot prices without exposing the ENTSO-E API key to external clients like web or mobile applications.

## Features

- **Rate Limiting**: IP-based rate limiting using `Flask-Limiter`
- **Response Caching**: In-memory caching to minimize upstream ENTSO-E API calls
- **CORS Support**: Cross-Origin Resource Sharing enabled for web/mobile client integrations
- **Multiple Bidding Zones**: Support for various European bidding zones (e.g. FI, SE1-4, NO1-5, DK1-2, EE)

## Getting Started

### Prerequisites

- Python 3.11+
- ENTSO-E API Key

### Local Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables**:
   ```bash
   export ENTSOE_API_KEY="your-api-key-here"
   export PORT=8080 # Optional, defaults to 5000
   export DEBUG=True # Optional
   ```

3. **Run the application**:
   ```bash
   gunicorn "entsodata.app:app"
   ```
   Or for local development:
   ```bash
   python -m entsodata.app
   ```

## Deployment

### Docker

A Dockerfile is provided to containerize the application:

```bash
docker build -t entsodata .
docker run -p 8080:8080 -e ENTSOE_API_KEY="your-key" entsodata
```

### Kubernetes

Kubernetes deployment manifests are located in the `k8s/` directory.

- `k8s/deployment.yaml`: The deployment specification for `entsodata`.
- `k8s/manifest.yaml`: A complete manifest including Namespace, Secret, Deployment, Service, and Ingress.

To deploy (ensure you update the secret in `manifest.yaml` with your valid API key or use a secret management system first):

```bash
kubectl apply -f k8s/manifest.yaml
```

## Endpoints

- `GET /health` - Health check endpoint.
- `GET /zones` - List available bidding zones.
- `GET /prices/<zone>` - Get prices for today in the given zone.
- `GET /prices/<zone>/<date_str>` - Get prices for a specific date (`YYYY-MM-DD`). 
