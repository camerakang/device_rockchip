#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

/* stdout: 64-byte little-endian header followed by width*height gray bytes.
 * Python struct: <4sIIIQQQddII. Capture never waits for the preview consumer.
 */
struct metadata {
    uint32_t sequence;
    uint64_t frames, dropped, timestamp_ns;
    double fps;
};
struct preview {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    uint8_t *latest;
    size_t length;
    unsigned width, height, stride, bits;
    bool fresh;
    struct metadata meta;
};
struct buffer { void *address; size_t length; };
static _Atomic int quitting;

static void on_signal(int sig) { (void)sig; quitting = 1; }
static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
}
static int xioctl(int fd, unsigned long request, void *arg) {
    int ret;
    do { ret = ioctl(fd, request, arg); } while (ret < 0 && errno == EINTR && !quitting);
    return ret;
}
static void put32(uint8_t *p, uint32_t n) {
    for (int i = 0; i < 4; i++) p[i] = (uint8_t)(n >> (i * 8));
}
static void put64(uint8_t *p, uint64_t n) {
    for (int i = 0; i < 8; i++) p[i] = (uint8_t)(n >> (i * 8));
}
static void put_double(uint8_t *p, double value) {
    uint64_t bits;
    memcpy(&bits, &value, sizeof(bits));
    put64(p, bits);
}
static int write_all(const void *data, size_t count) {
    const uint8_t *p = data;
    while (count && !quitting) {
        ssize_t n = write(STDOUT_FILENO, p, count);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n; count -= (size_t)n;
    }
    return count ? -1 : 0;
}

static int gray_from_raw(uint8_t *gray, const uint8_t *raw, size_t length,
                         unsigned width, unsigned height, unsigned stride, unsigned bits) {
    if (!width || !height || (bits != 8 && bits != 10 && bits != 12)) return -1;
    size_t active = ((size_t)width * bits + 7) / 8;
    if (stride < active || length < (size_t)stride * height) return -1;
    bool unpacked = bits != 8 && stride >= width * 2;
    for (unsigned y = 0; y < height; y++) {
        const uint8_t *row = raw + (size_t)y * stride;
        for (unsigned x = 0; x < width; x++) {
            unsigned value;
            if (bits == 8) value = row[x];
            else if (unpacked) value = row[x * 2] | ((unsigned)row[x * 2 + 1] << 8);
            else {
                unsigned bit = x * bits, byte = bit / 8, shift = bit % 8;
                value = row[byte];
                if (byte + 1 < active) value |= (unsigned)row[byte + 1] << 8;
                if (byte + 2 < active) value |= (unsigned)row[byte + 2] << 16;
                value >>= shift;
            }
            gray[(size_t)y * width + x] = (uint8_t)((value & ((1U << bits) - 1)) >> (bits - 8));
        }
    }
    return 0;
}

static void *send_previews(void *arg) {
    struct preview *p = arg;
    size_t size = (size_t)p->stride * p->height;
    uint8_t *snapshot = malloc(size), *gray = malloc((size_t)p->width * p->height);
    if (!snapshot || !gray) { quitting = 1; free(snapshot); free(gray); return NULL; }
    while (!quitting) {
        pthread_mutex_lock(&p->mutex);
        while (!p->fresh && !quitting) {
            struct timespec deadline;
            clock_gettime(CLOCK_REALTIME, &deadline);
            deadline.tv_sec++;
            pthread_cond_timedwait(&p->condition, &p->mutex, &deadline);
        }
        if (quitting) { pthread_mutex_unlock(&p->mutex); break; }
        memcpy(snapshot, p->latest, p->length);
        size_t length = p->length;
        struct metadata meta = p->meta;
        p->fresh = false;
        pthread_mutex_unlock(&p->mutex);
        if (gray_from_raw(gray, snapshot, length, p->width, p->height, p->stride, p->bits)) {
            fprintf(stderr, "ERROR short or invalid raw frame\n"); quitting = 1; break;
        }
        uint8_t header[64] = {0};
        memcpy(header, "FRM1", 4);
        put32(header + 4, p->width); put32(header + 8, p->height);
        put32(header + 12, meta.sequence); put64(header + 16, meta.frames);
        put64(header + 24, meta.dropped); put64(header + 32, meta.timestamp_ns);
        put_double(header + 40, meta.fps);
        double mean = 0;
        for (size_t i = 0; i < (size_t)p->width * p->height; i++) mean += gray[i];
        mean /= (double)p->width * p->height;
        put_double(header + 48, mean);
        put32(header + 56, p->width * p->height); put32(header + 60, p->bits);
        if (write_all(header, sizeof(header)) || write_all(gray, (size_t)p->width * p->height)) {
            quitting = 1; break;
        }
    }
    free(snapshot); free(gray);
    return NULL;
}

static int self_test(void) {
    /* Independent golden compact RAW12 samples, including row padding. */
    const uint8_t raw[] = {0x00, 0xf0, 0xff, 0xbc, 0x3a, 0x12, 0xee,
                          0xbc, 0x3a, 0x12, 0x00, 0xf0, 0xff, 0xee};
    const uint8_t expected[] = {0, 255, 171, 18, 171, 18, 0, 255};
    uint8_t result[8];
    if (gray_from_raw(result, raw, sizeof(raw), 4, 2, 7, 12) ||
        memcmp(result, expected, sizeof(expected))) return 1;
    if (!gray_from_raw(result, raw, 6, 4, 2, 7, 12)) return 1;
    const uint8_t words[] = {0x00, 0x00, 0xff, 0x0f, 0xbc, 0x0a, 0x23, 0x01};
    if (gray_from_raw(result, words, sizeof(words), 4, 1, 8, 12) ||
        memcmp(result, expected, 4)) return 1;
    fprintf(stderr, "Compact RAW12, padded rows, unpacked RAW12, truncated frame: OK\n");
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
    if (argc != 3) { fprintf(stderr, "Usage: %s /dev/videoN preview-fps\n", argv[0]); return 2; }
    double preview_fps = strtod(argv[2], NULL);
    if (!(preview_fps >= 1 && preview_fps <= 30)) return 2;
    struct sigaction action = {.sa_handler = on_signal};
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL); sigaction(SIGTERM, &action, NULL);
    signal(SIGPIPE, SIG_IGN);
    int fd = open(argv[1], O_RDWR | O_NONBLOCK | O_CLOEXEC), ret = 1;
    if (fd < 0) { perror("open video"); return 1; }
    struct preview p = {.mutex = PTHREAD_MUTEX_INITIALIZER, .condition = PTHREAD_COND_INITIALIZER};
    struct buffer buffers[8] = {{0}};
    unsigned mapped = 0;
    bool streaming = false, thread_started = false;
    pthread_t writer;
    struct v4l2_format format = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
    if (xioctl(fd, VIDIOC_G_FMT, &format)) { perror("VIDIOC_G_FMT"); goto done; }
    struct v4l2_pix_format_mplane *f = &format.fmt.pix_mp;
    if (f->pixelformat == V4L2_PIX_FMT_GREY) p.bits = 8;
    else if (f->pixelformat == V4L2_PIX_FMT_Y10) p.bits = 10;
    else if (f->pixelformat == V4L2_PIX_FMT_Y12) p.bits = 12;
    else { fprintf(stderr, "ERROR unsupported monochrome format\n"); goto done; }
    p.width = f->width; p.height = f->height; p.stride = f->plane_fmt[0].bytesperline;
    if (f->num_planes != 1 || p.width != 720 || p.height != 544 || p.stride > 4096) {
        fprintf(stderr, "ERROR expected 720x544 single-plane monochrome\n"); goto done;
    }
    p.latest = malloc((size_t)p.stride * p.height);
    if (!p.latest) goto done;
    struct v4l2_requestbuffers request = {.count = 8, .type = format.type, .memory = V4L2_MEMORY_MMAP};
    if (xioctl(fd, VIDIOC_REQBUFS, &request)) { perror("VIDIOC_REQBUFS"); goto done; }
    if (!request.count || request.count > 8) goto done;
    for (unsigned i = 0; i < request.count; i++) {
        struct v4l2_plane plane = {0};
        struct v4l2_buffer b = {.type = format.type, .memory = V4L2_MEMORY_MMAP,
                                .index = i, .length = 1, .m.planes = &plane};
        if (xioctl(fd, VIDIOC_QUERYBUF, &b)) { perror("VIDIOC_QUERYBUF"); goto done; }
        buffers[i].length = plane.length;
        buffers[i].address = mmap(NULL, plane.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, plane.m.mem_offset);
        if (buffers[i].address == MAP_FAILED) { buffers[i].address = NULL; perror("mmap"); goto done; }
        mapped++;
        if (xioctl(fd, VIDIOC_QBUF, &b)) { perror("VIDIOC_QBUF"); goto done; }
    }
    enum v4l2_buf_type type = format.type;
    if (xioctl(fd, VIDIOC_STREAMON, &type)) { perror("VIDIOC_STREAMON"); goto done; }
    streaming = true;
    if (pthread_create(&writer, NULL, send_previews, &p)) goto done;
    thread_started = true;
    fprintf(stderr, "READY width=%u height=%u bits=%u stride=%u\n", p.width, p.height, p.bits, p.stride);
    uint64_t sample_interval = (uint64_t)(1000000000.0 / preview_fps), last_sample = 0;
    uint64_t first_ts = 0, window_ts = 0, window_frames = 0;
    uint32_t last_sequence = 0;
    struct metadata meta = {0};
    while (!quitting) {
        struct pollfd pollfd = {.fd = fd, .events = POLLIN};
        int ready = poll(&pollfd, 1, 500);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { perror("poll"); goto done; }
        if (!ready) continue;
        if (pollfd.revents & (POLLERR | POLLHUP | POLLNVAL)) { fprintf(stderr, "ERROR video poll failure\n"); goto done; }
        struct v4l2_plane plane = {0};
        struct v4l2_buffer b = {.type = type, .memory = V4L2_MEMORY_MMAP, .length = 1, .m.planes = &plane};
        if (xioctl(fd, VIDIOC_DQBUF, &b)) {
            if (errno == EAGAIN || errno == EINTR) continue;
            perror("VIDIOC_DQBUF"); goto done;
        }
        if (b.index >= mapped || plane.bytesused > buffers[b.index].length ||
            plane.bytesused < (size_t)p.stride * p.height) {
            fprintf(stderr, "ERROR invalid captured buffer\n"); goto done;
        }
        uint64_t ts = (uint64_t)b.timestamp.tv_sec * 1000000000ULL + (uint64_t)b.timestamp.tv_usec * 1000ULL;
        if (!ts) ts = now_ns();
        if (meta.frames && b.sequence != last_sequence + 1) meta.dropped += (uint32_t)(b.sequence - last_sequence - 1);
        if (b.flags & V4L2_BUF_FLAG_ERROR) meta.dropped++;
        last_sequence = b.sequence;
        meta.sequence = b.sequence; meta.frames++; meta.timestamp_ns = ts;
        if (!first_ts) { first_ts = ts; window_ts = ts; window_frames = meta.frames; }
        if (ts > window_ts) meta.fps = (double)(meta.frames - window_frames) * 1000000000.0 / (ts - window_ts);
        if (ts - window_ts >= 1000000000ULL) { window_ts = ts; window_frames = meta.frames; }
        uint64_t now = now_ns();
        if (now - last_sample >= sample_interval && !pthread_mutex_trylock(&p.mutex)) {
            memcpy(p.latest, buffers[b.index].address, (size_t)p.stride * p.height);
            p.length = (size_t)p.stride * p.height;
            p.meta = meta;
            p.fresh = true;
            pthread_cond_signal(&p.condition);
            pthread_mutex_unlock(&p.mutex);
            last_sample = now;
        }
        if (xioctl(fd, VIDIOC_QBUF, &b)) { perror("VIDIOC_QBUF"); goto done; }
    }
    ret = 0;
done:
    quitting = 1;
    pthread_cond_broadcast(&p.condition);
    if (streaming) { enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; xioctl(fd, VIDIOC_STREAMOFF, &type); }
    if (thread_started) pthread_join(writer, NULL);
    for (unsigned i = 0; i < mapped; i++) munmap(buffers[i].address, buffers[i].length);
    free(p.latest); close(fd);
    return ret;
}
