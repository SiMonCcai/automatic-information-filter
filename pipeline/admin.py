"""
Simple admin panel for RSS pipeline management.
Requires HTTP Basic Authentication.
"""

import os
import threading
import base64
import binascii
from datetime import datetime
from http import HTTPStatus

from flask import Flask, render_template, request, jsonify, Response

from .config import Config
from .storage import Storage
from .runner import run_once


app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# Global state
config = Config.from_env()
storage = Storage(config.db_path)
sync_status = {'running': False, 'last_result': None}


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
    return jsonify([{
        'id': f.id,
        'name': f.name,
        'url': f.url,
        'enabled': f.enabled,
        'default_author': f.default_author,
        'last_fetched_at': f.last_fetched_at,
        'fetch_error': f.fetch_error,
        'created_at': f.created_at,
    } for f in feeds])


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
            'started_at': j.started_at,
            'finished_at': j.finished_at,
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
