"""
Authentication Blueprint — admin login with Cloudflare Turnstile captcha.
"""

import requests
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
import config
from backend.rate_limiter import rate_limit

auth_bp = Blueprint('auth', __name__)




def _is_safe_redirect_url(target):
    """Validate that a redirect target is a safe relative URL.

    Rejects absolute URLs (http://..., https://..., etc.), protocol-relative
    URLs (//evil.com), and anything else that doesn't start with a single /.
    This prevents open redirect attacks.
    """
    # Reject anything containing :// (catches http://, https://, ftp://, etc.)
    if '://' in target:
        return False
    # Reject protocol-relative URLs (//evil.com)
    if target.startswith('//'):
        return False
    # Must be a relative path starting with /
    return target.startswith('/')


@auth_bp.route('/login', methods=['GET'])
def login_page():
    if session.get('authenticated'):
        next_url = request.args.get('next', '/')
        if not _is_safe_redirect_url(next_url):
            next_url = '/'
        return redirect(next_url)
    return render_template('login.html',
                           turnstile_site_key=config.TURNSTILE_SITE_KEY,
                           error=None)


@auth_bp.route('/login', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=300)
def login_submit():
    password = request.form.get('password', '')
    turnstile_token = request.form.get('cf-turnstile-response', '')
    next_url = request.form.get('next', '/')

    # Verify Turnstile if configured
    if config.TURNSTILE_SECRET_KEY:
        try:
            ts_res = requests.post(
                'https://challenges.cloudflare.com/turnstile/v0/siteverify',
                data={
                    'secret': config.TURNSTILE_SECRET_KEY,
                    'response': turnstile_token,
                    'remoteip': request.remote_addr,
                },
                timeout=10
            )
            ts_data = ts_res.json()
            if not ts_data.get('success'):
                return render_template('login.html',
                                       turnstile_site_key=config.TURNSTILE_SITE_KEY,
                                       error='Captcha verification failed. Please try again.')
        except Exception:
            return render_template('login.html',
                                   turnstile_site_key=config.TURNSTILE_SITE_KEY,
                                   error='Captcha verification error. Please try again.')

    if not config.ADMIN_PASSWORD_HASH:
        return render_template('login.html',
                               turnstile_site_key=config.TURNSTILE_SITE_KEY,
                               error='Admin password not configured.')

    from werkzeug.security import check_password_hash
    if not check_password_hash(config.ADMIN_PASSWORD_HASH, password):
        return render_template('login.html',
                               turnstile_site_key=config.TURNSTILE_SITE_KEY,
                               error='Invalid password.')

    session['authenticated'] = True
    if not _is_safe_redirect_url(next_url):
        next_url = '/'
    return redirect(next_url)


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login_page'))
