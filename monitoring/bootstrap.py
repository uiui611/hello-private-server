#!/usr/bin/env python3
"""Create the dedicated database, least-privilege roles, and reader Secret without logging credentials."""
import json
import pathlib
import secrets
import shlex

from monitor import run, sql_literal, ysql


def main():
    config = json.loads(pathlib.Path('/etc/resource-reports/config.json').read_text())
    target = config['database']
    admin = dict(config, database=dict(target, user='yugabyte', name='yugabyte'))
    prefix = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', target['ssh']]
    get_secret = ['kubectl', 'get', 'secret', 'resource-reports-reader', '-n', target['namespace'],
                  '--ignore-not-found', '-o', 'json']
    existing = run(prefix + [shlex.join(get_secret)]).strip()
    if existing:
        import base64
        password = base64.b64decode(json.loads(existing)['data']['password']).decode()
    else:
        password = secrets.token_urlsafe(32)
    for role, role_password in [('resource_report_reader', password),
                                ('resource_report_writer', secrets.token_urlsafe(32))]:
        # Do not change existing writer credentials during reruns.
        ysql(admin, f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={sql_literal(role)}) "
             f"THEN CREATE ROLE {role} LOGIN PASSWORD {sql_literal(role_password)}; END IF; END $$;")
    # Keep reader role consistent with the existing or freshly generated Secret.
    ysql(admin, f'ALTER ROLE resource_report_reader PASSWORD {sql_literal(password)};')
    exists = ysql(admin, "SELECT 1 FROM pg_database WHERE datname='resource_reports';").strip()
    if not exists:
        ysql(admin, 'CREATE DATABASE resource_reports WITH COLOCATION = true;')
    schema_admin = dict(admin, database=dict(admin['database'], name='resource_reports'))
    ysql(schema_admin, pathlib.Path(__file__).with_name('schema.sql').read_text())
    secret = dict(apiVersion='v1', kind='Secret', type='Opaque',
                  metadata=dict(name='resource-reports-reader', namespace=target['namespace']),
                  stringData=dict(password=password))
    run(prefix + ['kubectl apply -f -'], json.dumps(secret))
    print('Dedicated database, reader/writer roles and reader Secret are ready.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Bootstrap failed: {type(exc).__name__}; connection details omitted.')
        raise SystemExit(1)
