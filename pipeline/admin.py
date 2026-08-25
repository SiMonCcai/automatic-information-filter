"""
Simple admin panel for RSS pipeline management.
Requires HTTP Basic Authentication.
"""

import os
import threading
import base64
import binascii
from datetime import datetime
from zoneinfo import ZoneInfo
from http import HTTPStatus

from flask import Flask, render_template, request, jsonify, Response

from .config import Config
from .storage import Storage, SCORING_PROMPT_KEYS, AI_META_FIELDS
from .runner import run_once


app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# Global state
config = Config.from_env()
storage = Storage(config.db_path)
sync_status = {'running': False, 'last_result': None}


def to_beijing(time_str: str | None) -> str | None:
    """Convert UTC-ish DB timestamp string to Asia/Shanghai display string."""
    if not time_str:
        return time_str
    try:
        # sqlite datetime('now') format: YYYY-MM-DD HH:MM:SS (UTC)
        dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('UTC'))
        return dt.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return time_str


def check_auth(username: str, password: str) -> bool:
    """Check username and password against env vars."""
    expected_user = os.getenv('ADMIN_USERNAME')
    expected_pass = os.getenv('ADMIN_PASSWORD')
    if not expected_user or not expected_pass:
        return False
    return username == expected_user and password == expected_pass


def authenticate() -> Response:
    """Send 401 response with Basic Auth challenge."""
    return Response(
        'Authentication required.',
        HTTPStatus.UNAUTHORIZED,
        {'WWW-Authenticate': 'Basic realm="RSS Pipeline Admin"'}
    )


def get_auth() -> tuple[str | None, str | None]:
    """Parse Basic Auth from request."""
    auth = request.authorization
    if auth:
        return auth.username, auth.password

    # Fallback: check Authorization header manually
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Basic '):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode('utf-8')
            if ':' in decoded:
                username, password = decoded.split(':', 1)
                return username, password
        except (binascii.Error, UnicodeDecodeError):
            pass
    return None, None


def sanitize_keywords(values) -> list[str]:
    """Normalize textarea/API keyword lists into unique ordered strings."""
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = values.splitlines()
    elif isinstance(values, list):
        raw_items = []
        for item in values:
            raw_items.extend(str(item or '').splitlines())
    else:
        return []

    seen = set()
    cleaned = []
    for item in raw_items:
        keyword = str(item or '').strip()
        if not keyword:
            continue
        dedup_key = keyword.casefold()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        cleaned.append(keyword)
    return cleaned


@app.before_request
def require_auth():
    """Require HTTP Basic Auth for all requests."""
    username, password = get_auth()
    if not username or not password or not check_auth(username, password):
        return authenticate()
    return None


@app.route('/')
def index():
    """Main admin page."""
    return render_template('admin.html')


@app.route('/api/feeds')
def list_feeds():
    """List all feeds."""
    feeds = storage.list_feeds(enabled_only=False)
    payload = []
    for f in feeds:
        filters = storage.get_feed_keyword_filters(f.id)
        payload.append({
            'id': f.id,
            'name': f.name,
            'url': f.url,
            'enabled': f.enabled,
            'default_author': f.default_author,
            'last_fetched_at': to_beijing(f.last_fetched_at),
            'fetch_error': f.fetch_error,
            'created_at': to_beijing(f.created_at),
            'title_rule_count': len(filters['title_keywords']),
            'content_rule_count': len(filters['content_keywords']),
        })
    return jsonify(payload)


@app.route('/api/feeds', methods=['POST'])
def add_feed():
    """Add a new feed."""
    data = request.get_json()
    name = data.get('name')
    url = data.get('url')
    default_author = data.get('default_author')

    if not name or not url:
        return jsonify({'error': 'name and url are required'}), HTTPStatus.BAD_REQUEST

    try:
        feed = storage.add_feed(name, url)
        if default_author:
            storage.update_feed_default_author(feed.id, default_author)
        return jsonify({'id': feed.id, 'name': feed.name, 'url': feed.url})
    except Exception as e:
        return jsonify({'error': str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route('/api/feeds/<int:feed_id>', methods=['DELETE'])
def delete_feed(feed_id: int):
    """Delete a feed."""
    storage.delete_feed(feed_id)
    return jsonify({'success': True})


@app.route('/api/feeds/<int:feed_id>/toggle', methods=['POST'])
def toggle_feed(feed_id: int):
    """Toggle feed enabled status."""
    feed = storage.get_feed(feed_id)
    if not feed:
        return jsonify({'error': 'Feed not found'}), HTTPStatus.NOT_FOUND
    storage.set_feed_enabled(feed_id, not feed.enabled)
    return jsonify({'enabled': not feed.enabled})


@app.route('/api/feeds/<int:feed_id>/author', methods=['PUT'])
def update_feed_author(feed_id: int):
    """Update feed default author."""
    data = request.get_json()
    default_author = data.get('default_author')
    storage.update_feed_default_author(feed_id, default_author)
    return jsonify({'success': True})


@app.route('/api/feeds/<int:feed_id>/filters')
def get_feed_filters(feed_id: int):
    """Get keyword filters for a feed."""
    feed = storage.get_feed(feed_id)
    if not feed:
        return jsonify({'error': 'Feed not found'}), HTTPStatus.NOT_FOUND
    filters = storage.get_feed_keyword_filters(feed_id)
    return jsonify({
        'feed_id': feed_id,
        'feed_name': feed.name,
        'title_keywords': filters['title_keywords'],
        'content_keywords': filters['content_keywords'],
    })


@app.route('/api/feeds/<int:feed_id>/filters', methods=['PUT'])
def update_feed_filters(feed_id: int):
    """Replace keyword filters for a feed."""
    feed = storage.get_feed(feed_id)
    if not feed:
        return jsonify({'error': 'Feed not found'}), HTTPStatus.NOT_FOUND

    data = request.get_json() or {}
    title_keywords = sanitize_keywords(data.get('title_keywords'))
    content_keywords = sanitize_keywords(data.get('content_keywords'))
    storage.replace_feed_keyword_rules(feed_id, title_keywords, content_keywords)
    return jsonify({
        'success': True,
        'feed_id': feed_id,
        'title_keywords': title_keywords,
        'content_keywords': content_keywords,
    })


@app.route('/api/feeds/<int:feed_id>/discard-logs')
def get_feed_discard_logs(feed_id: int):
    """List recent discard logs for a feed."""
    feed = storage.get_feed(feed_id)
    if not feed:
        return jsonify({'error': 'Feed not found'}), HTTPStatus.NOT_FOUND
    try:
        limit = min(max(int(request.args.get('limit', 20)), 1), 100)
    except ValueError:
        limit = 20
    rows = storage.list_article_discard_logs(feed_id, limit=limit)
    return jsonify([
        {
            **row,
            'created_at': to_beijing(row.get('created_at')),
        }
        for row in rows
    ])


@app.route('/api/sync/status')
def sync_status_api():
    """Get current sync status."""
    unsynced = storage.get_unsynced_count()
    jobs = storage.list_sync_jobs(limit=10)

    return jsonify({
        'sync_running': sync_status['running'],
        'unsynced_count': unsynced,
        'jobs': [{
            'id': j.id,
            'started_at': to_beijing(j.started_at),
            'finished_at': to_beijing(j.finished_at),
            'status': j.status,
            'articles_synced': j.articles_synced,
            'error': j.error_message,
        } for j in jobs],
        'last_result': sync_status['last_result'],
    })


@app.route('/api/sync/trigger', methods=['POST'])
def trigger_sync():
    """Trigger a manual sync run."""
    if sync_status['running']:
        return jsonify({'error': 'Sync already running'}), HTTPStatus.CONFLICT

    def run_in_background():
        sync_status['running'] = True
        try:
            result = run_once(config, storage, dry_run=False)
            sync_status['last_result'] = result
        finally:
            sync_status['running'] = False

    thread = threading.Thread(target=run_in_background, daemon=True)
    thread.start()

    return jsonify({'success': True, 'message': 'Sync started'})


@app.route('/api/scoring/prompts')
def get_scoring_prompts():
    """Return editable AI prompt config."""
    cfg = storage.get_ai_prompt_config()
    return jsonify({
        'dimensions': SCORING_PROMPT_KEYS,
        'prompts': cfg['score_prompts'],
        'score_dimensions': cfg['score_dimensions'],
        'score_prompts': cfg['score_prompts'],
        'meta_fields': AI_META_FIELDS,
        'combined_prompt': cfg['combined_prompt'],
    })


@app.route('/api/scoring/prompts', methods=['PUT'])
def update_scoring_prompts():
    """Persist AI prompt config."""
    data = request.get_json() or {}
    prompts = data.get('prompts') or data.get('score_prompts') or {}
    if not isinstance(prompts, dict):
        return jsonify({'error': 'prompts must be an object'}), HTTPStatus.BAD_REQUEST

    sanitized = {key: str(prompts.get(key, '') or '').strip() for key in SCORING_PROMPT_KEYS}
    combined_prompt = str(data.get('combined_prompt', '') or '').strip()
    storage.set_scoring_prompts(sanitized)
    storage.set_combined_prompt(combined_prompt)
    cfg = storage.get_ai_prompt_config()
    return jsonify({
        'success': True,
        'score_prompts': cfg['score_prompts'],
        'combined_prompt': cfg['combined_prompt'],
    })


def run(host='127.0.0.1', port=5000, debug=False):
    """Run the admin server."""
    print(f"Starting RSS Pipeline Admin on http://{host}:{port}")
    print(f"Database: {config.db_path}")

    # Check env vars
    if not os.getenv('ADMIN_USERNAME') or not os.getenv('ADMIN_PASSWORD'):
        print("WARNING: ADMIN_USERNAME or ADMIN_PASSWORD not set!")
        print("Set them in environment variables for authentication.")

    app.run(host=host, port=port, debug=debug)


def main():
    """CLI entry point for admin server."""
    import argparse
    parser = argparse.ArgumentParser(description="RSS Pipeline Admin Panel")
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
