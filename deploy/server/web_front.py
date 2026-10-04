"""Render an HTTPS-only Nginx front end; the bot remains on loopback."""
import argparse
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


def public_host(origin):
    value = urlsplit(origin)
    if (value.scheme != 'https' or not value.hostname or value.username or value.password
            or value.path not in {'', '/'} or value.query or value.fragment
            or value.port not in {None, 443}):
        raise ValueError('Use a public HTTPS origin on port 443')
    host = value.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or '.' not in host or host.endswith('.')
                or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                           for part in host.split('.'))):
            raise ValueError('Use a valid public domain or IPv4 address') from None
        if host.endswith(('.localhost', '.local', '.invalid', '.test', '.example')):
            raise ValueError('Use a public hostname')
    else:
        if address.version != 4 or not address.is_global:
            raise ValueError('Use a public static IPv4 address')
    return host


def render(origin, *, tls=False):
    host = public_host(origin)
    # No project directories are served. Only the ACME challenge has a static root.
    config = '''# Managed by Trading Bot HTTPS deployment.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log off;
    return 444;
}
server {
    listen 80;
    listen [::]:80;
    server_name HOST;
    access_log off;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/trading-bot-web/acme;
        try_files $uri =404;
    }
    location / {
        return 308 https://HOST$request_uri;
    }
}
'''.replace('HOST', host)
    if tls:
        config += '''server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name HOST;
    ssl_certificate /etc/letsencrypt/live/HOST/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/HOST/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:TradingBotTLS:10m;
    ssl_session_tickets off;
    if ($host != "HOST") { return 444; }
    access_log off;
    server_tokens off;
    client_max_body_size 32m;
    add_header Strict-Transport-Security "max-age=86400" always;
    location / {
        proxy_pass http://127.0.0.1:5329;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 300s;
        proxy_redirect off;
    }
}
'''.replace('HOST', host)
    return config


def renewal_service():
    return '''[Unit]
Description=Renew Trading Bot HTTPS certificate
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/trading-bot-certbot/bin/certbot renew --quiet --deploy-hook /usr/local/sbin/trading-bot-web-reload
'''


def renewal_timer():
    return '''[Unit]
Description=Check Trading Bot certificate twice a day

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=3600
Persistent=true

[Install]
WantedBy=timers.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--tls', action='store_true')
    args = parser.parse_args()
    config = render(args.origin, tls=args.tls)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'nginx.conf').write_text(config, encoding='utf-8')
    (args.output / 'renew.service').write_text(renewal_service(), encoding='utf-8')
    (args.output / 'renew.timer').write_text(renewal_timer(), encoding='utf-8')
    (args.output / 'reload.sh').write_text('#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/bin/systemctl reload nginx\n', encoding='utf-8')
    print(public_host(args.origin))


if __name__ == '__main__':
    main()
