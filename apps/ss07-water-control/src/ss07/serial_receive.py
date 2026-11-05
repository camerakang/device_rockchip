"""Linux USB serial reception only. There is no serial write or transmit method."""
import fcntl
import os
import re
import stat
import termios
from pathlib import Path

from .config import ConfigError
from .timecode_wire import MAX_READ


def parameters(uart):
    if set(uart) != {'baud', 'data_bits', 'parity', 'stop_bits'} or any(value is None for value in uart.values()):
        raise ConfigError('timecode_uart_unconfigured')
    if uart['data_bits'] != 8 or uart['parity'] not in ('none', 'even', 'odd') or uart['stop_bits'] not in (1, 2):
        raise ConfigError('timecode_requires_eight_data_bits_and_explicit_framing')
    baud = getattr(termios, 'B' + str(uart['baud']), None)
    if baud is None or type(uart['baud']) is not int or uart['baud'] <= 0:
        raise ConfigError('timecode_baud_not_supported_by_termios')
    return baud


def usb_parent(path):
    for candidate in (path,) + tuple(path.parents):
        if (candidate / 'idVendor').is_file() and (candidate / 'idProduct').is_file():
            return candidate
    raise ConfigError('timecode_adapter_is_not_usb')


def identify(config):
    binding = config['timecode']['binding']
    if not isinstance(binding, str) or re.fullmatch(r'/dev/serial/by-(id|path)/[^/\s]+', binding) is None:
        raise ConfigError('timecode_binding_unconfigured')
    parameters(config['timecode']['uart'])
    target = Path(binding).resolve(strict=True)
    if re.fullmatch(r'/dev/tty(?:USB|ACM)[0-9]+', str(target)) is None:
        raise ConfigError('timecode_requires_usb_serial_device')
    info = target.stat()
    if not stat.S_ISCHR(info.st_mode):
        raise ConfigError('timecode_binding_not_character_device')
    other = config['sonar']['binding']
    if other and Path(other).exists() and Path(other).stat().st_rdev == info.st_rdev:
        raise ConfigError('timecode_sonar_adapter_conflict')
    physical = usb_parent((Path('/sys/class/tty') / target.name / 'device').resolve(strict=True))
    for interface in physical.glob('*:*/bInterfaceClass'):
        if interface.read_text().strip().lower() == '0e':
            raise ConfigError('camera_serial_interface_refused')
    if other and Path(other).exists():
        other_target = Path(other).resolve(strict=True)
        other_physical = usb_parent((Path('/sys/class/tty') / other_target.name / 'device').resolve(strict=True))
        if other_physical == physical:
            raise ConfigError('timecode_sonar_adapter_conflict')
    for video in list(Path('/sys/class/video4linux').glob('video[0-9]*'))[:128]:
        try:
            if usb_parent((video / 'device').resolve(strict=True)) == physical:
                raise ConfigError('camera_serial_interface_refused')
        except FileNotFoundError:
            continue
        except ConfigError as exc:
            if str(exc) != 'timecode_adapter_is_not_usb':
                raise
    if '/by-id/' in binding:
        try:
            serial = (physical / 'serial').read_text().strip()
        except OSError as exc:
            raise ConfigError('timecode_serial_identity_missing_use_by_path') from exc
        if not serial or set(serial) == {'0'}:
            raise ConfigError('timecode_serial_identity_ambiguous_use_by_path')
        matches = 0
        for candidate in list(Path('/sys/bus/usb/devices').iterdir())[:512]:
            try:
                if (candidate / 'serial').read_text().strip() == serial:
                    matches += 1
            except OSError:
                continue
        if matches != 1:
            raise ConfigError('timecode_serial_identity_ambiguous_use_by_path')
    return target, info, str(physical)


class ReceivePort:
    """Own an already opened O_RDONLY tty. Tests use a PTY, not a production flag."""
    def __init__(self, fd, uart):
        self.fd = fd
        self.exclusive = False
        try:
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY or not os.isatty(fd):
                raise ConfigError('read_only_tty_required')
            speed = parameters(uart)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(fd, termios.TIOCEXCL)
            self.exclusive = True
            attributes = termios.tcgetattr(fd)
            attributes[0] = termios.IGNBRK | termios.IGNPAR
            attributes[1] = attributes[3] = 0
            attributes[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
            if uart['parity'] != 'none':
                attributes[0] |= termios.INPCK
                attributes[2] |= termios.PARENB
                if uart['parity'] == 'odd': attributes[2] |= termios.PARODD
            if uart['stop_bits'] == 2: attributes[2] |= termios.CSTOPB
            attributes[4] = attributes[5] = speed
            attributes[6][termios.VMIN], attributes[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
            applied = termios.tcgetattr(fd)
            mask = (termios.CSIZE | termios.CLOCAL | termios.CREAD | termios.PARENB
                    | termios.PARODD | termios.CSTOPB | termios.HUPCL | getattr(termios, 'CRTSCTS', 0))
            if (any(applied[i] != attributes[i] for i in (0, 1, 3, 4, 5))
                    or applied[2] & mask != attributes[2] & mask):
                raise ConfigError('timecode_uart_readback_mismatch')
            # Drop bytes from before this receive lifetime; never flush/output or
            # request a break, RTS/DE transition, modem control or XON/XOFF.
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            self.close()
            raise

    def read(self):
        return os.read(self.fd, MAX_READ)

    def close(self):
        if self.fd is not None:
            try:
                if self.exclusive:
                    try: fcntl.ioctl(self.fd, termios.TIOCNXCL)
                    except OSError: pass
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def open_port(config):
    target, identity, physical = identify(config)
    fd = os.open(str(target), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        current = os.fstat(fd)
        after, after_identity, after_physical = identify(config)
        if (after != target or after_physical != physical
                or (current.st_rdev, current.st_ino) != (identity.st_rdev, identity.st_ino)
                or (current.st_rdev, current.st_ino) != (after_identity.st_rdev, after_identity.st_ino)):
            raise ConfigError('timecode_binding_changed_during_open')
    except Exception:
        os.close(fd)
        raise
    return ReceivePort(fd, config['timecode']['uart'])
