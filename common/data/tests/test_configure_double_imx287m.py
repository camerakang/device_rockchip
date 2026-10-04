import contextlib
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "dual_imx287m", Path(__file__).resolve().parents[1] / "configure-double-imx287m.py")
camera = importlib.util.module_from_spec(spec)
spec.loader.exec_module(camera)


def topology(index, bus, video):
    return f"""Media device information
- entity 1: stream_cif_mipi_id0 (1 pad, 1 link)
    device node name {video}
    pad0: Sink
- entity 9: m{index:02d}_b_mvcam {bus}-003b (1 pad, 1 link)
    type V4L2 subdev subtype Sensor flags 0
    device node name /dev/v4l-subdev{bus}
    pad0: Source
"""


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sysfs = Path(self.temp.name)
        for bus in (1, 5):
            model = self.sysfs / f"{bus}-003b/veye_mvcam/camera_model"
            model.parent.mkdir(parents=True)
            model.write_text("MV-MIPI-IMX287M\n")

    def discover(self, graphs):
        with patch.object(camera.glob, "glob", return_value=list(graphs)), \
             patch.object(camera, "run", side_effect=lambda command: graphs[command[2]]):
            return camera.discover(self.sysfs)

    def test_device_numbers_and_graph_order_do_not_define_camera_index(self):
        result = self.discover({"/dev/media1": topology(1, 5, "/dev/video18"),
                                "/dev/media7": topology(0, 1, "/dev/video4")})
        self.assertEqual(result[0]["video"], "/dev/video4")
        self.assertEqual(result[1]["video"], "/dev/video18")
        self.assertEqual(result[1]["subdev"], "/dev/v4l-subdev5")

    def test_one_camera_is_insufficient_for_dual_configuration(self):
        with self.assertRaisesRegex(RuntimeError, "Both CAM0"):
            self.discover({"/dev/media1": topology(0, 1, "/dev/video4")})

    def test_different_module_is_rejected(self):
        (self.sysfs / "5-003b/veye_mvcam/camera_model").write_text("MV-MIPI-IMX296M\n")
        with self.assertRaisesRegex(RuntimeError, "unexpected camera"):
            self.discover({"/dev/media1": topology(1, 5, "/dev/video18")})

    def test_shared_capture_node_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "share one capture device"):
            self.discover({"/dev/media1": topology(0, 1, "/dev/video4"),
                           "/dev/media2": topology(1, 5, "/dev/video4")})

    def test_hardware_dry_run_does_not_execute_any_commands(self):
        cameras = self.discover({"/dev/media1": topology(0, 1, "/dev/video4"),
                                 "/dev/media2": topology(1, 5, "/dev/video18")})
        args = types.SimpleNamespace(mode="hardware", fps=30, exposure_us=1000,
                                     edge="rising", dry_run=True)
        with patch.object(camera.subprocess, "run") as execute, \
             patch.object(camera, "run") as read, contextlib.redirect_stdout(io.StringIO()):
            camera.configure(cameras, args)
        execute.assert_not_called()
        read.assert_not_called()

    def test_quantized_exposure_is_returned_for_pair_comparison(self):
        with patch.object(camera, "execute"), patch.object(camera.time, "sleep"), \
             patch.object(camera, "run", return_value="0x00 0x00 0x03 0xe7"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(camera.register_write(1, 0x0C10, 1000, False), 999)

    def test_failed_trigger_register_readback_is_rejected(self):
        with patch.object(camera, "execute"), patch.object(camera.time, "sleep"), \
             patch.object(camera, "run", return_value="0x00 0x00 0x00 0x02"):
            with self.assertRaisesRegex(RuntimeError, "requested 1, read back 2"):
                camera.register_write(5, 0x040C, 1, False)


if __name__ == "__main__":
    unittest.main()
