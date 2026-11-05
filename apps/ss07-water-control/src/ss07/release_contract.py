"""Persistent data and service contract used before switching an application release."""
# config is a deployment compatibility revision, separate from the JSON
# envelope's schema_version. Pre-sonar applications reject the new optional
# archive flag, so switching to/from their data contract needs a migration.
DATA = dict(config=2, accounts=1, catalog=2, recording_metadata=1, timecode_journal=1,
            sonar_journal=1, sonar_archive_catalog=1, sonar_archive_metadata=1,
            audit=1, application_journal=1, worker_snapshot=1)
LEGACY_DATA = dict(config=1, accounts=1, catalog=2, recording_metadata=1, timecode_journal=1,
                   audit=1, application_journal=1, worker_snapshot=1)
from .release_restore import UNIT_NAMES as UNITS, RUN_UNITS, LEGACY_UNIT_NAMES
SYSTEM_FILES = tuple('usr/lib/systemd/system/' + n for n in UNITS) + (
    'usr/lib/sysusers.d/ss07.conf', 'usr/lib/tmpfiles.d/ss07.conf')
DEFAULT_FILES = ('etc/ss07/system.json', 'etc/ss07-active/system.json')
BOOTSTRAP_FILES = ('opt/ss07/recovery/restore.py', 'usr/lib/systemd/system/ss07-release-recover.service') + tuple(
    'etc/systemd/system/' + name + '.d/00-ss07-release.conf' for name in RUN_UNITS if name != 'ss07.target')


def family(data):
    if type(data) is not dict or any(type(v) is not int for v in data.values()):
        raise ValueError('unsupported_data_contract')
    if data == DATA: return 'sonar'
    if data == LEGACY_DATA: return 'legacy'
    raise ValueError('unsupported_data_contract')


def runtime_units(data):
    return tuple(n for n in RUN_UNITS if family(data) == 'sonar' or n != 'ss07-sonar.service')


def system_files(data):
    units = UNITS if family(data) == 'sonar' else LEGACY_UNIT_NAMES
    return tuple('usr/lib/systemd/system/' + n for n in units) + (
        'usr/lib/sysusers.d/ss07.conf', 'usr/lib/tmpfiles.d/ss07.conf')


def bootstrap_files(data):
    return BOOTSTRAP_FILES[:2] + tuple('etc/systemd/system/' + n + '.d/00-ss07-release.conf'
        for n in runtime_units(data) if n != 'ss07.target')


def contract(legacy=False):
    return dict(schema_version=1, data=dict(LEGACY_DATA if legacy else DATA),
                units=list(LEGACY_UNIT_NAMES if legacy else UNITS),
                upgrade_policy='same_data_contract_only' if legacy else 'reviewed_sonar_config_transition_v1',
                config_defaults='first_install_only')
