#!/usr/bin/env python3
"""Generate bitrix_mcp/data/method_catalog.json from b24-rest-docs (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / 'bitrix_mcp' / 'data' / 'method_catalog.json'
DEFAULT_OVERRIDES = Path(__file__).resolve().parent / 'catalog_data' / 'overrides.json'
DOCS_TARBALL_URL = 'https://codeload.github.com/bitrix-tools/b24-rest-docs/tar.gz/{ref}'
GITHUB_COMMIT_API = 'https://api.github.com/repos/bitrix-tools/b24-rest-docs/commits/{ref}'

METHOD_TOKEN_RE = re.compile(r'^[a-z][a-zA-Z0-9]*(\.[a-zA-Z0-9_]+)+$')
EVENT_NAME_RE = re.compile(r'^on[A-Z]')
SCOPE_RE = re.compile(r'^>\s*Scope:\s*\[`(\w+)`\]', re.MULTILINE)
PERMISSION_RE = re.compile(r'^>\s*Кто может выполнять метод:\s*(.+)$', re.MULTILINE)
H1_RE = re.compile(r'^#\s+(.+)$', re.MULTILINE)
DEPRECATED_NOTE_RE = re.compile(
    r'\{%\s*note\s+warning\s+"DEPRECATED"\s*%\}(.*?)\{%\s*endnote\s*%\}',
    re.DOTALL | re.IGNORECASE,
)

EXACT_ACCESS: dict[str, str] = {
    'methods': 'read',
    'scope': 'read',
    'server.time': 'read',
    'profile': 'read',
    'app.info': 'read',
    'app.option.get': 'read',
    'user.current': 'read',
    'user.get': 'read',
    'user.search': 'read',
    'batch': 'read',
    'event.get': 'read',
    'event.offline.get': 'read',
    'event.offline.list': 'read',
    'crm.deal.fields': 'read',
    'crm.lead.fields': 'read',
    'crm.contact.fields': 'read',
    'crm.company.fields': 'read',
    'crm.item.fields': 'read',
    'crm.status.list': 'read',
    'crm.status.entity.types': 'read',
    'crm.type.list': 'read',
    'crm.activity.list': 'read',
    'crm.activity.get': 'read',
    'tasks.task.list': 'read',
    'tasks.task.get': 'read',
    'tasks.task.getfields': 'read',
    'tasks.task.getFields': 'read',
    'voximplant.statistic.get': 'read',
    'voximplant.line.get': 'read',
}

READ_SUFFIXES = {
    'get',
    'list',
    'fields',
    'getfields',
    'search',
    'count',
    'status',
    'history',
    'items',
    'types',
    'enum',
    'userfield',
    'fieldsget',
    'getlist',
    'getbyid',
    'getchildren',
    'gettree',
    'check',
    'has',
    'is',
    'find',
    'info',
    'time',
    'current',
    'profile',
    'methods',
    'scope',
    'configuration',
    'result',
    'calculate',
    'preview',
}

WRITE_SUFFIXES = {
    'add',
    'create',
    'set',
    'update',
    'register',
    'bind',
    'unbind',
    'send',
    'import',
    'export',
    'move',
    'copy',
    'share',
    'start',
    'stop',
    'pause',
    'resume',
    'complete',
    'approve',
    'reject',
    'attach',
    'detach',
    'upload',
    'download',
    'mute',
    'unmute',
    'invite',
    'join',
    'leave',
    'enable',
    'disable',
    'install',
    'uninstall',
}

DESTRUCTIVE_SUFFIXES = {
    'delete',
    'remove',
    'destroy',
    'clear',
    'drop',
    'purge',
    'wipe',
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs-ref', default='main', help='Git ref for b24-rest-docs tarball')
    parser.add_argument('--docs-dir', default='', help='Local docs directory (skip download)')
    parser.add_argument('--overrides', default=str(DEFAULT_OVERRIDES))
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--strict', action='store_true', help='Fail if SERVER_METHODS are not read')
    parser.add_argument('--check', action='store_true', help='Exit 1 if output would change')
    args = parser.parse_args(argv)

    overrides = load_overrides(Path(args.overrides))
    cleanup_dir = None
    try:
        # Always resolve the ref to an immutable sha so --docs-dir + --check stay deterministic.
        commit = resolve_commit_ref(args.docs_ref, Path(args.docs_dir) if args.docs_dir else None)
        if args.docs_dir:
            docs_root = Path(args.docs_dir)
        else:
            cleanup_dir = Path(tempfile.mkdtemp(prefix='b24-rest-docs-'))
            docs_root = download_docs(args.docs_ref, cleanup_dir)

        methods, events = parse_docs(docs_root)
        catalog = build_catalog(methods, events, overrides, commit=commit)
        rendered = render_catalog(catalog)

        out_path = Path(args.out)
        if args.check:
            if not out_path.exists():
                print(f'Catalog missing: {out_path}', file=sys.stderr)
                return 1
            existing = out_path.read_text(encoding='utf-8')
            if existing != rendered:
                print('Catalog is out of date.', file=sys.stderr)
                return 1
            print('Catalog is up to date.')
            return 0

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding='utf-8')

        if args.report:
            print_report(catalog)
        if args.strict:
            return 0 if validate_server_methods(catalog) else 1
        print(f'Wrote {len(catalog["methods"])} methods to {out_path}')
        return 0
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'methods': []}
    return json.loads(path.read_text(encoding='utf-8'))


def resolve_commit(ref: str) -> str:
    if re.fullmatch(r'[0-9a-f]{40}', ref):
        return ref
    url = GITHUB_COMMIT_API.format(ref=ref)
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'BitrixMCP'})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return str(payload['sha'])


def resolve_commit_ref(ref: str, docs_dir: Path | None = None) -> str:
    """Resolve docs-ref to an immutable sha (API, then local git, then literal ref)."""
    if re.fullmatch(r'[0-9a-f]{40}', ref):
        return ref
    try:
        return resolve_commit(ref)
    except (urllib.error.URLError, OSError, KeyError, ValueError, TimeoutError):
        pass
    if docs_dir is not None:
        git_dir = docs_dir / '.git'
        if git_dir.exists() or (docs_dir / '..' / '.git').exists():
            import subprocess

            try:
                completed = subprocess.run(
                    ['git', '-C', str(docs_dir), 'rev-parse', 'HEAD'],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                sha = completed.stdout.strip()
                if re.fullmatch(r'[0-9a-f]{40}', sha):
                    return sha
            except (OSError, subprocess.CalledProcessError):
                pass
    return ref


def download_docs(ref: str, dest_dir: Path) -> Path:
    url = DOCS_TARBALL_URL.format(ref=ref)
    tarball_path = dest_dir / 'docs.tar.gz'
    print(f'Downloading {url}')
    urllib.request.urlretrieve(url, tarball_path)
    with tarfile.open(tarball_path, 'r:gz') as archive:
        archive.extractall(dest_dir, filter='data')
    children = [path for path in dest_dir.iterdir() if path.is_dir()]
    if not children:
        raise RuntimeError('Docs tarball did not contain a directory.')
    return children[0]


def parse_docs(docs_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    methods: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for path in sorted(docs_root.rglob('*.md')):
        rel = path.relative_to(docs_root).as_posix()
        if '/api-reference/' not in f'/{rel}' and not rel.startswith('api-reference/'):
            # Prefer api-reference tree when present; still accept nested copies.
            if 'api-reference' not in rel:
                continue
        text = path.read_text(encoding='utf-8', errors='replace')
        parsed = parse_markdown_page(text, rel)
        if parsed is None:
            continue
        if parsed['kind'] == 'event':
            events.append(parsed)
        else:
            methods.append(parsed)
    return methods, events


def parse_markdown_page(text: str, rel_path: str) -> dict[str, Any] | None:
    h1_match = H1_RE.search(text)
    if not h1_match:
        return None
    title = h1_match.group(1).strip()
    tokens = title.split()
    method_token = None
    for token in reversed(tokens):
        cleaned = token.strip('`"\'.,;:()[]{}')
        if METHOD_TOKEN_RE.fullmatch(cleaned):
            method_token = cleaned
            break

    if method_token is None:
        # Event pages often look like "# onBookingAdd"
        bare = tokens[-1].strip('`"\'.,;:()[]{}') if tokens else ''
        if EVENT_NAME_RE.match(bare):
            return {
                'kind': 'event',
                'name': bare,
                'title': title,
                'doc_path': strip_md(rel_path),
            }
        return None

    scope_match = SCOPE_RE.search(text)
    permission_match = PERMISSION_RE.search(text)
    deprecated, replaced_by = parse_deprecated(text)
    doc_path = strip_md(rel_path)
    # Normalize doc_path to api-reference/... when possible.
    if 'api-reference/' in doc_path:
        doc_path = doc_path[doc_path.index('api-reference/') + len('api-reference/') :]

    return {
        'kind': 'method',
        'method': method_token,
        'title': title,
        'scope': scope_match.group(1) if scope_match else None,
        'permission': permission_match.group(1).strip() if permission_match else '',
        'deprecated': deprecated,
        'replaced_by': replaced_by,
        'doc_path': doc_path,
        'keywords': keywords_from_method(method_token),
        'source': 'docs',
    }


def parse_deprecated(text: str) -> tuple[bool, str | None]:
    first_h2 = re.search(r'^##\s+', text, re.MULTILINE)
    head = text[: first_h2.start()] if first_h2 else text
    match = DEPRECATED_NOTE_RE.search(head)
    if not match:
        return False, None
    block = match.group(1)
    replaced_by = None
    for token in re.findall(r'[a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9_]+)+', block):
        if METHOD_TOKEN_RE.fullmatch(token):
            replaced_by = token
            break
    return True, replaced_by


def keywords_from_method(method: str) -> list[str]:
    parts = [part for part in method.lower().replace('_', '.').split('.') if part]
    return sorted(set(parts))


def strip_md(path: str) -> str:
    return path.removesuffix('.md')


def classify_access(method: str, override_access: str | None = None) -> tuple[str, str]:
    if override_access:
        return override_access, 'override'
    lower = method.lower()
    exact = {key.lower(): value for key, value in EXACT_ACCESS.items()}
    if lower in exact:
        return exact[lower], 'exact'
    parts = [part for part in lower.replace('-', '.').split('.') if part]
    if not parts:
        return 'unknown', 'unknown'

    # Only the last segment counts — never scan the whole dotted path.
    # CamelCase heads: setSettings → set, deleteFromComment → delete, getFields → get.
    last = parts[-1]
    original_tail = method.split('.')[-1]
    head = re.split(r'(?<=[a-z0-9])(?=[A-Z])', original_tail)[0].lower()
    for token in (last, head):
        if token in DESTRUCTIVE_SUFFIXES:
            return 'destructive', 'suffix'
        if token in WRITE_SUFFIXES:
            return 'write', 'suffix'
        if token in READ_SUFFIXES:
            return 'read', 'suffix'
    return 'unknown', 'unknown'


def build_catalog(
    methods: list[dict[str, Any]],
    events: list[dict[str, Any]],
    overrides: dict[str, Any],
    *,
    commit: str,
) -> dict[str, Any]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in methods:
        key = item['method'].lower()
        by_name[key] = dict(item)

    for item in overrides.get('methods', []):
        method = str(item['method'])
        key = method.lower()
        existing = by_name.get(key, {
            'method': method,
            'title': item.get('title') or method,
            'scope': item.get('scope'),
            'permission': item.get('permission') or '',
            'deprecated': bool(item.get('deprecated', False)),
            'replaced_by': item.get('replaced_by'),
            'doc_path': item.get('doc_path') or '',
            'keywords': item.get('keywords') or keywords_from_method(method),
            'source': 'manual',
        })
        if 'access' in item:
            existing['access'] = item['access']
            existing['access_source'] = 'override'
        if 'scope' in item:
            existing['scope'] = item['scope']
        if 'title' in item:
            existing['title'] = item['title']
        if 'keywords' in item:
            existing['keywords'] = sorted(set(item['keywords']))
        if 'deprecated' in item:
            existing['deprecated'] = bool(item['deprecated'])
        if 'replaced_by' in item:
            existing['replaced_by'] = item['replaced_by']
        if 'doc_path' in item:
            existing['doc_path'] = item['doc_path']
        if existing.get('source') != 'docs':
            existing['source'] = item.get('source') or 'manual'
        elif 'access' in item:
            existing['source'] = 'override'
        by_name[key] = existing

    catalog_methods: list[dict[str, Any]] = []
    for key in sorted(by_name):
        item = by_name[key]
        access = item.get('access')
        access_source = item.get('access_source')
        if access is None:
            access, access_source = classify_access(item['method'])
        catalog_methods.append(
            {
                'method': item['method'],
                'scope': item.get('scope'),
                'access': access,
                'access_source': access_source,
                'title': item.get('title') or item['method'],
                'doc_path': item.get('doc_path') or '',
                'permission': item.get('permission') or '',
                'deprecated': bool(item.get('deprecated', False)),
                'replaced_by': item.get('replaced_by'),
                'keywords': sorted(set(item.get('keywords') or keywords_from_method(item['method']))),
                'source': item.get('source') or 'docs',
            }
        )

    payload = {
        'version': 1,
        'source': {
            'repository': 'https://github.com/bitrix-tools/b24-rest-docs',
            'commit': commit,
        },
        'methods': catalog_methods,
        'events': sorted(
            (
                {
                    'name': event['name'],
                    'title': event.get('title') or event['name'],
                    'doc_path': event.get('doc_path') or '',
                }
                for event in events
            ),
            key=lambda item: item['name'].lower(),
        ),
    }
    digest_source = json.dumps(
        {'methods': payload['methods'], 'events': payload['events']},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    payload['sourceDigest'] = hashlib.sha256(digest_source.encode('utf-8')).hexdigest()
    return payload


def render_catalog(catalog: dict[str, Any]) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def print_report(catalog: dict[str, Any]) -> None:
    methods = catalog['methods']
    by_access: dict[str, int] = {}
    unknown = 0
    deprecated = 0
    for item in methods:
        by_access[item['access']] = by_access.get(item['access'], 0) + 1
        if item['access'] == 'unknown':
            unknown += 1
        if item['deprecated']:
            deprecated += 1
    print('Methods:', len(methods))
    print('Events:', len(catalog['events']))
    print('Access:', json.dumps(by_access, sort_keys=True))
    print('Unknown:', unknown)
    print('Deprecated:', deprecated)
    print('Commit:', catalog['source'].get('commit'))
    print('Digest:', catalog['sourceDigest'])


SERVER_METHODS = [
    'crm.item.list',
    'crm.item.get',
    'crm.item.fields',
    'crm.type.list',
    'crm.activity.list',
    'crm.activity.get',
    'crm.deal.list',
    'crm.deal.fields',
    'crm.lead.list',
    'crm.lead.fields',
    'crm.contact.list',
    'crm.contact.fields',
    'crm.company.list',
    'crm.company.fields',
    'crm.status.list',
    'crm.status.entity.types',
    'crm.lead.userfield.list',
    'crm.deal.userfield.list',
    'crm.company.userfield.list',
    'crm.contact.userfield.list',
    'crm.quote.userfield.list',
    'crm.lead.userfield.get',
    'crm.deal.userfield.get',
    'crm.company.userfield.get',
    'crm.contact.userfield.get',
    'crm.quote.userfield.get',
    'tasks.task.list',
    'tasks.task.get',
    'user.get',
    'voximplant.statistic.get',
    'voximplant.line.get',
    'methods',
    'scope',
]


def validate_server_methods(catalog: dict[str, Any]) -> bool:
    by_name = {item['method'].lower(): item for item in catalog['methods']}
    ok = True
    for method in SERVER_METHODS:
        item = by_name.get(method.lower())
        if item is None:
            print(f'SERVER_METHODS missing: {method}', file=sys.stderr)
            ok = False
            continue
        if item['access'] != 'read':
            print(f'SERVER_METHODS not read: {method} -> {item["access"]}', file=sys.stderr)
            ok = False
    return ok


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        raise SystemExit(1)
