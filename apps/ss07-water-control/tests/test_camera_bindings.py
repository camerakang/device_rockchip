"""Unit tests for USB camera auto-fallback resolution and binding validation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ss07.camera_bindings import resolve_all, resolve_channel, short_label, validate_bindings_patch


def _make_config(bindings):
    mode = {'fourcc': 'H264', 'width': 1280, 'height': 720, 'fps_num': 25, 'fps_den': 1}
    return {
        'cameras': {
            ch: {'binding': bindings.get(ch), 'mode': mode}
            for ch in ('front', 'back', 'left', 'right', 'down')
        }
    }


def _dev(suffix, node):
    binding = f'/dev/v4l/by-path/platform-{suffix}-video-index0'
    return {'binding': binding, 'label': short_label(binding), 'device_node': node}


class CameraBindingsTest(unittest.TestCase):
    def test_short_label(self):
        self.assertEqual(
            short_label('/dev/v4l/by-path/platform-xhci-hcd.9.auto-usb-0:1.2:1.0-video-index0'),
            'xhci-hcd.9.auto · 0:1.2',
        )

    def test_exact_match_has_no_fallback(self):
        devices = [
            _dev('fc880000.usb-usb-0:1.3:1.0', '/dev/video4'),
            _dev('fc800000.usb-usb-0:1:1.0', '/dev/video0'),
            _dev('xhci-hcd.9.auto-usb-0:1.1:1.0', '/dev/video8'),
            _dev('xhci-hcd.9.auto-usb-0:1.4:1.0', '/dev/video6'),
            _dev('fc880000.usb-usb-0:1.1:1.0', '/dev/video2'),
        ]
        cfg = _make_config({
            'front': devices[0]['binding'],
            'back': devices[1]['binding'],
            'left': devices[2]['binding'],
            'right': devices[3]['binding'],
            'down': devices[4]['binding'],
        })
        res = resolve_all(cfg, devices=devices, by_path_dir='/nonexistent')
        for ch in ('front', 'back', 'left', 'right', 'down'):
            self.assertFalse(res[ch]['fallback'])
            self.assertEqual(res[ch]['effective_binding'], res[ch]['configured_binding'])

    def test_single_port_moved_claims_sole_free_camera(self):
        devices = [
            _dev('fc880000.usb-usb-0:1.3:1.0', '/dev/video4'),
            _dev('fc800000.usb-usb-0:1:1.0', '/dev/video0'),
            _dev('xhci-hcd.9.auto-usb-0:1.1:1.0', '/dev/video8'),
            _dev('xhci-hcd.9.auto-usb-0:1.2:1.0', '/dev/video6'),  # moved from 0:1.4
            _dev('fc880000.usb-usb-0:1.1:1.0', '/dev/video2'),
        ]
        cfg = _make_config({
            'front': devices[0]['binding'],
            'back': devices[1]['binding'],
            'left': devices[2]['binding'],
            'right': '/dev/v4l/by-path/platform-xhci-hcd.9.auto-usb-0:1.4:1.0-video-index0',
            'down': devices[4]['binding'],
        })
        effective, fallback = resolve_channel(cfg, 'right', devices=devices, by_path_dir='/nonexistent')
        self.assertTrue(fallback)
        self.assertEqual(effective, devices[3]['binding'])
        # Other 4 channels remain untouched and non-fallback
        res = resolve_all(cfg, devices=devices, by_path_dir='/nonexistent')
        for ch in ('front', 'back', 'left', 'down'):
            self.assertFalse(res[ch]['fallback'])

    def test_validate_rejects_duplicate_and_allows_swap(self):
        cfg = _make_config({
            'front': '/dev/v4l/by-path/platform-a-usb-0:1:1.0-video-index0',
            'back': '/dev/v4l/by-path/platform-b-usb-0:2:1.0-video-index0',
            'left': None,
            'right': None,
            'down': None,
        })
        # Duplicate fails
        _, errs = validate_bindings_patch(cfg['cameras'], {
            'front': '/dev/v4l/by-path/platform-b-usb-0:2:1.0-video-index0',
        })
        self.assertTrue(errs)
        # Swap succeeds
        merged, errs = validate_bindings_patch(cfg['cameras'], {
            'front': '/dev/v4l/by-path/platform-b-usb-0:2:1.0-video-index0',
            'back': '/dev/v4l/by-path/platform-a-usb-0:1:1.0-video-index0',
        })
        self.assertEqual(errs, [])
        self.assertEqual(merged['front'], '/dev/v4l/by-path/platform-b-usb-0:2:1.0-video-index0')
        self.assertEqual(merged['back'], '/dev/v4l/by-path/platform-a-usb-0:1:1.0-video-index0')


if __name__ == '__main__':
    unittest.main()
