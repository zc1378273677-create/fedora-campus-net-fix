#!/usr/bin/env python3
"""Fedora / NetworkManager 校园网认证路由修复。只使用 Python 标准库。"""
import argparse
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

STATE = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'campus-net-fix/routes.json'


def run(args, timeout=15):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{' '.join(args[:4])}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def nmget(field, kind, ident):
    return run(['nmcli', '-g', field, kind, 'show', ident])


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return []


def save_state(entries):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
    temp.replace(STATE)


def choose_interface(explicit):
    if explicit:
        if not (Path('/sys/class/net') / explicit).exists():
            raise RuntimeError(f'网卡不存在：{explicit}')
        return explicit
    candidates = []
    for dev in Path('/sys/class/net').iterdir():
        device = dev / 'device'
        if not device.exists() or '/usb' in str(device.resolve()):
            continue
        if (dev / 'type').read_text().strip() != '1' or (dev / 'wireless').exists():
            continue
        if (dev / 'carrier').read_text().strip() == '1':
            candidates.append(dev.name)
    if len(candidates) != 1:
        raise RuntimeError('无法唯一识别已插线的 PCI 有线网卡，请用 --interface enp2s0 指定。')
    return candidates[0]


def interface_info(dev):
    data = json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', dev]))
    addresses = [a['local'] for d in data for a in d.get('addr_info', [])
                 if a.get('scope') == 'global']
    if not addresses:
        raise RuntimeError('有线网卡没有 IPv4 地址。请先重连有线网络；若仍失败，需要排查 DHCP/IP 冲突。')
    uuid = nmget('GENERAL.CON-UUID', 'device', dev)
    gateway = nmget('IP4.GATEWAY', 'device', dev).splitlines()
    if not uuid or uuid == '--' or not gateway or gateway[0] == '--':
        raise RuntimeError('没有活动的 NetworkManager 连接或 DHCP 网关。')
    gateway = str(ipaddress.IPv4Address(gateway[0]))
    mac = (Path('/sys/class/net') / dev / 'address').read_text().strip()
    return uuid, addresses[0], gateway, mac


def portal_from_response(response):
    # 仅接受服务器返回的明文 HTTP IPv4 地址，不执行页面脚本。
    matches = re.findall(r'''https?://[^\s<>"']+''', html.unescape(response))
    for value in matches:
        value = value.rstrip(');')
        try:
            parsed = urlsplit(value)
            ipaddress.IPv4Address(parsed.hostname)
            if parsed.scheme == 'http' and parsed.hostname != '1.1.1.1' and not parsed.username and not parsed.password:
                return value
        except (ValueError, TypeError):
            continue
    return None


def ensure_route(uuid, dev, host, gateway, entries):
    host = str(ipaddress.IPv4Address(host))
    dest = f'{host}/32'
    route = f'{dest} {gateway}'
    current = nmget('ipv4.routes', 'connection', uuid)
    # DHCP 换网段后，更新旧网关；只自动处理没有额外属性的单主机路由。
    old_routes = [item.strip() for item in current.split(',')
                  if re.fullmatch(re.escape(dest) + r'\s+(?:\d{1,3}\.){3}\d{1,3}', item.strip())]
    if len(old_routes) == 1 and old_routes[0] != route:
        old_route = old_routes[0]
        record = next((r for r in entries if r['uuid'] == uuid and r['route'] == old_route), None)
        if record is None:
            record = {'uuid': uuid, 'interface': dev, 'route': route, 'previous_route': old_route}
            entries.append(record)
        else:
            record['route'] = route
        save_state(entries)
        run(['nmcli', 'connection', 'modify', uuid, '+ipv4.routes', route])
        run(['nmcli', 'connection', 'modify', uuid, '-ipv4.routes', old_route])
        current = nmget('ipv4.routes', 'connection', uuid)
        print(f'已更新网关：{old_route} → {route}')
    exists = re.search(r'(?<![\d.])' + re.escape(host) + r'/32(?!\d)', current)
    if not exists:
        record = {'uuid': uuid, 'interface': dev, 'route': route}
        if record not in entries:
            entries.append(record)
            save_state(entries)  # 先记录，确保中断后仍可撤销。
        run(['nmcli', 'connection', 'modify', uuid, '+ipv4.routes', route])
        print(f'已保存认证路由：{route}')
    else:
        print(f'已有 {dest} 路由，保留现有设置。')
    run(['nmcli', 'device', 'reapply', dev])
    actual = run(['ip', '-4', 'route', 'get', host])
    print(actual)
    if not re.search(r'\bdev\s+' + re.escape(dev) + r'\b', actual):
        raise RuntimeError('认证地址仍未走有线网卡，可能有更高优先级的代理规则。请关闭代理 TUN 后重试。')


def undo():
    entries = load_state()
    if not entries:
        print('没有本脚本添加的路由需要撤销。')
        return
    for record in list(entries):
        uuid, dev, route = record['uuid'], record['interface'], record['route']
        current = nmget('ipv4.routes', 'connection', uuid)
        if route in current:
            run(['nmcli', 'connection', 'modify', uuid, '-ipv4.routes', route])
        previous = record.get('previous_route')
        if previous and previous not in nmget('ipv4.routes', 'connection', uuid):
            run(['nmcli', 'connection', 'modify', uuid, '+ipv4.routes', previous])
        # 配置已还原；即便接口未连接，也保存进度。
        entries.remove(record)
        save_state(entries)
        try:
            if nmget('GENERAL.CON-UUID', 'device', dev) == uuid:
                run(['nmcli', 'device', 'reapply', dev])
        except RuntimeError as error:
            print(f'配置已撤销，实时应用失败；下次重连生效：{error}')
        print(f'已撤销：{route}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true', help='添加认证路由、探测登录入口')
    mode.add_argument('--status', action='store_true', help='只读检查当前连接')
    mode.add_argument('--undo', action='store_true', help='撤销本脚本添加的路由')
    parser.add_argument('--interface', help='有线网卡名，默认自动识别 PCI 网卡')
    parser.add_argument('--portal-ip', default='10.7.0.103', type=ipaddress.IPv4Address,
                        help='认证服务器 IPv4，默认北理工珠海 10.7.0.103')
    parser.add_argument('--open', action='store_true', help='用默认浏览器打开探测到的登录入口')
    args = parser.parse_args()
    for cmd in ('nmcli', 'ip', 'curl'):
        if not shutil.which(cmd):
            raise RuntimeError(f'缺少命令：{cmd}')
    if args.undo:
        undo()
        return
    dev = choose_interface(args.interface)
    uuid, address, gateway, mac = interface_info(dev)
    host = str(args.portal_ip)
    print(f'网卡：{dev}，IPv4：{address}，网关：{gateway}，MAC：{mac}')
    if args.status:
        print(run(['ip', '-4', 'route', 'get', host]))
        print('此模式未修改配置。')
        return
    entries = load_state()
    ensure_route(uuid, dev, host, gateway, entries)
    portal = None
    try:
        response = run(['curl', '--noproxy', '*', '--interface', dev,
                        '--connect-timeout', '4', '--max-time', '7', '--max-filesize', '65536',
                        '-sS', '-D', '-', 'http://1.1.1.1/'])
        portal = portal_from_response(response)
    except RuntimeError as error:
        print(f'自动探测入口失败：{error}')
    if portal:
        target = urlsplit(portal).hostname
        if target != host:
            ensure_route(uuid, dev, target, gateway, entries)
        print('校园网实际返回的登录入口：')
    elif host == '10.7.0.103':
        compact = mac.replace(':', '')
        formatted_mac = '-'.join(compact[i:i+4] for i in range(0, 12, 4))
        portal = f'http://{host}:30004/byod/index.html?' + urlencode({
            'usermac': formatted_mac, 'userip': address, 'userurl': 'http://1.1.1.1/',
            'original': 'http://1.1.1.1/', 'ssid': 'E'})
        print('未探测到重定向，按本次已验证的北理工珠海入口生成备用链接：')
    else:
        print('未探测到认证入口。请关闭代理 TUN 后，通过有线网络访问普通 HTTP 网页获取入口。')
    if portal:
        print(portal)
        if args.open:
            if not shutil.which('xdg-open'):
                print('缺少 xdg-open，请复制上面的链接到浏览器。')
            else:
                subprocess.Popen(['xdg-open', portal], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('路由已处理；是否能上网仍须完成校园网认证后验证。')
    print('如果 IP 变化，请重新运行此命令生成新入口。')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        print(f'未完成：{error}', file=sys.stderr)
        print('如需撤销已添加的路由：python3 campus-net-fix.py --undo', file=sys.stderr)
        sys.exit(1)
