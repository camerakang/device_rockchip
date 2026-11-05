#!/usr/bin/env python3
"""Isolated native GStreamer test client; ephemeral credentials arrive on stdin."""
import json
import sys
import time


def main():
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gio, Gst
    Gst.init(None)
    value = json.loads(sys.stdin.buffer.read(4097))
    pipeline = Gst.parse_launch('rtspsrc name=source protocols=tcp ! rtph264depay name=depay '
                               '! video/x-h264,stream-format=byte-stream,alignment=au ! filesink name=output sync=false')
    source = pipeline.get_by_name('source')
    source.set_property('location', value['url'])
    source.set_property('user-id', value['username']); source.set_property('user-pw', value['password'])
    source.set_property('tls-database', Gio.TlsFileDatabase.new(value['certificate']))
    source.set_property('tls-validation-flags', Gio.TlsCertificateFlags.VALIDATE_ALL)
    pipeline.get_by_name('output').set_property('location', value['output'])
    frames = [0]
    def count(pad, info): frames[0] += 1; return Gst.PadProbeReturn.OK
    pipeline.get_by_name('depay').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, count)
    try:
        pipeline.set_state(Gst.State.PLAYING)
        bus, deadline = pipeline.get_bus(), time.monotonic() + 12
        # Start mid-GOP deliberately; retain enough input to include an IDR and
        # verify sustained decoding rather than only receiving codec headers.
        while frames[0] < 75 and time.monotonic() < deadline:
            message = bus.timed_pop_filtered(Gst.SECOND // 5, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    detail = (error.message + ' ' + (debug or ''))[:1000].replace(value['password'], '[redacted]')
                    raise RuntimeError('GStreamer client: ' + detail)
                break
        if frames[0] < 75: raise RuntimeError('GStreamer client receive timeout')
    finally: pipeline.set_state(Gst.State.NULL)
    print(json.dumps(dict(runtime=Gst.version_string(), received_access_units=frames[0], tls_validation='all',
                          test_ca_scope='this client only', password_in_argv_or_url=False)))


if __name__ == '__main__': main()
