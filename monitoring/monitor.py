#!/usr/bin/env python3
"""Low frequency collection, durable local buffering, and idempotent daily YSQL reports."""
import argparse
import datetime as dt
import fcntl
import json
import logging
import pathlib
import shlex
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
LOG = logging.getLogger('resource-reports')


def run(command, stdin=None, timeout=90):
    # Never log command output: SQL/SSH errors can include connection information.
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'command failed (exit {result.returncode})')
    return result.stdout


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def ysql(config, sql):
    target = config['database']
    command = ['kubectl', 'exec', '-i', '-n', target['namespace'], target['pod'],
               '-c', 'yb-tserver', '--', '/home/yugabyte/bin/ysqlsh', '-X',
               '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-U', target['user'],
               '-d', target['name'], '-At']
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                target['ssh'], shlex.join(command)], sql, timeout=120)


def open_buffer(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS samples (
        server TEXT, kind TEXT, timestamp TEXT, payload TEXT,
        PRIMARY KEY(server, kind, timestamp));
      CREATE TABLE IF NOT EXISTS published (day TEXT PRIMARY KEY);
    ''')
    return db


def collect(config, db, history_days=2):
    helper = pathlib.Path(__file__).with_name('snapshot.py').read_text()
    failed = False
    for server in config['servers']:
        try:
            if server.get('ssh'):
                raw = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           server['ssh'], f'LC_ALL=C RESOURCE_HISTORY_DAYS={history_days} python3 -'], helper)
            else:
                raw = run(['env', 'LC_ALL=C', f'RESOURCE_HISTORY_DAYS={history_days}',
                           'python3', str(pathlib.Path(__file__).with_name('snapshot.py'))])
            snapshot = json.loads(raw)
            for stat in snapshot['statistics']:
                stamp = stat['timestamp']
                if stamp.get('utc') != 1:
                    raise ValueError('sysstat timestamps must be UTC')
                timestamp = f"{stamp['date']}T{stamp['time']}+00:00"
                if not stat.get('cpu-load') or not stat.get('memory'):
                    continue  # restart records have no CPU/memory statistics
                payload = dict(stat, memory_total_bytes=snapshot['memory_total_bytes'],
                               cpus=snapshot['cpus'])
                db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?)',
                           (server['name'], 'resources', timestamp, json.dumps(payload)))
            db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?)',
                       (server['name'], 'storage', snapshot['timestamp'], json.dumps(snapshot['mounts'])))
            db.commit()
            LOG.info('collected %s (%d sysstat records, %d mounts)', server['name'],
                     len(snapshot['statistics']), len(snapshot['mounts']))
            for message in snapshot['errors']:
                LOG.warning('%s: %s', server['name'], message)
        except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            LOG.error('collection failed for %s; other servers continue', server['name'])
            failed = True
    # Retain a recovery day beyond the seven report dates.
    cutoff = (dt.datetime.now(UTC) - dt.timedelta(days=9)).isoformat()
    db.execute('DELETE FROM samples WHERE timestamp < ?', (cutoff,))
    db.commit()
    if failed:
        raise RuntimeError('one or more servers could not be collected')


def daily_report(db, server, day, timezone):
    start = dt.datetime.combine(day, dt.time.min, timezone).astimezone(UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, timezone).astimezone(UTC)
    # CPU records describe the interval ending at their timestamp; storage is a point sample.
    rows = db.execute('SELECT kind, timestamp, payload FROM samples WHERE server = ? AND '
                      "((kind='resources' AND timestamp > ? AND timestamp <= ?) OR "
                      "(kind='storage' AND timestamp >= ? AND timestamp < ?)) ORDER BY timestamp",
                      (server['name'], start.isoformat(), end.isoformat(),
                       start.isoformat(), end.isoformat())).fetchall()
    resources, storage = [], []
    for kind, stamp, payload in rows:
        value = json.loads(payload)
        (resources if kind == 'resources' else storage).append((dt.datetime.fromisoformat(stamp), value))
    cpu, mem, swaps, coverage, io, steal = [], [], [], [], [], []
    for timestamp, value in resources:
        all_cpu = next((c for c in value['cpu-load'] if c['cpu'] == 'all'), None)
        memory = value['memory']
        if all_cpu is None or 'avail' not in memory:
            continue
        seconds = max(0, min(float(value['timestamp']['interval']),
                             (timestamp - start).total_seconds()))
        if seconds == 0:
            continue
        # Busy excludes I/O wait and steal, shown separately.
        cpu.append((100 - all_cpu['idle'] - all_cpu['iowait'] - all_cpu['steal'], seconds))
        io.append((all_cpu['iowait'], seconds))
        steal.append((all_cpu['steal'], seconds))
        total = value['memory_total_bytes']
        available = memory['avail'] * 1024
        mem.append((max(0, total - available), available, total))
        swaps.append(memory.get('swpused', 0) * 1024)
        coverage.append(seconds)

    def weighted(values):
        return round(sum(v * w for v, w in values) / sum(w for _, w in values), 2) if values else None

    mounts = {}
    for timestamp, entries in storage:
        for entry in entries:
            key = (entry['device'], entry['mount'])
            previous = mounts.get(key)
            if previous is None:
                mounts[key] = dict(entry, min_free_bytes=entry['free_bytes'],
                                   max_used_percent=entry['used_percent'], samples=1,
                                   sampled_at=timestamp.isoformat())
            else:
                mounts[key] = dict(entry, min_free_bytes=min(previous['min_free_bytes'], entry['free_bytes']),
                                   max_used_percent=max(previous['max_used_percent'], entry['used_percent']),
                                   samples=previous['samples'] + 1, sampled_at=timestamp.isoformat())
    for key, entry in mounts.items():
        prior = db.execute("SELECT payload FROM samples WHERE server=? AND kind='storage' "
                           'AND timestamp < ? ORDER BY timestamp DESC LIMIT 1',
                           (server['name'], start.isoformat())).fetchone()
        earlier = next((p for p in json.loads(prior[0]) if (p['device'], p['mount']) == key), None) if prior else None
        entry['free_change_bytes'] = entry['free_bytes'] - earlier['free_bytes'] if earlier else None
    covered = min(86400, sum(coverage))
    return dict(schema_version=1, server=server['name'], kind=server.get('kind', 'vm'),
                report_date=day.isoformat(), timezone=str(timezone),
                period_start=start.isoformat(), period_end=end.isoformat(),
                generated_at=dt.datetime.now(UTC).isoformat(),
                samples=len(cpu), coverage_percent=round(covered / 86400 * 100, 1),
                cpu=dict(average_percent=weighted(cpu), max_percent=round(max(v for v, _ in cpu), 2) if cpu else None,
                         iowait_average_percent=weighted(io), steal_average_percent=weighted(steal)),
                memory=dict(average_used_bytes=round(sum(v[0] for v in mem) / len(mem)) if mem else None,
                            max_used_bytes=max(v[0] for v in mem) if mem else None,
                            min_available_bytes=min(v[1] for v in mem) if mem else None,
                            total_bytes=mem[-1][2] if mem else None),
                swap=dict(max_used_bytes=max(swaps) if swaps else None),
                storage=list(mounts.values()), storage_samples=len(storage),
                notes=['CPU maximum is the maximum interval average, not an instantaneous peak.',
                       'Memory used is MemTotal - MemAvailable.',
                       'Missing samples are not treated as zero.'])


def report(config, db, day=None, force=False):
    timezone = ZoneInfo(config.get('timezone', 'Asia/Tokyo'))
    today = dt.datetime.now(timezone).date()
    days = [day] if day else [today - dt.timedelta(days=i) for i in range(7, 0, -1)]
    for date in days:
        if date >= today or date < today - dt.timedelta(days=7):
            raise ValueError('report date must be one of the last seven completed days')
        if not force and db.execute('SELECT 1 FROM published WHERE day=?', (date.isoformat(),)).fetchone():
            continue
        reports = [daily_report(db, s, date, timezone) for s in config['servers']]
        # Do not manufacture empty historical days when there is no collected data at all.
        if not any(r['samples'] or r['storage_samples'] for r in reports):
            continue
        sql = ['BEGIN;']
        for value in reports:
            payload = sql_literal(json.dumps(value, separators=(',', ':')))
            sql.append('INSERT INTO public.daily_resource_reports (report_date, server, report) VALUES '
                       f"({sql_literal(date.isoformat())}::date, {sql_literal(value['server'])}, {payload}::jsonb) "
                       'ON CONFLICT (report_date, server) DO UPDATE SET report=EXCLUDED.report, generated_at=now();')
        sql.append('DELETE FROM public.daily_resource_reports WHERE report_date < '
                   f"{sql_literal((today - dt.timedelta(days=7)).isoformat())}::date;")
        sql.append('COMMIT;')
        ysql(config, '\n'.join(sql))
        db.execute('INSERT OR REPLACE INTO published VALUES (?)', (date.isoformat(),))
        db.commit()
        LOG.info('saved %d daily reports for %s', len(reports), date)
    # Cleanup still runs when there are no new reports (including prolonged outages).
    ysql(config, 'DELETE FROM public.daily_resource_reports WHERE report_date < '
         f"{sql_literal((today - dt.timedelta(days=7)).isoformat())}::date;")
    db.execute('DELETE FROM published WHERE day < ?', ((today - dt.timedelta(days=8)).isoformat(),))
    db.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['collect', 'report'])
    parser.add_argument('--config', default='/etc/resource-reports/config.json')
    parser.add_argument('--date', type=dt.date.fromisoformat)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--history-days', type=int, choices=range(2, 10), default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    config = json.loads(pathlib.Path(args.config).read_text())
    state = pathlib.Path(config.get('state_dir', '/var/lib/resource-reports'))
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'lock').open('w') as lock:
        (state / 'lock').chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open_buffer(state / 'samples.sqlite3') as db:
            if args.action == 'collect':
                collect(config, db, args.history_days)
            else:
                report(config, db, args.date, args.force)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        LOG.error('operation failed: %s (details omitted to protect connection data)', type(exc).__name__)
        raise SystemExit(1)
