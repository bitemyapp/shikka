#!/usr/bin/env python3
"""Short real-ripgrep benchmark. Use a disposable, clean checkout on Linux."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import struct
import subprocess
import time

p = argparse.ArgumentParser()
p.add_argument('--app', required=True)
p.add_argument('--repo', required=True, type=Path)
p.add_argument('--state', required=True, type=Path)
p.add_argument('--nativelink', required=True)
p.add_argument('--output', required=True, type=Path)
a = p.parse_args()
root = a.repo.resolve()
a.state.mkdir(mode=0o700, parents=True, exist_ok=True)
a.output.mkdir(parents=True, exist_ok=True)
base = [a.app, '--cache', 'managed', '--nativelink', a.nativelink, '--state-dir', str(a.state), '--local-content-store', str(a.state / 'content-store'), '-j', '12', '-p', 'ripgrep', '--bin', 'rg']
records = []
def invoke(name, extra=(), cwd=root):
    started = time.monotonic()
    result = subprocess.run(base + ['--buck-arg=--event-log=' + str(a.output / (name + '.json-lines')), 'build', *extra], cwd=cwd, text=True, capture_output=True)
    seconds = time.monotonic() - started
    (a.output / (name + '.log')).write_text(result.stdout + result.stderr)
    if result.returncode: raise RuntimeError(result.stderr)
    matches = re.findall(r'Commands: (\d+) \(cached: (\d+), remote: (\d+), local: (\d+)\)', result.stderr)
    record = {'name': name, 'seconds': seconds, 'actions': dict(zip(['total','cached','remote','local'], map(int,matches[-1]))) if matches else {'total':0,'cached':0,'remote':0,'local':0}}
    report = cwd / 'buck-out/shikka/local-cas-report.json'
    if report.exists(): record['sharing'] = json.loads(report.read_text())
    records.append(record)
    print(json.dumps(record), flush=True)

def verify(cwd=root):
    result = subprocess.run(base + ['run', '--', '--version'], cwd=cwd, text=True, capture_output=True, check=True)
    assert result.stdout.startswith('ripgrep 15.2.0'), result.stdout

assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True), 'Use a clean disposable checkout'
subprocess.run([a.app, "generate" if (root / ".shikka/project.json").exists() else "init"], cwd=root, check=True)
small = root / 'crates/core/main.rs'
deep = root / 'crates/matcher/src/lib.rs'
original = {path: path.read_bytes() for path in [small,deep]}
try:
    invoke('cold')
    verify()
    for i in range(3): invoke('unchanged-' + str(i))
    for name,path in [('small',small),('deep',deep)]:
        for i in range(3):
            path.write_bytes(original[path] + f'\n/// Temporary incremental benchmark probe.\n#[allow(dead_code)] pub fn shikka_benchmark_change() -> u64 {{ {i+1} }}\n'.encode())
            invoke(name + '-' + str(i))
            verify()
        path.write_bytes(original[path])
        invoke(name + '-restore')
    # A second checkout has no local Buck outputs/daemon history. No symlinked
    # roots: exercise actual action reuse from the shared service.
    fresh = root.with_name(root.name + '-fresh')
    subprocess.run(['git','clone','--local',str(root),str(fresh)], check=True, capture_output=True)
    subprocess.run([a.app, 'init'], cwd=fresh, check=True, capture_output=True)
    invoke('fresh-checkout', cwd=fresh)
    verify(fresh)
    # A service restart must preserve the cache. The already built checkout is
    # not enough evidence, so use a third clean checkout after restart.
    subprocess.run([a.app,'--state-dir',str(a.state),'cache','stop'],check=True,capture_output=True)
    restarted = root.with_name(root.name + '-restart')
    subprocess.run(['git','clone','--local',str(root),str(restarted)],check=True,capture_output=True)
    subprocess.run([a.app,'init'],cwd=restarted,check=True,capture_output=True)
    invoke('cache-restart',cwd=restarted)
    verify(restarted)
    # FIEMAP unions across owners: logical bytes and mapped physical ranges are
    # deliberately distinct. Inline/encoded/unknown mappings are reported.
    ranges = {}
    logical = allocated = files = excluded = 0
    roots = [root/'buck-out',fresh/'buck-out',restarted/'buck-out',a.state/'cas',a.state/'content-store']
    for directory in roots:
        for path in directory.rglob('*'):
            if path.is_symlink() or not path.is_file(): continue
            info = path.stat()
            if not info.st_size: continue
            files += 1; logical += info.st_size; allocated += info.st_blocks * 512
            fd = os.open(path, os.O_RDONLY)
            try:
                start=0
                while True:
                    request = bytearray(32 + 256*56)
                    struct.pack_into('=QQIII',request,0,start,2**64-1-start,1,0,256)
                    fcntl.ioctl(fd,0xC020660B,request,True)
                    count=struct.unpack_from('=I',request,20)[0]
                    if not count: break
                    for i in range(count):
                        off=32+i*56
                        logical_offset,physical,length=struct.unpack_from('=QQQ',request,off)
                        flags=struct.unpack_from('=I',request,off+40)[0]
                        if flags & 0x30e: excluded += length
                        else: ranges.setdefault(info.st_dev,[]).append((physical,physical+length))
                        start=logical_offset+length
                    if flags & 1: break
            finally: os.close(fd)
    unique=mapped=0
    for intervals in ranges.values():
        end=0
        for left,right in sorted(intervals):
            mapped += right-left
            unique += max(0,right-max(left,end));end=max(end,right)
    storage={'files':files,'logical_bytes':logical,'stat_allocated_sum_bytes':allocated,'fiemap_mapped_sum_bytes':mapped,'fiemap_unique_bytes':unique,'fiemap_shared_bytes':mapped-unique,'excluded_mapping_bytes':excluded}
    summary={'repository_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'runs':records,'medians_seconds':{name:statistics.median(r['seconds'] for r in records if r['name'].startswith(name+'-') and not r['name'].endswith('restore')) for name in ['unchanged','small','deep']},'storage':storage,'loadavg':os.getloadavg()}
    (a.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))
finally:
    for path,data in original.items(): path.write_bytes(data)
    for cwd in [root,root.with_name(root.name+'-fresh'),root.with_name(root.name+'-restart')]:
        config=cwd/'.shikka/project.json'
        if config.exists():
            buck=json.loads(config.read_text())['buck2']
            subprocess.run([buck,'--isolation-dir','shikka','kill'],cwd=cwd,capture_output=True)
    subprocess.run([a.app,'--state-dir',str(a.state),'cache','stop'],capture_output=True)
