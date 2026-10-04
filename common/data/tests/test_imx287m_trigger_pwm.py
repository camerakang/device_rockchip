import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "trigger_pwm", Path(__file__).resolve().parents[1] / "imx287m-trigger-pwm.py")
pwm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pwm)


class TriggerTests(unittest.TestCase):
    def test_discovery_uses_controller_address_not_pwmchip_number(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, address in ((0, "febd0000"), (7, "febf0020")):
                device = root / f"{address}.pwm"
                device.mkdir()
                chip = root / f"pwmchip{index}"
                chip.mkdir()
                (chip / "device").symlink_to(device)
            self.assertEqual(pwm.find_chip(root), root / "pwmchip7")

    def test_missing_controller_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                pwm.find_chip(Path(temp))

    def test_invalid_pulse_or_frequency_is_rejected(self):
        for frequency, width in ((0, 100), (float("nan"), 100),
                                 (30, 0), (30, 40000), (30, float("inf"))):
            with self.subTest(frequency=frequency, width=width):
                with self.assertRaises(ValueError):
                    pwm.timing(frequency, width)

    def test_stop_does_not_export_an_unused_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            chip = Path(temp)
            pwm.control(chip, "stop", 33333333, 100000)
            self.assertFalse((chip / "export").exists())

    def test_fresh_channel_sets_period_before_other_pwm_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            chip = Path(temp)
            channel = chip / "pwm0"
            channel.mkdir()
            for name, value in (("period", "0"), ("duty_cycle", "0"),
                                ("enable", "0"), ("polarity", "inversed")):
                (channel / name).write_text(value)
            write = Path.write_text

            def kernel_write(path, value):
                if int((channel / "period").read_text()) == 0 and path.name != "period":
                    raise OSError(22, "Invalid argument")
                return write(path, value)

            with patch.object(Path, "write_text", kernel_write):
                pwm.control(chip, "stop", 33333333, 100000)
                pwm.control(chip, "start", 33333333, 100000)
                pwm.control(chip, "stop", 33333333, 100000)
            self.assertEqual((channel / "period").read_text(), "33333333")
            self.assertEqual((channel / "duty_cycle").read_text(), "100000")
            self.assertEqual((channel / "enable").read_text(), "0")


if __name__ == "__main__":
    unittest.main()
