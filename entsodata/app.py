"""
ENTSO-E API Proxy Server
========================

A Flask-based proxy server for the ENTSO-E Transparency Platform API.
Provides a simple REST API for fetching day-ahead electricity spot prices
without exposing the API key to clients.

Features:
- Rate limiting (per IP)
- Response caching (reduces ENTSO-E API calls)
- Support for multiple bidding zones
- CORS enabled for web/mobile clients
- Error handling and logging

Author: harbour-spotclock proxy
License: GPL-2.0 (to match harbour-spotclock)
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
import os
from datetime import datetime, timedelta
from functools import lru_cache
import logging
import xml.etree.ElementTree as ET
from typing import Optional, Dict, List
import zoneinfo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Rate limiting: 60 requests per minute per IP
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["60 per minute"],
    storage_uri="memory://"
)

# Configuration
ENTSOE_API_KEY = os.environ.get('ENTSOE_API_KEY', '')
ENTSOE_API_URL = 'https://web-api.tp.entsoe.eu/api'

# Bidding zone EIC codes
BIDDING_ZONES = {
    'FI': '10YFI-1--------U',
    'EE': '10Y1001A1001A39I',
    'SE1': '10Y1001A1001A44P',
    'SE2': '10Y1001A1001A45N',
    'SE3': '10Y1001A1001A46L',
    'SE4': '10Y1001A1001A47J',
    'NO1': '10YNO-1--------2',
    'NO2': '10YNO-2--------T',
    'NO3': '10YNO-3--------J',
    'NO4': '10YNO-4--------9',
    'NO5': '10Y1001A1001A48H',
    'DK1': '10YDK-1--------W',
    'DK2': '10YDK-2--------M',
}

# Cache configuration
cache_ttl = timedelta(hours=1)
cached_responses = {}


def is_cache_valid(cache_key: str) -> bool:
    """Check if cached response is still valid."""
    if cache_key not in cached_responses:
        return False
    
    cached_time, _ = cached_responses[cache_key]
    return datetime.now() - cached_time < cache_ttl


def get_cached_response(cache_key: str) -> Optional[Dict]:
    """Get cached response if valid."""
    if is_cache_valid(cache_key):
        _, response = cached_responses[cache_key]
        logger.info(f"Cache HIT for {cache_key}")
        return response
    
    logger.info(f"Cache MISS for {cache_key}")
    return None


def set_cached_response(cache_key: str, response: Dict):
    """Cache a response."""
    cached_responses[cache_key] = (datetime.now(), response)
    
    if len(cached_responses) > 1000:
        sorted_keys = sorted(cached_responses.keys(), 
                           key=lambda k: cached_responses[k][0])
        for key in sorted_keys[:200]:
            del cached_responses[key]


def format_date_for_entsoe(date: datetime) -> str:
    """Format date for ENTSO-E API (YYYYMMDDHHmm in UTC)."""
    utc_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    return utc_date.strftime('%Y%m%d%H%M')


def parse_xml_response(xml_string: str, tz: zoneinfo.ZoneInfo, target_date: datetime) -> List[Dict]:
    """Parse ENTSO-E XML response and extract hourly prices with quarter detail.

    Handles both PT60M (hourly) and PT15M (quarter-hourly) resolutions.
    Uses absolute timestamps from the XML to avoid position overwrites across periods.
    """
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        logger.error(f"Failed to parse XML: {e}")
        raise ValueError(f"Invalid XML response: {e}")

    # ENTSO-E uses a default namespace
    ns_match = root.tag
    ns = ''
    if ns_match.startswith('{'):
        ns = ns_match.split('}')[0] + '}'

    # Check for error acknowledgement
    if root.tag == f'{ns}Acknowledgement_MarketDocument':
        reason_el = root.find(f'.//{ns}Reason/{ns}text')
        error_msg = reason_el.text if reason_el is not None else "Unknown API error"
        logger.error(f"ENTSO-E API error: {error_msg}")
        raise ValueError(f"ENTSO-E API error: {error_msg}")

    # Collect all points using (hour, quarter) as keys
    all_points = {}
    resolution = 'PT60M'

    for ts in root.findall(f'.//{ns}TimeSeries'):
        for period in ts.findall(f'{ns}Period'):
            res_el = period.find(f'{ns}resolution')
            if res_el is not None and res_el.text:
                resolution = res_el.text.strip()
            
            interval_start_el = period.find(f'.//{ns}timeInterval/{ns}start')
            if interval_start_el is None or not interval_start_el.text:
                continue
                
            start_str = interval_start_el.text.replace('Z', '+00:00')
            try:
                period_start = datetime.fromisoformat(start_str)
            except ValueError:
                continue

            if resolution == 'PT15M':
                delta = timedelta(minutes=15)
            else:
                delta = timedelta(minutes=60)

            for point in period.findall(f'{ns}Point'):
                pos_el = point.find(f'{ns}position')
                price_el = point.find(f'{ns}price.amount')
                if pos_el is None or price_el is None:
                    continue
                position = int(pos_el.text)
                price_eur_mwh = float(price_el.text)
                
                point_start_utc = period_start + (position - 1) * delta
                point_local = point_start_utc.astimezone(tz)
                
                # Filter for the target date
                if point_local.date() == target_date.date():
                    hour = point_local.hour
                    minute = point_local.minute
                    if resolution == 'PT15M':
                        q = minute // 15
                        all_points[(hour, q)] = price_eur_mwh
                    else:
                        all_points[(hour, 0)] = price_eur_mwh

    if not all_points:
        logger.error("No price points found in XML response")
        raise ValueError("No price data found in response")

    logger.info(f"Parsed {len(all_points)} valid points with resolution {resolution} for target date")

    prices = []

    if resolution == 'PT15M':
        for hour in range(24):
            quarter_prices = []
            for q in range(4):
                if (hour, q) in all_points:
                    quarter_prices.append(round(all_points[(hour, q)] / 10, 4))
                else:
                    quarter_prices.append(None)

            valid = [p for p in quarter_prices if p is not None]
            avg_price = round(sum(valid) / len(valid), 2) if valid else 0.0

            prices.append({
                'hour': hour,
                'price': avg_price,
                'quarters': [
                    {'minute': q * 15, 'price': quarter_prices[q]}
                    for q in range(4)
                ]
            })
    else:
        for hour in range(24):
            if (hour, 0) in all_points:
                price_cents_kwh = round(all_points[(hour, 0)] / 10, 2)
                prices.append({
                    'hour': hour,
                    'price': price_cents_kwh,
                    'quarters': []
                })

    prices.sort(key=lambda x: x['hour'])

    if len(prices) != 24:
        logger.warning(f"Expected 24 hourly entries, got {len(prices)}")

    return prices


def fetch_from_entsoe(bidding_zone: str, date: datetime) -> List[Dict]:
    """Fetch day-ahead prices from ENTSO-E API."""
    
    tz_name = get_zone_timezone(bidding_zone)
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception as e:
        logger.warning(f"Could not load timezone {tz_name}, falling back to UTC: {e}")
        tz = zoneinfo.ZoneInfo("UTC")

    local_start = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    
    utc_start = local_start.astimezone(zoneinfo.ZoneInfo("UTC"))
    utc_end = local_end.astimezone(zoneinfo.ZoneInfo("UTC"))
    
    period_start = utc_start.strftime('%Y%m%d%H%M')
    period_end = utc_end.strftime('%Y%m%d%H%M')
    
    eic_code = BIDDING_ZONES.get(bidding_zone.upper())
    if not eic_code:
        raise ValueError(f"Unknown bidding zone: {bidding_zone}")
    
    params = {
        'documentType': 'A44',
        'in_Domain': eic_code,
        'out_Domain': eic_code,
        'periodStart': period_start,
        'periodEnd': period_end,
        'securityToken': ENTSOE_API_KEY
    }
    
    logger.info(f"Fetching prices for {bidding_zone} on {date.date()}")
    
    try:
        response = requests.get(ENTSOE_API_URL, params=params, timeout=10)
        response.raise_for_status()
        prices = parse_xml_response(response.text, tz, date)
        logger.info(f"Successfully fetched {len(prices)} prices for {bidding_zone}")
        return prices
        
    except requests.exceptions.RequestException as e:
        logger.error(f"ENTSO-E API request failed: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error processing ENTSO-E response: {str(e)}")
        raise


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'service': 'ENTSO-E Proxy',
        'api_key_configured': bool(ENTSOE_API_KEY)
    })


@app.route('/zones', methods=['GET'])
def list_zones():
    """List available bidding zones."""
    zones = [
        {'code': code, 'name': get_zone_name(code)}
        for code in sorted(BIDDING_ZONES.keys())
    ]
    return jsonify({
        'zones': zones,
        'count': len(zones)
    })


def get_zone_name(code: str) -> str:
    """Get human-readable name for bidding zone."""
    names = {
        'FI': 'Finland',
        'EE': 'Estonia',
        'SE1': 'Sweden (SE1 - Luleå)',
        'SE2': 'Sweden (SE2 - Sundsvall)',
        'SE3': 'Sweden (SE3 - Stockholm)',
        'SE4': 'Sweden (SE4 - Malmö)',
        'NO1': 'Norway (NO1 - Oslo)',
        'NO2': 'Norway (NO2 - Kristiansand)',
        'NO3': 'Norway (NO3 - Trondheim)',
        'NO4': 'Norway (NO4 - Tromsø)',
        'NO5': 'Norway (NO5 - Bergen)',
        'DK1': 'Denmark (DK1 - West)',
        'DK2': 'Denmark (DK2 - East)',
    }
    return names.get(code.upper(), code)


def get_zone_timezone(bidding_zone: str) -> str:
    """Get timezone for an ENTSO-E bidding zone."""
    bz = bidding_zone.upper()
    if bz == 'FI':
        return 'Europe/Helsinki'
    elif bz == 'EE':
        return 'Europe/Tallinn'
    elif bz.startswith('SE'):
        return 'Europe/Stockholm'
    elif bz.startswith('NO'):
        return 'Europe/Oslo'
    elif bz.startswith('DK'):
        return 'Europe/Copenhagen'
    return 'UTC'


@app.route('/prices/<zone>/<date_str>', methods=['GET'])
@limiter.limit("30 per minute")
def get_prices(zone: str, date_str: str):
    """Get day-ahead electricity prices for a specific zone and date."""
    
    if not ENTSOE_API_KEY:
        logger.error("ENTSO-E API key not configured")
        return jsonify({
            'error': 'Server configuration error',
            'message': 'API key not configured'
        }), 500
    
    zone = zone.upper()
    if zone not in BIDDING_ZONES:
        return jsonify({
            'error': 'Invalid bidding zone',
            'message': f'Unknown zone: {zone}. Use /zones to see available zones.',
            'valid_zones': list(BIDDING_ZONES.keys())
        }), 400
    
    try:
        date = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        # Accept non-zero-padded dates like 2026-3-10
        try:
            parts = date_str.split('-')
            date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            return jsonify({
                'error': 'Invalid date format',
                'message': 'Date must be in YYYY-MM-DD format'
            }), 400
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days_diff = (date - today).days
    
    if days_diff < -90:
        return jsonify({
            'error': 'Date too far in the past',
            'message': 'Data only available for last 90 days'
        }), 400
    
    if days_diff > 2:
        return jsonify({
            'error': 'Date too far in the future',
            'message': 'Day-ahead prices only available for today and tomorrow'
        }), 400
    
    cache_key = f"{zone}_{date_str}"
    cached = get_cached_response(cache_key)
    if cached:
        cached_response = dict(cached)
        cached_response['cached'] = True
        return jsonify(cached_response)
    
    try:
        prices = fetch_from_entsoe(zone, date)
        
        response = {
            'zone': zone,
            'zone_name': get_zone_name(zone),
            'date': date_str,
            'currency': 'EUR',
            'unit': 'cents/kWh',
            'prices': prices,
            'count': len(prices),
            'cached': False
        }
        
        set_cached_response(cache_key, response)
        return jsonify(response)
        
    except ValueError as e:
        return jsonify({
            'error': 'Invalid request',
            'message': str(e)
        }), 400
    
    except requests.exceptions.RequestException as e:
        logger.error(f"ENTSO-E API request failed: {str(e)}")
        return jsonify({
            'error': 'Upstream API error',
            'message': 'Failed to fetch data from ENTSO-E'
        }), 502
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({
            'error': 'Internal server error',
            'message': 'An unexpected error occurred'
        }), 500


@app.route('/prices/<zone>', methods=['GET'])
@limiter.limit("30 per minute")
def get_prices_today(zone: str):
    """Get prices for today (convenience endpoint)."""
    today = datetime.now().strftime('%Y-%m-%d')
    return get_prices(zone, today)


@app.errorhandler(429)
def ratelimit_handler(e):
    """Handle rate limit exceeded."""
    return jsonify({
        'error': 'Rate limit exceeded',
        'message': 'Too many requests. Please try again later.'
    }), 429


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    return jsonify({
        'error': 'Not found',
        'message': 'The requested endpoint does not exist',
        'endpoints': [
            '/health',
            '/zones',
            '/prices/<zone>',
            '/prices/<zone>/<date>'
        ]
    }), 404


if __name__ == '__main__':
    if not ENTSOE_API_KEY:
        logger.warning("=" * 60)
        logger.warning("WARNING: ENTSOE_API_KEY environment variable not set!")
        logger.warning("The proxy will not work without a valid API key.")
        logger.warning("Set it with: export ENTSOE_API_KEY='your-key-here'")
        logger.warning("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting ENTSO-E proxy server on port {port}")
    logger.info(f"Supported zones: {', '.join(sorted(BIDDING_ZONES.keys()))}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
