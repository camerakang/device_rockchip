"""Opaque-ID archive queries and verified downloads; no paths supplied by clients."""
import os
import re
import sqlite3
from urllib.parse import parse_qs,urlsplit

from .recording_http import byte_range,error
from .snapshots import DIRECTIONS,u64
from .sonar_archive import Store,MAX_PART_EVENTS
from .volume import MountLease,VolumeError

PREFIX='/api/v1/sonars/archives'


def filters(query):
    result={k:v for k,v in query.items() if k!='cursor'}
    result['limit']=int(result.get('limit','50'))
    if not 1<=result['limit']<=200:raise ValueError('invalid_limit')
    if 'direction' in result and result['direction'] not in DIRECTIONS:raise ValueError('invalid_direction')
    if 'source_epoch' in result and not u64(result['source_epoch']):raise ValueError('invalid_epoch')
    for k in ('start_us','end_us'):
        if k in result:
            u64(result[k])
            if 'source_epoch' not in result:raise ValueError('epoch_required')
    if all(k in result for k in ('start_us','end_us')) and u64(result['start_us'])>u64(result['end_us']):raise ValueError('reversed_range')
    return result


def samples(store,archive_id,query):
    options=filters(query);after=u64(query.get('cursor','0'))
    if after>MAX_PART_EVENTS:raise ValueError('invalid_sample_cursor')
    items=[];limit=options['limit']
    def visit(position,sample):
        if position<=after or len(items)>limit:return
        if options.get('direction') is not None and sample['direction']!=options['direction']:return
        tag=sample['time_tag'];external=tag['estimated_time_us']
        if options.get('source_epoch') is not None and tag['source_epoch']!=options['source_epoch']:return
        for k,compare in (('start_us',lambda a,b:a>=b),('end_us',lambda a,b:a<=b)):
            if k in options and (external is None or not compare(u64(external),u64(options[k]))):return
        items.append(dict(event_index=str(position),**sample))
    fd,meta=store.open(archive_id,visit);os.close(fd)
    return dict(archive_id=archive_id,evidence_kind=meta['evidence_kind'],complete_file=True,contains_gap=meta['contains_gap'],
                known_lost_events=meta['known_lost_events'],items=items[:limit],
                next_cursor=items[limit-1]['event_index'] if len(items)>limit else None)


def handle(handler,head=False):
    parsed=urlsplit(handler.path)
    if parsed.path!=PREFIX and not parsed.path.startswith(PREFIX+'/'):return False
    catalog=handler.server.state.sonar_catalog
    if catalog is None:error(handler,503,'SONAR_ARCHIVE_CATALOG_UNAVAILABLE');return True
    try:
        raw=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True,max_num_fields=6) if parsed.query else {}
        if any(len(v)!=1 for v in raw.values()):raise ValueError('duplicate_query')
        query={k:v[0] for k,v in raw.items()}
        if parsed.path==PREFIX and not head:
            if set(query)-{'limit','cursor','direction','source_epoch','start_us','end_us'}:raise ValueError('unknown_query')
            result=catalog.list(cursor=query.get('cursor'),**filters(query))
            result['disk_online']=False;online=None
            try:
                with MountLease.acquire(handler.server.state.config['storage']) as volume:online=volume.volume_uuid
                result['disk_online']=True
            except (OSError,ValueError):pass
            for item in result['items']:
                item['availability']='indexed_unverified' if item['volume_uuid']==online else 'disk_offline'
            handler.reply(200,{'data':result});return True
        match=re.fullmatch(re.escape(PREFIX)+r'/([a-f0-9]{64})/(samples|content)',parsed.path)
        if match is None:error(handler,404,'NOT_FOUND');return True
        archive_id,operation=match.groups()
        if operation=='samples':
            if head or set(query)-{'limit','cursor','direction','source_epoch','start_us','end_us'}:raise ValueError('invalid_sample_query')
            filters(query)
            with MountLease.acquire(handler.server.state.config['storage']) as volume:
                handler.reply(200,{'data':samples(Store(catalog,volume),archive_id,query)})
            return True
        if set(query)-{'download'} or query.get('download','0') not in ('0','1'):raise ValueError('invalid_download_query')
        with MountLease.acquire(handler.server.state.config['storage']) as volume:
            fd,meta=Store(catalog,volume).open(archive_id)
            with os.fdopen(fd,'rb') as stream:
                size=int(meta['size_bytes']);etag='"'+meta['sha256']+'"'
                try:
                    if len(handler.headers.get_all('Range',[]))>1:raise ValueError('multiple_ranges')
                    requested=handler.headers.get('Range') if handler.headers.get('If-Range') in (None,etag) else None
                    start,end,partial=byte_range(requested,size)
                except ValueError:
                    handler.send_headers(416,'application/x-ndjson',0,{'Content-Range':'bytes */'+str(size)})
                    return True
                extra={'Accept-Ranges':'bytes','ETag':etag}
                if partial:extra['Content-Range']='bytes %d-%d/%d'%(start,end,size)
                if query.get('download')=='1':extra['Content-Disposition']='attachment; filename="ss07-sonar-'+archive_id+'.jsonl"'
                handler.send_headers(206 if partial else 200,'application/x-ndjson',end-start+1,extra)
                if not head:
                    sent=0
                    try:
                        while sent<end-start+1 and not handler.server.stopping.is_set():
                            if not handler.stream_allowed('recordings.read'):break
                            volume.require_valid()
                            count=handler.connection.sendfile(stream,offset=start+sent,count=min(1048576,end-start+1-sent))
                            if not count:break
                            sent+=count
                    except OSError:pass
                    if sent!=end-start+1:handler.close_connection=True
        return True
    except KeyError:error(handler,404,'SONAR_ARCHIVE_NOT_FOUND')
    except FileNotFoundError:error(handler,503,'SONAR_ARCHIVE_STORAGE_UNAVAILABLE')
    except VolumeError:error(handler,409,'SONAR_ARCHIVE_UNAVAILABLE')
    except (OSError,sqlite3.Error):error(handler,503,'SONAR_ARCHIVE_STORAGE_UNAVAILABLE')
    except (ValueError,TypeError):error(handler,422,'INVALID_SONAR_ARCHIVE_REQUEST')
    return True
