"""验证类型登记规则和身份字段编码；全部使用虚拟夹具，不证明真实设备能力。"""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from .device_types import DeviceType, is_registered_device_type, is_virtual_device_type, is_production_device_type
from .profiles import lens
from .reference import DeviceCore, VirtualDriver
from .wire import decode, Command
from tools.generate_device_types import load_registry, render

ROOT = Path(__file__).resolve().parents[1]


class DeviceTypeTests(unittest.TestCase):
    def test_generated_sources_and_docs_are_current(self):
        # 常量、说明和向量都由同一份登记表生成，避免各语言手工维护后发生漂移。
        data = load_registry()
        self.assertEqual(len(data['types']), len(DeviceType))
        for name, expected in render(data).items():
            self.assertEqual((ROOT / name).read_bytes(), expected, name)

    def test_existing_and_production_assignments_are_stable(self):
        # 明确锁住已分配值；不能靠重新生成向量掩盖旧ID被改名或复用。
        expected = {'FAN': 0x9001, 'FOCUS_MOTOR': 0x9002, 'TREADMILL': 0x9003,
                    'STAIR_CLIMBER': 0x9004, 'MECHANICAL_HORSE': 0x9005, 'RAIL_CAR': 0x9006, 'GIMBAL': 0x9007,
                    'CUT_POINT': 0x9008, 'VIDEO_SONAR': 0x9009,
                    'VIRTUAL_LENS': 0x8001, 'VIRTUAL_GIMBAL': 0x8002, 'VIRTUAL_FANS': 0x8003}
        self.assertEqual({t.name: t.value for t in DeviceType}, expected)
        for t in DeviceType:
            self.assertTrue(is_registered_device_type(t))
            self.assertNotEqual(is_virtual_device_type(t), is_production_device_type(t))

    def test_unregistered_profile_is_rejected(self):
        # 同时覆盖保留值、未登记空位和隐式转换类型；失败前不应触发任何驱动副作用。
        for value in [0, 0xffff, 0x9000, 0x900a, 0x8fff, 0xf001, -1, 65536, True, '0x9001', 36865.0]:
            self.assertFalse(is_registered_device_type(value))
            p = lens(); p.device_type = value
            driver = VirtualDriver()
            with self.assertRaisesRegex(AssertionError, 'unregistered device type'):
                DeviceCore(p, 2, 10, driver)
            self.assertEqual(driver.apply_calls, 0)
            self.assertEqual(driver.safe_calls, 0)

    def test_registry_duplicate_reserved_and_bad_symbols_rejected(self):
        # 错误登记表写入临时目录，验证生成入口拒绝错误，不修改公共登记表。
        original = load_registry()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry.json'
            for bad in ['duplicate_id', 'duplicate_symbol', 'reserved', 'bad_symbol', 'wrong_scope']:
                data = json.loads(json.dumps(original)); entry = data['types'][1]
                if bad == 'duplicate_id': entry['id'] = data['types'][0]['id']
                elif bad == 'duplicate_symbol': entry['symbol'] = data['types'][0]['symbol']
                elif bad == 'reserved': entry['id'] = '0xFFFF'
                elif bad == 'bad_symbol': entry['symbol'] = 'bad-name'
                else: entry['scope'] = 'production'
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError, msg=bad): load_registry(path)

    def test_all_type_golden_replies(self):
        data = json.loads((ROOT / 'mobocom_realtime/device_type_vectors.json').read_text(encoding='utf-8'))
        self.assertTrue(data['virtual_fixture'])
        self.assertEqual(len(data['cases']), 3 * len(DeviceType))
        for case in data['cases']:
            p = lens(); p.device_type = DeviceType[case['symbol']]
            # 仅替换夹具的类别ID检查身份编码，不声称实现该真实类别的资源或安全动作。
            node = DeviceCore(p, 2, 10, VirtualDriver())
            result = node.transact(bytes.fromhex(case['request']), 0)
            self.assertIsNotNone(result)
            self.assertEqual(result.hex(), case['reply'])
            reply = decode(result)
            # CAPS的7字节页头后为节点记录type；INFO的type在UID之后；DISCOVER的UID在帧头，type在开头。
            offset = {Command.CAPS: 7, Command.INFO: 8, Command.DISCOVER: 0}[reply.command]
            self.assertEqual(struct.unpack_from('<H', reply.payload, offset)[0], case['type'])
