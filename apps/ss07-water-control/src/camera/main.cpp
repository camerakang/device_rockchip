// H.264 access units stay in GStreamer. Only bounded 1 Hz counters leave this
// process. RTP is sent to a dedicated loopback MediaMTX input per channel.
#include <gst/gst.h>
#include <linux/videodev2.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
volatile sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }
uint64_t now_us() { return static_cast<uint64_t>(g_get_monotonic_time()); }

struct Options {
    std::string device;
    int width = 0, height = 0, num = 0, den = 0, port = 0;
    bool synthetic = false;
};

int number(const std::string& value, int maximum) {
    size_t pos = 0;
    int result = std::stoi(value, &pos);
    if (pos != value.size() || result < 1 || result > maximum)
        throw std::runtime_error("invalid_numeric_argument");
    return result;
}

Options arguments(int argc, char** argv) {
    std::map<std::string, std::string> args;
    for (int i = 1; i < argc; i += 2) {
        if (i + 1 >= argc || !args.emplace(argv[i], argv[i + 1]).second)
            throw std::runtime_error("invalid_arguments");
    }
    const std::vector<std::string> keys = {
        "--device", "--width", "--height", "--fps-num", "--fps-den", "--port"};
    if (args.size() != keys.size()) throw std::runtime_error("invalid_arguments");
    for (const auto& key : keys)
        if (!args.count(key)) throw std::runtime_error("missing_argument");
    Options o;
    o.device = args.at("--device");
#ifdef SS07_TEST_SOURCE
    o.synthetic = o.device == "synthetic-test";
#endif
    const bool stable = o.device.rfind("/dev/v4l/by-path/", 0) == 0 ||
                        o.device.rfind("/dev/v4l/by-id/", 0) == 0;
    const auto last_slash = o.device.find_last_of('/');
    const std::string basename = o.device.substr(last_slash + 1);
    const size_t prefix_size = o.device.rfind("/dev/v4l/by-path/", 0) == 0 ? 17 : 15;
    if (!o.synthetic && (!stable || last_slash != prefix_size - 1 ||
                         basename.empty() || basename == "." || basename == ".."))
        throw std::runtime_error("stable_device_binding_required");
    o.width = number(args.at("--width"), 16384);
    o.height = number(args.at("--height"), 16384);
    o.num = number(args.at("--fps-num"), 1000000);
    o.den = number(args.at("--fps-den"), 1000000);
    o.port = number(args.at("--port"), 65535);
    if (o.port < 1024) throw std::runtime_error("invalid_port");
    return o;
}

int xioctl(int fd, unsigned long op, void* arg) {
    int result;
    do { result = ioctl(fd, op, arg); } while (result < 0 && errno == EINTR);
    return result;
}

// Hold the validated device open. GStreamer opens /proc/self/fd/<fd>, avoiding
// a second resolution of a udev symlink if the camera is unplugged meanwhile.
int open_device(const Options& o) {
    int fd = open(o.device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC | O_NOCTTY);
    if (fd < 0) throw std::runtime_error("device_open_failed");
    try {
        struct stat info{};
        if (fstat(fd, &info) < 0 || !S_ISCHR(info.st_mode))
            throw std::runtime_error("not_a_video_device");
        if (flock(fd, LOCK_EX | LOCK_NB) < 0)
            throw std::runtime_error("device_in_use");
        v4l2_capability caps{};
        if (xioctl(fd, VIDIOC_QUERYCAP, &caps) < 0)
            throw std::runtime_error("v4l2_query_failed");
        auto bits = caps.capabilities & V4L2_CAP_DEVICE_CAPS ? caps.device_caps : caps.capabilities;
        if (!(bits & V4L2_CAP_VIDEO_CAPTURE) || !(bits & V4L2_CAP_STREAMING))
            throw std::runtime_error("capture_streaming_required");
        bool format_ok = false, size_ok = false, interval_ok = false;
        v4l2_fmtdesc format{};
        format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        for (; format.index < 256 && xioctl(fd, VIDIOC_ENUM_FMT, &format) == 0; ++format.index)
            if (format.pixelformat == V4L2_PIX_FMT_H264) format_ok = true;
        v4l2_frmsizeenum size{};
        size.pixel_format = V4L2_PIX_FMT_H264;
        for (; size.index < 1024 && xioctl(fd, VIDIOC_ENUM_FRAMESIZES, &size) == 0; ++size.index)
            if (size.type == V4L2_FRMSIZE_TYPE_DISCRETE &&
                size.discrete.width == static_cast<unsigned>(o.width) &&
                size.discrete.height == static_cast<unsigned>(o.height)) size_ok = true;
        v4l2_frmivalenum interval{};
        interval.pixel_format = V4L2_PIX_FMT_H264;
        interval.width = o.width; interval.height = o.height;
        for (; interval.index < 1024 && xioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, &interval) == 0; ++interval.index)
            if (interval.type == V4L2_FRMIVAL_TYPE_DISCRETE &&
                uint64_t(interval.discrete.numerator) * o.num ==
                uint64_t(interval.discrete.denominator) * o.den) interval_ok = true;
        if (!format_ok || !size_ok || !interval_ok)
            throw std::runtime_error("requested_h264_mode_not_enumerated");
        return fd;
    } catch (...) { close(fd); throw; }
}

struct Counters {
    std::atomic<uint64_t> frames{0}, bytes{0}, last{0};
    std::atomic<bool> mismatch{false}, overrun{false};
    Options options;
};

GstPadProbeReturn observe(GstPad* pad, GstPadProbeInfo* info, gpointer data) {
    auto& c = *static_cast<Counters*>(data);
    if (!(GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_BUFFER)) return GST_PAD_PROBE_OK;
    GstBuffer* buffer = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buffer) return GST_PAD_PROBE_OK;
    if (c.frames.load() == 0) {
        GstCaps* caps = gst_pad_get_current_caps(pad);
        int width = 0, height = 0, num = 0, den = 0;
        bool ok = false;
        if (caps && !gst_caps_is_empty(caps)) {
            const auto* s = gst_caps_get_structure(caps, 0);
            ok = gst_structure_get_int(s, "width", &width) &&
                 gst_structure_get_int(s, "height", &height) &&
                 gst_structure_get_fraction(s, "framerate", &num, &den) &&
                 width == c.options.width && height == c.options.height &&
                 int64_t(num) * c.options.den == int64_t(den) * c.options.num;
        }
        if (caps) gst_caps_unref(caps);
        if (!ok) { c.mismatch = true; return GST_PAD_PROBE_DROP; }
    }
    c.bytes += gst_buffer_get_size(buffer);
    c.frames += 1;
    c.last = now_us();
    return GST_PAD_PROBE_OK;
}

GstElement* element(const char* factory, const char* name, GstElement* pipeline) {
    auto* e = gst_element_factory_make(factory, name);
    if (!e) throw std::runtime_error(std::string("missing_plugin_") + factory);
    if (!gst_bin_add(GST_BIN(pipeline), e)) {
        gst_object_unref(e);
        throw std::runtime_error("pipeline_add_failed");
    }
    return e;
}

struct Report { uint64_t frames, bytes, time; };
Report emit(const Counters& c, uint64_t previous_frames, uint64_t previous_bytes,
            uint64_t previous_time) {
    const uint64_t now = now_us(), frames = c.frames, bytes = c.bytes, last = c.last;
    const double elapsed = static_cast<double>(now - previous_time) / 1000000.0;
    std::string line = "{\"evidence_kind\":\"" +
        std::string(c.options.synthetic ? "synthetic_test" : "device_observation") +
        "\",\"observed_monotonic_us\":\"" + std::to_string(now) +
        "\",\"last_frame_monotonic_us\":" + (last ? "\"" + std::to_string(last) + "\"" : "null") +
        ",\"frames\":\"" + std::to_string(frames) + "\",\"bytes\":\"" + std::to_string(bytes) +
        "\",\"fps_observed\":" + std::to_string(elapsed > 0 ? (frames - previous_frames) / elapsed : 0) +
        ",\"bitrate_observed\":" + std::to_string(elapsed > 0 ? (bytes - previous_bytes) * 8 / elapsed : 0) + "}\n";
    // Smaller than PIPE_BUF: a stalled supervisor never blocks a streaming task.
    const auto result = write(STDOUT_FILENO, line.data(), line.size());
    if (result < 0 && errno != EAGAIN && errno != EINTR) stopping = 1;
    return {frames, bytes, now};
}

int run(const Options& o) {
    const int fd = o.synthetic ? -1 : open_device(o);
    GstElement* pipeline = gst_pipeline_new("ss07-h264");
    GstBus* bus = nullptr;
    Counters counts;
    counts.options = o;
    int result = 0;
    try {
        auto* input_caps = element("capsfilter", "mode", pipeline);
        GstElement* source;
#ifdef SS07_TEST_SOURCE
        if (o.synthetic) {
            source = element("videotestsrc", "test_only", pipeline);
            g_object_set(source, "is-live", TRUE, "pattern", 18, nullptr);
            auto* encoder = element("x264enc", "test_encoder", pipeline);
            g_object_set(encoder, "tune", 4, "speed-preset", 1, "bitrate", 1000,
                         "key-int-max", 25, "bframes", 0, "byte-stream", TRUE, nullptr);
            auto* raw = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "I420",
                "width", G_TYPE_INT, o.width, "height", G_TYPE_INT, o.height,
                "framerate", GST_TYPE_FRACTION, o.num, o.den, nullptr);
            if (!gst_element_link_filtered(source, encoder, raw) || !gst_element_link(encoder, input_caps)) {
                gst_caps_unref(raw); throw std::runtime_error("test_source_link_failed");
            }
            gst_caps_unref(raw);
        } else
#endif
        {
            source = element("v4l2src", "camera", pipeline);
            std::string opened = "/proc/self/fd/" + std::to_string(fd);
            g_object_set(source, "device", opened.c_str(), nullptr);
            if (!gst_element_link(source, input_caps)) throw std::runtime_error("source_link_failed");
        }
        auto* caps = gst_caps_new_simple("video/x-h264", "width", G_TYPE_INT, o.width,
            "height", G_TYPE_INT, o.height, "framerate", GST_TYPE_FRACTION, o.num, o.den, nullptr);
        g_object_set(input_caps, "caps", caps, nullptr);
        gst_caps_unref(caps);
        auto* parser = element("h264parse", "parse", pipeline);
        g_object_set(parser, "config-interval", -1, nullptr);
        auto* aligned = element("capsfilter", "access_units", pipeline);
        caps = gst_caps_new_simple("video/x-h264", "stream-format", G_TYPE_STRING, "byte-stream",
                                   "alignment", G_TYPE_STRING, "au", nullptr);
        g_object_set(aligned, "caps", caps, nullptr); gst_caps_unref(caps);
        auto* queue = element("queue", "bounded_queue", pipeline);
        g_object_set(queue, "max-size-buffers", 60U, "max-size-bytes", 8U * 1024U * 1024U,
                     "max-size-time", guint64(GST_SECOND), "leaky", 0, nullptr);
        g_signal_connect(queue, "overrun", G_CALLBACK(+[](GstElement*, gpointer p) {
            static_cast<Counters*>(p)->overrun = true;
        }), &counts);
        auto* pay = element("rtph264pay", "rtp", pipeline);
        // h264parse inserts SPS/PPS once per IDR access unit. Older RTP plugins
        // repeat them before each IDR slice when -1 is set here as well, which
        // can split a multi-slice picture at the receiver. Keep one insertion point.
        g_object_set(pay, "pt", 96U, "config-interval", 0, "mtu", 1200U, nullptr);
        auto* sink = element("udpsink", "local_media", pipeline);
        g_object_set(sink, "host", "127.0.0.1", "port", o.port, "sync", FALSE, "async", FALSE, nullptr);
        if (!gst_element_link_many(input_caps, parser, aligned, queue, pay, sink, nullptr))
            throw std::runtime_error("h264_pipeline_link_failed");
        auto* pad = gst_element_get_static_pad(aligned, "src");
        gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, observe, &counts, nullptr);
        gst_object_unref(pad);
        bus = gst_element_get_bus(pipeline);
        if (gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE)
            throw std::runtime_error("pipeline_start_failed");
        uint64_t previous = now_us(), started = previous, frames = 0, bytes = 0;
        while (!stopping) {
            GstMessage* msg = gst_bus_timed_pop_filtered(bus, 100 * GST_MSECOND,
                static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS));
            if (msg) {
                // GStreamer debug strings can contain private paths. Expose a code only.
                const bool eos = GST_MESSAGE_TYPE(msg) == GST_MESSAGE_EOS;
                gst_message_unref(msg);
                throw std::runtime_error(eos ? "unexpected_eos" : "pipeline_error");
            }
            if (counts.mismatch) throw std::runtime_error("negotiated_mode_mismatch");
            if (counts.overrun) throw std::runtime_error("queue_overrun_restart_required");
            const uint64_t now = now_us(), last = counts.last;
            if (now - (last ? last : started) >= 3000000)
                throw std::runtime_error("frame_timeout");
            if (now - previous >= 1000000) {
                const auto report = emit(counts, frames, bytes, previous);
                frames = report.frames; bytes = report.bytes; previous = report.time;
            }
        }
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; result = 3; }
    gst_element_set_state(pipeline, GST_STATE_NULL);
    if (bus) gst_object_unref(bus);
    gst_object_unref(pipeline);
    if (fd >= 0) close(fd);
    return result;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--version") {
        std::cout << "ss07-camera 0.3.1 (GStreamer " << GST_VERSION_MAJOR << '.'
                  << GST_VERSION_MINOR << '.' << GST_VERSION_MICRO << ")\n";
        return 0;
    }
    try {
        const auto options = arguments(argc, argv);
        gst_init(nullptr, nullptr);
        signal(SIGINT, stop); signal(SIGTERM, stop); signal(SIGPIPE, SIG_IGN);
        const int flags = fcntl(STDOUT_FILENO, F_GETFL);
        if (flags >= 0) fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK);
        return run(options);
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 2; }
}
