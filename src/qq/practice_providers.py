"""Resolve configured judge routes without borrowing credentials across endpoints."""
from urllib.parse import urlsplit


def endpoint_identity(value):
    try:
        parsed = urlsplit(str(value or '').rstrip('/'))
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        path = parsed.path.rstrip('/')
        # DeepSeek documents both of these base URL spellings for the same API.
        if parsed.hostname == 'api.deepseek.com' and path == '/v1': path = ''
        return parsed.hostname.lower(), parsed.port or 443, path
    except ValueError:
        return None


def configured_routes(config, credentials):
    if not isinstance(config, dict) or not isinstance(credentials, dict): raise ValueError('invalid configuration')
    primary = config.get('model') or {}
    fallbacks = config.get('fallback_providers') or []
    named = config.get('custom_providers') or []
    if isinstance(named, dict): named = [dict(row, name=name) for name, row in named.items() if isinstance(row, dict)]
    if not isinstance(primary, dict) or not isinstance(fallbacks, list) or not isinstance(named, list): raise ValueError('invalid providers')
    if any(not isinstance(row, dict) for row in [*fallbacks, *named]): raise ValueError('invalid provider')
    routes = []
    for original in [primary, *fallbacks]:
        route = dict(original)
        provider = str(route.get('provider') or '').strip()
        alias = provider[7:] if provider.startswith('custom:') else provider
        identity = endpoint_identity(route.get('base_url'))
        matches = []
        if provider not in {'', 'custom', 'deepseek'}:
            matches = [row for row in named if row.get('name') == alias]
        elif provider == 'custom' and identity:
            matches = [row for row in named if endpoint_identity(row.get('base_url')) == identity]
        # An explicit route credential always wins. Named credentials are
        # inherited only with unambiguous endpoint identity or an explicit name.
        own_secret = any(route.get(k) for k in ('api_key', 'key_env', 'api_key_env'))
        if matches and len(matches) == 1:
            selected = matches[0]
            selected_identity = endpoint_identity(selected.get('base_url'))
            if identity and selected_identity != identity:
                # A named route with a conflicting URL is invalid, never send
                # the named credential to the conflicting endpoint.
                route['api_key'] = None; route['key_env'] = None; route['api_key_env'] = None
            else:
                for field in ('base_url', 'api_mode', 'model'):
                    if not route.get(field): route[field] = selected.get(field)
                if not own_secret:
                    for field in ('api_key', 'key_env', 'api_key_env'): route[field] = selected.get(field)
        elif len(matches) > 1 and not own_secret:
            route['api_key'] = None; route['key_env'] = None; route['api_key_env'] = None
        if provider == 'deepseek' and not route.get('base_url'): route['base_url'] = 'https://api.deepseek.com'
        route['model'] = original.get('default') or original.get('model') or route.get('model')
        # A registry env key is allowed only for its registry host. No generic
        # OPENAI_API_KEY or relay key can silently authenticate a different host.
        env_name = route.get('key_env') or route.get('api_key_env')
        key = route.get('api_key') or (credentials.get(env_name) if isinstance(env_name, str) else None)
        if not key and provider == 'deepseek' and endpoint_identity(route.get('base_url')) == ('api.deepseek.com', 443, ''):
            key = credentials.get('DEEPSEEK_API_KEY')
        route['api_key'] = key
        routes.append(route)
    return routes
