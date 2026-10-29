"""离线回归测试：使用虚拟命令结果，不操作真实网卡。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'campus-net-fix.py'
spec = importlib.util.spec_from_file_location('campus_patch', MODULE_PATH)
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)
HOST = '10.7.0.103'
OLD = f'{HOST}/32 10.110.0.1'
NEW = f'{HOST}/32 10.111.176.1'


class PortalTests(unittest.TestCase):
    def test_script_redirect_and_html_entities(self):
        page = '<script>location.href="http://10.7.0.103:30004/byod/index.html?a=1&amp;b=2"</script>'
        self.assertEqual(fix.portal_from_response(page),
                         'http://10.7.0.103:30004/byod/index.html?a=1&b=2')

    def test_rejects_credentials_and_unrelated_non_ipv4_links(self):
        for value in ('http://u:p@10.7.0.103/', 'http://1.1.1.1/',
                      'https://example.com/', '<html>no redirect</html>'):
            with self.subTest(value=value):
                self.assertIsNone(fix.portal_from_response(value))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_patch = patch.object(fix, 'STATE', Path(self.temp.name) / 'routes.json')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.routes = []
        self.calls = []
        self.exit_device = 'enp2s0'
        self.run_patch = patch.object(fix, 'run', side_effect=self.command)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def command(self, args, timeout=15):
        self.calls.append(args)
        if args[:4] == ['nmcli', '-g', 'ipv4.routes', 'connection']:
            return ', '.join(self.routes)
        if args[:4] == ['nmcli', '-g', 'GENERAL.CON-UUID', 'device']:
            return 'uuid-test'
        if args[:3] == ['nmcli', 'connection', 'modify']:
            operation, route = args[4:6]
            if operation == '+ipv4.routes' and route not in self.routes:
                self.routes.append(route)
            if operation == '-ipv4.routes' and route in self.routes:
                self.routes.remove(route)
        if args[:4] == ['ip', '-4', 'route', 'get']:
            return f'{HOST} via 10.111.176.1 dev {self.exit_device}'
        return ''

    def apply(self, gateway, entries):
        fix.ensure_route('uuid-test', 'enp2s0', HOST, gateway, entries)

    def test_repeated_apply_and_undo_preserve_other_routes(self):
        other = '192.0.2.20/32 10.111.176.1'
        self.routes = [other]
        entries = []
        self.apply('10.111.176.1', entries)
        self.apply('10.111.176.1', entries)
        self.assertEqual(len(fix.load_state()), 1)
        self.assertEqual(sum('+ipv4.routes' in c for c in self.calls), 1)
        fix.undo()
        self.assertEqual(self.routes, [other])
        self.assertEqual(fix.load_state(), [])

    def test_existing_correct_route_is_not_claimed(self):
        self.routes = [NEW]
        self.apply('10.111.176.1', [])
        fix.undo()
        self.assertEqual(self.routes, [NEW])
        self.assertEqual(fix.load_state(), [])

    def test_existing_stale_route_migrates_and_undo_restores(self):
        self.routes = [OLD]
        self.apply('10.111.176.1', [])
        self.assertEqual(self.routes, [NEW])
        self.assertEqual(fix.load_state()[0]['previous_route'], OLD)
        fix.undo()
        self.assertEqual(self.routes, [OLD])

    def test_owned_route_survives_gateway_change_and_undo_removes(self):
        entries = []
        self.apply('10.110.0.1', entries)
        self.apply('10.111.176.1', entries)
        self.assertEqual(self.routes, [NEW])
        self.assertNotIn('previous_route', fix.load_state()[0])
        fix.undo()
        self.assertEqual(self.routes, [])

    def test_wrong_exit_is_reported_and_changes_can_be_undone(self):
        self.exit_device = 'Meta'
        with self.assertRaisesRegex(RuntimeError, '仍未走有线网卡'):
            self.apply('10.111.176.1', [])
        self.assertEqual(self.routes, [NEW])
        fix.undo()
        self.assertEqual(self.routes, [])


if __name__ == '__main__':
    unittest.main()
