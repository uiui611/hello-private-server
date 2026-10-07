#!/usr/bin/env python3
"""Read existing sysstat history and filesystem metadata; runs locally or over SSH."""
import datetime as dt
import json
import os
import pathlib
import subprocess


def snapshot():
    now = dt.datetime.now(dt.timezone.utc)
    statistics = []
    cpus = None
    errors = []
    history_days = min(9, max(2, int(os.environ.get('RESOURCE_HISTORY_DAYS', '2'))))
    for day in (now.date() - dt.timedelta(days=i) for i in range(history_days - 1, -1, -1)):
        candidates = [pathlib.Path('/var/log/sysstat') / day.strftime('sa%Y%m%d'),
                      pathlib.Path('/var/log/sysstat') / day.strftime('sa%d')]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            errors.append(f'No sysstat file for {day}')
            continue
        try:
            output = subprocess.run(['sadf', '-j', str(path), '--', '-u', '-r', '-S'],
                                    check=True, capture_output=True, text=True, timeout=20)
            host = json.loads(output.stdout)['sysstat']['hosts'][0]
            # saDD may still contain a previous month's data.
            if host['file-date'] != day.isoformat():
                errors.append(f'Stale sysstat file for {day}')
                continue
            cpus = host['number-of-cpus']
            statistics.extend(host['statistics'])
        except (subprocess.SubprocessError, ValueError, KeyError) as exc:
            errors.append(f'sysstat read failed: {type(exc).__name__}')
    mem = {}
    for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    output = subprocess.run([
        'df', '-l', '-B1', '--output=source,fstype,size,used,avail,pcent,itotal,iused,ipcent,target',
        '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs', '-x', 'overlay'],
        check=True, capture_output=True, text=True, timeout=20)
    mounts = []
    for line in output.stdout.splitlines()[1:]:
        fields = line.split(maxsplit=9)
        if len(fields) != 10:
            continue
        device, fs, total, used, avail, percent, itotal, iused, ipercent, mount = fields
        mounts.append(dict(device=device, filesystem=fs, mount=mount,
                           total_bytes=int(total), used_bytes=int(used), free_bytes=int(avail),
                           used_percent=float(percent.rstrip('%')),
                           inode_used_percent=None if ipercent == '-' else float(ipercent.rstrip('%'))))
    return dict(timestamp=now.isoformat(), cpus=cpus, memory_total_bytes=mem['MemTotal'],
                statistics=statistics, mounts=mounts, errors=errors)


if __name__ == '__main__':
    print(json.dumps(snapshot(), separators=(',', ':')))
