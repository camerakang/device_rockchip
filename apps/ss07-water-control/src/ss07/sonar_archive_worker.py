"""Independent bounded archival queue; disk and timecode failures cannot stall polls."""
import fcntl
import os
import queue
import sqlite3
import stat
import threading
import time

from .sonar_archive import Catalog, MAX_PART_BYTES, Store, Writer, identity
from .sonar_core import event, validate_event
from .timecode_model import unavailable
from .volume import MountLease, VolumeError


def disabled_status(enabled=False, reason='archive_disabled'):
    return dict(enabled=enabled, state='BLOCKED' if enabled else 'DISABLED', reason=reason,
        queued_events=0, dropped_events='0', written_events='0', last_committed_monotonic_us=None,
        active_archive_id=None, data_disk_archive=True, volume_uuid=None, recovery_failures=0)


class AsyncArchive:
    def __init__(self, config, path, boot, instance, timecode, evidence_kind='device_observation'):
        self.config, self.catalog = dict(config), Catalog(path)
        self.boot, self.instance, self.timecode, self.evidence = boot, instance, timecode, evidence_kind
        self.queue=queue.Queue(maxsize=256);self.guard=threading.Lock();self.stopping=threading.Event()
        self.data=disabled_status(True,'archive_starting');self.data['state']='STARTING'
        self.writing_since=None
        self.thread=threading.Thread(target=self._run,daemon=True)

    def start(self):self.thread.start()

    def submit(self,sample):
        validate_event(sample)
        if any(sample[k]!=v for k,v in (('rk_boot_id',self.boot),('collector_instance_id',self.instance),('evidence_kind',self.evidence))):
            raise ValueError('archive_collector_identity_changed')
        with self.guard:
            if self.stopping.is_set():raise ValueError('sonar_archive_closed')
            try:self.queue.put_nowait(dict(sample))
            except queue.Full:self.data['dropped_events']=str(int(self.data['dropped_events'])+1)

    def status(self):
        with self.guard:
            result=dict(self.data,queued_events=self.queue.qsize())
            if self.writing_since is not None and time.monotonic()-self.writing_since>=1:
                result.update(state='ERROR',reason='archive_write_stalled')
            return result

    def close(self):
        with self.guard:self.stopping.set()
        self.thread.join(timeout=1)

    def tag(self,sample):
        when=int(sample['event_monotonic_us'])
        try:
            result=dict(sample,time_tag=self.timecode.tag(when,sample['timestamp_basis']))
            validate_event(result,tagged=True)
            return result
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            return dict(sample,time_tag=unavailable(self.boot,when,sample['timestamp_basis'],
                        'archive_timecode_unavailable',self.evidence))

    def _run(self):
        volume=writer=lock=None;next_open=0;delay=1;reported=0
        try:
            self.catalog.path.parent.mkdir(parents=True,exist_ok=True)
            lock=os.open(str(self.catalog.path)+'.owner.lock',os.O_RDONLY|os.O_CREAT|os.O_NONBLOCK|os.O_NOFOLLOW,0o640)
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('invalid_archive_owner_lock')
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.catalog.initialize()
            while not self.stopping.is_set() or not self.queue.empty():
                try:batch=[self.queue.get(timeout=.1)]
                except queue.Empty:
                    if writer is not None and writer.fd is not None and time.monotonic_ns()//1000-writer.started>=writer.segment_us:
                        try:
                            writer.finish()
                            with self.guard:self.data['active_archive_id']=None
                        except (OSError,ValueError,sqlite3.Error):
                            writer.abort();writer=None
                            if volume is not None:volume.close();volume=None
                            with self.guard:self.data.update(state='ERROR',reason='archive_finalize_failed',active_archive_id=None,volume_uuid=None)
                    continue
                while len(batch)<31:
                    try:batch.append(self.queue.get_nowait())
                    except queue.Empty:break
                with self.guard:
                    dropped=int(self.data['dropped_events']);self.writing_since=time.monotonic()
                try:
                    if volume is None:
                        if time.monotonic()<next_open:raise VolumeError('archive_reconnect_wait')
                        volume=MountLease.acquire(self.config,writable=True)
                        if volume.evidence_kind!=self.evidence:raise VolumeError('archive_volume_evidence_mismatch')
                        recovery=Store(self.catalog,volume).recover()
                        writer=Writer(self.catalog,volume,self.config.get('segment_seconds',300))
                        with self.guard:self.data['recovery_failures']=recovery['failures']
                    free,_=volume.space()
                    target=int(self.config.get('reserve_bytes','50000000000'))+MAX_PART_BYTES
                    if free<target:
                        writer.finish()
                        gate=Store(self.catalog,volume).prune(target,self.config.get('retention_days'))
                        if not gate['space_available']:raise VolumeError('archive_space_reserve')
                    # Apply expiry independently of space, once per new part.
                    elif writer.fd is None and self.config.get('retention_days') is not None:
                        Store(self.catalog,volume).prune(target,self.config['retention_days'])
                    tagged=[self.tag(sample) for sample in batch]
                    if dropped>reported:
                        gap=event(self.boot,self.instance,0,'HISTORY_GAP',time.monotonic_ns()//1000,self.evidence,lost=dropped-reported)
                        tagged.append(self.tag(gap))
                    writer.append(tagged)
                    reported=dropped;delay=1
                    with self.guard:
                        self.data.update(state='RECORDING',reason=None,volume_uuid=volume.volume_uuid,
                            written_events=str(int(self.data['written_events'])+len(batch)),
                            last_committed_monotonic_us=str(time.monotonic_ns()//1000),
                            active_archive_id=identity(volume.volume_uuid,writer.relative) if writer.fd is not None else None)
                except (OSError,ValueError,sqlite3.Error) as exc:
                    if writer is not None:writer.abort();writer=None
                    if volume is not None:volume.close();volume=None
                    if str(exc)!='archive_reconnect_wait':next_open=time.monotonic()+delay;delay=min(30,delay*2)
                    reason=str(exc) if isinstance(exc,VolumeError) else 'archive_write_failed'
                    with self.guard:
                        self.data.update(state='BLOCKED',reason=reason,volume_uuid=None,active_archive_id=None,
                            dropped_events=str(int(self.data['dropped_events'])+len(batch)))
                finally:
                    with self.guard:self.writing_since=None
            if writer is not None:
                with self.guard:dropped=int(self.data['dropped_events'])
                if dropped>reported:
                    gap=event(self.boot,self.instance,0,'HISTORY_GAP',time.monotonic_ns()//1000,self.evidence,lost=dropped-reported)
                    writer.append([self.tag(gap)])
                writer.finish()
            with self.guard:self.data.update(state='STOPPED',active_archive_id=None)
        except (OSError,ValueError,sqlite3.Error):
            with self.guard:self.data.update(state='ERROR',reason='archive_unavailable',active_archive_id=None)
        finally:
            if writer is not None:writer.abort()
            if volume is not None:volume.close()
            if lock is not None:os.close(lock)
