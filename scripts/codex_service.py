"""Install/remove the host Codex runner as a per-user macOS LaunchAgent."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.codex_bridge import directory

LABEL = 'local.a-share-analysis.codex-host'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'uninstall', 'status'])
    args = parser.parse_args()
    if sys.platform != 'darwin':raise SystemExit('macOS 专用；其他系统运行 python -m backend.codex_host')
    plist = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
    domain = f'gui/{os.getuid()}'
    if args.action == 'status':
        raise SystemExit(subprocess.run(['launchctl', 'print', f'{domain}/{LABEL}']).returncode)
    if args.action == 'uninstall':
        subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
        plist.unlink(missing_ok=True)
        print('本机 Codex 服务已停用；账号与研究数据保留。')
        return
    binary = shutil.which('codex');node = shutil.which('node')
    if not binary or not node:raise SystemExit('请先安装 Codex CLI 和 Node.js')
    runtime = directory().parent
    document = {'Label': LABEL, 'ProgramArguments': [sys.executable, '-m', 'backend.codex_host', '--codex', binary],
                'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                'EnvironmentVariables': {'HOME': str(Path.home()), 'PATH': f'{Path(node).parent}:{Path(binary).parent}:/usr/bin:/bin:/usr/sbin:/sbin', 'ASHARE_RUNTIME': str(runtime)},
                'StandardOutPath': str(runtime / 'codex-host.stdout.log'),
                'StandardErrorPath': str(runtime / 'codex-host.stderr.log')}
    if os.environ.get('CODEX_HOME'):document['EnvironmentVariables']['CODEX_HOME'] = os.environ['CODEX_HOME']
    plist.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
    plist.write_bytes(plistlib.dumps(document));plist.chmod(0o600)
    # bootout is asynchronous on macOS; wait for the old registration to disappear.
    for attempt in range(20):
        result=subprocess.run(['launchctl', 'bootstrap', domain, str(plist)],capture_output=True,text=True)
        if result.returncode==0:break
        time.sleep(.5)
    else:raise SystemExit('本机服务安装失败：'+result.stderr.strip())
    print('本机 Codex 服务已安装，登录 Mac 时自动启动；无需额外端口。')


if __name__ == '__main__':main()
