"""Reviewed additive sonar transition; plan only, all writes use the install journal."""
import json

from .application import journal, read_owned
from .config import decode_json, revision, validate
from .release_contract import DEFAULT_FILES, family
from .release_restore import read_regular, safe_path

HISTORY = tuple('var/lib/ss07-apply/' + n + '.json' for n in ('transaction', 'previous', 'candidate'))


def plan(root, previous, target):
    target_family = family(target)
    source_family = family(previous) if previous is not None else None
    result = dict(name='none', updates={}, originals={}, history_archived=False)
    if source_family is None or source_family == target_family: return result
    result['name'] = 'sonar_config_v1_to_v2' if target_family == 'sonar' else 'sonar_config_v2_to_v1'
    configurations = {}
    for name in DEFAULT_FILES:
        raw = read_regular(safe_path(root, name))
        value = decode_json(raw); validate(value)
        result['originals'][name] = raw
        configurations[name] = value
        if target_family == 'legacy':
            storage = value['storage']
            if storage.get('sonar_recording_enabled', False):
                raise ValueError('sonar_archive_must_be_disabled_before_downgrade')
            if 'sonar_recording_enabled' in storage:
                del storage['sonar_recording_enabled']
                result['updates'][name] = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
    if target_family == 'legacy':
        # The old coordinator cannot interpret sonar service results or hashes
        # including the removed optional field. Archive only completed history;
        # retain its exact original bytes in this installation's file-N backups.
        # Audit DBs are never reset. On failed installation, restore these files
        # together with configuration and code before any consumer starts.
        try: history = journal(safe_path(root, 'var/lib/ss07-apply'))
        except FileNotFoundError: history = None
        if history is not None:
            if history['phase'] not in ('APPLIED', 'ROLLED_BACK', 'FAILED'):
                raise ValueError('configuration_recovery_required')
            expected = history['target_revision'] if history['phase'] == 'APPLIED' else history['previous_revision']
            active = decode_json(result['originals']['etc/ss07-active/system.json'])
            if revision(active) != expected: raise ValueError('active_configuration_drift')
        for name in HISTORY:
            try: raw = read_regular(safe_path(root, name))
            except FileNotFoundError:
                if history is not None: raise ValueError('configuration_history_incomplete')
                continue
            # Validate the exact bytes to be archived, as well as the live
            # journal's ownership rules. Do not hide damaged recovery inputs
            # merely because the old coordinator cannot read them.
            value = read_owned(safe_path(root, name))
            if json.dumps(decode_json(raw), sort_keys=True) != json.dumps(value, sort_keys=True):
                raise ValueError('migration_input_changed')
            if name == HISTORY[0]:
                if value != history: raise ValueError('migration_input_changed')
            else:
                validate(value)
                key = 'previous_revision' if name == HISTORY[1] else 'target_revision'
                if history is not None and revision(value) != history[key]:
                    raise ValueError('configuration_history_mismatch')
            result['originals'][name] = raw
            result['updates'][name] = None
            result['history_archived'] = True
    return result


def require_unchanged(root, originals):
    for name, raw in originals.items():
        try: current = read_regular(safe_path(root, name))
        except FileNotFoundError: current = None
        if current != raw: raise ValueError('migration_input_changed')
