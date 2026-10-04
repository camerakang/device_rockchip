import importlib.util
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
