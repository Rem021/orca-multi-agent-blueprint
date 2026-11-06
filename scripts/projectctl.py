#!/usr/bin/env python3
"""Canonical entrypoints and guarded worktree creation; never deletes or pushes."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

VOLUME=Path(os.environ.get('AI_VOLUME_PATH','/Volumes/AI_Workspace'))
UUID=os.environ.get('AI_VOLUME_UUID','AUTO')
ROOT=VOLUME/'AI/Codex/Worktrees'
REGISTRY=Path(__file__).with_name('projects.json')

def command(args):
    return subprocess.run(args,check=True,text=True,capture_output=True,
                          env={**os.environ,'GIT_TERMINAL_PROMPT':'0'},timeout=90).stdout.strip()

def volume():
    if str(VOLUME).startswith('/Volumes/'):
        data=plistlib.loads(subprocess.check_output(['diskutil','info','-plist',str(VOLUME)]))
        if (UUID and UUID!='AUTO' and data.get('VolumeUUID')!=UUID) or data.get('MountPoint')!=str(VOLUME):
            raise ValueError('目标外置卷缺失或身份不符；已停止')

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sp=ap.add_subparsers(dest='action',required=True)
    sp.add_parser('list',help='显示主入口，不改文件')
    new=sp.add_parser('new',help='预览新工作树；--apply 才实际创建')
    new.add_argument('project');new.add_argument('topic');new.add_argument('--apply',action='store_true')
    args=ap.parse_args();volume()
    entries=json.loads(REGISTRY.read_text())['projects']
    if args.action=='list':
        for r in entries:print(f"{r['key']:18} {r['repo']:28} {r.get('path') or '仅 GitHub'}")
        return
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}',args.topic):
        raise ValueError('主题仅允许 2–64 个小写字母、数字、连字符')
    matches=[r for r in entries if r['key']==args.project]
    if len(matches)!=1:raise ValueError('未知或不唯一的项目键；先运行 list')
    r=matches[0]
    if r.get('blocked_start_reason'):raise ValueError(r['blocked_start_reason'])
    if not r.get('path'):raise ValueError('仓库尚无本机主入口；不会自动另行 clone')
    src=Path(r['path']).resolve(strict=True)
    git=['git','-C',str(src)]
    real=Path(command(git+['rev-parse','--show-toplevel'])).resolve()
    if real!=src:raise ValueError('入口不是真实 Git 根')
    remote=r['remote']; url=command(git+['remote','get-url',remote])
    match=re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$',url)
    if not match or match.group(1).lower()!=r['repo'].lower():raise ValueError('远端身份与入口登记不一致')
    branch='codex/'+args.topic
    command(git+['check-ref-format','refs/heads/'+branch])
    target=ROOT/(r['key']+'-'+args.topic)
    if ROOT.is_symlink() or ROOT.resolve()!=ROOT:raise ValueError('工作树根被重定向；先核对')
    if target.exists() or target.is_symlink():raise ValueError('目标已存在；先复用或核对，不覆盖')
    local_branch=subprocess.run(git+['show-ref','--verify','--quiet','refs/heads/'+branch]).returncode
    if local_branch==0:raise ValueError('同名任务分支已存在；先检查已有工作树')
    refs=command(git+['ls-remote','--symref',remote,'HEAD'])
    m=re.search(r'^ref: refs/heads/(.+)\tHEAD$',refs,re.M)
    if not m:raise ValueError('无法核实远端默认分支')
    base=m.group(1); command(git+['check-ref-format','refs/heads/'+base])
    remote_branch=command(git+['ls-remote','--heads',remote,'refs/heads/'+branch])
    if remote_branch:raise ValueError('远端同名分支已存在；先查阅对应 PR')
    plan={'repo':r['repo'],'source':str(src),'destination':str(target),
          'branch':branch,'base':remote+'/'+base,'apply':args.apply}
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    if not args.apply:return
    volume()
    command(git+['fetch','--no-tags',remote,f'refs/heads/{base}:refs/remotes/{remote}/{base}'])
    ROOT.mkdir(exist_ok=True)
    volume()
    command(git+['worktree','add','-b',branch,str(target),'refs/remotes/'+remote+'/'+base])
    print(command(['git','-C',str(target),'status','--short','--branch']))

if __name__=='__main__':
    try:main()
    except (ValueError,subprocess.SubprocessError,OSError) as e:
        print('停止：'+str(e),file=sys.stderr);sys.exit(1)
