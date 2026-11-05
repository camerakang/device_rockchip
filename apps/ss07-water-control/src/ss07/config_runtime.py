"""Service configuration dependencies and observed application acknowledgements."""
from .config import ConfigError, revision, validate

DIRECTIONS = ('front', 'back', 'left', 'right', 'down')
ORDER = ('media',) + tuple('camera.' + d for d in DIRECTIONS) + ('timecode', 'sonar', 'storage', 'maintenance', 'api', 'gateway')
UNITS = {name: 'ss07-' + name + '.service' for name in ORDER if not name.startswith('camera.')}
UNITS.update({'camera.' + d: 'ss07-camera@' + d + '.service' for d in DIRECTIONS})


def projection(config, service):
    from .media import settings
    media = settings(config)
    if service.startswith('camera.') and service[7:] in DIRECTIONS:
        return {'camera': config['cameras'][service[7:]], 'rtp_base_port': media['rtp_base_port']}
    # Production applies use the separately provisioned HTTPS/ICE settings.
    # Its origin is independent of the internal API listener port.
    if service == 'media': return {'media': media, 'sdi': config['sdi']}
    if service == 'timecode': return config['timecode']
    if service == 'sonar':
        storage = config['storage']
        return {'sonar': config['sonar'], 'storage': storage if storage.get('sonar_recording_enabled', False)
                else {'sonar_recording_enabled': False}}
    # Sonar archival policy is independent from video recording and maintenance.
    video_storage = {k: v for k, v in config['storage'].items() if k != 'sonar_recording_enabled'}
    if service == 'storage': return {'storage': video_storage, 'rtsp_port': media['rtsp_port']}
    if service == 'maintenance': return video_storage
    if service == 'api': return config
    if service == 'gateway': return config['api']
    raise ConfigError('unknown configuration consumer')


def fingerprint(config, service):
    return revision(projection(config, service))


def plan(previous, candidate):
    validate(previous); validate(candidate)
    if (previous['sonar'] != candidate['sonar'] or
            (candidate['storage'].get('sonar_recording_enabled', False) and
             previous['storage'].get('sonar_recording_enabled', False) != candidate['storage'].get('sonar_recording_enabled', False))
            or (candidate['storage'].get('sonar_recording_enabled',False)
                and previous['storage']!=candidate['storage'])):
        raise ConfigError('sonar_driver_unavailable')
    for camera in candidate['cameras'].values():
        if camera['mode'] is not None and camera['mode']['fourcc'] != 'H264':
            raise ConfigError('only_native_h264_is_implemented')
    changed = [name for name in ORDER if fingerprint(previous, name) != fingerprint(candidate, name)]
    return {'services': changed, 'units': [UNITS[name] for name in changed],
            'session_restart': 'api' in changed, 'recording_restart': 'storage' in changed,
            'capture_restart': [name[7:] for name in changed if name.startswith('camera.')],
            'hardware_verified': False}


def acknowledgement(config, service, snapshot, started_us=0):
    """An acknowledgement proves loaded settings; separately state hardware availability."""
    if snapshot.get('service_revision') != fingerprint(config, service): return False, 'revision_not_loaded'
    try:
        if int(snapshot.get('observed_monotonic_us', '-1')) < started_us: return False, 'observation_predates_start'
    except (TypeError, ValueError): return False, 'invalid_observation'
    state, data = snapshot['state'], snapshot.get('payload', {})
    if service == 'media' and data.get('native_rtsp', {}).get('enabled') is True:
        if data['native_rtsp'].get('state') != 'READY': return False, 'native_rtsp_required'
    if service.startswith('camera.'):
        item = config['cameras'][service[7:]]
        if item['binding'] is None or item['mode'] is None:
            return state == 'UNAVAILABLE' and data.get('reason') == 'camera_unconfigured', 'unconfigured'
        return (state == 'RUNNING' and data.get('capture_state') == 'STREAMING'
                and data.get('publish_state') == 'PUBLISHING'), 'capture_and_publish_required'
    if service == 'storage':
        if not config['storage'].get('recording_enabled', False):
            return state == 'UNAVAILABLE' and data.get('gate') == 'DISABLED', 'disabled'
        return state == 'RUNNING' and data.get('gate') == 'RECORDING', 'recording_volume_required'
    if service == 'timecode':
        item = config['timecode']
        if not item['binding'] or any(v is None for v in item['uart'].values()):
            return state == 'UNAVAILABLE' and data.get('reason') == 'timecode_not_configured', 'unconfigured'
        return state == 'RUNNING', 'serial_receiver_required'
    if service == 'sonar':
        from .sonar_driver import resolve
        try: resolve(config)
        except ConfigError as exc:
            return state == 'UNAVAILABLE' and data.get('reason') == str(exc), 'sonar_profile_unavailable'
        if state != 'RUNNING' or data.get('serial_connected') is not True:
            return False, 'sonar_serial_required'
        if config['storage'].get('sonar_recording_enabled', False):
            archive = data.get('archive', {})
            return archive.get('state') == 'RECORDING', 'sonar_archive_required'
        return True, 'sonar_serial_connected'
    if service == 'media' and config['sdi']['rtsp_url'] is not None:
        return (state == 'RUNNING' and data.get('streams', {}).get('sdi', {}).get('ready') is True), 'sdi_h264_required'
    return state == 'RUNNING', 'running_required'
