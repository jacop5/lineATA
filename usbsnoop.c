// LD_PRELOAD ioctl shim: dump usbfs URB control/bulk payloads (Keenetic Linear FXS / nvox)
// Build: zig cc -target mipsel-linux-musl -O2 -shared -fPIC usbsnoop.c -o usbsnoop.so
#define _GNU_SOURCE
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <time.h>
#include <sys/types.h>

#define UB 0x55u            // 'U' ioctl magic
#define IOC_NR(cmd)  ((unsigned)(cmd) & 0xFFu)
#define IOC_TYP(cmd) (((unsigned)(cmd) >> 8) & 0xFFu)

struct urb {
    uint8_t  type;
    uint8_t  endpoint;
    int32_t  status;
    uint32_t flags;
    void    *buffer;
    int32_t  buffer_length;
    int32_t  actual_length;
    uint32_t signr;
    void    *usercontext;
};
struct ctrltransfer {
    uint8_t  bRequest;
    uint8_t  bRequestType;
    uint16_t wValue;
    uint16_t wIndex;
    uint16_t wLength;
    uint32_t timeout;
    void    *data;
};

static int logfd = -1;

static int pl(char *o, const char *s) { int n = 0; while (s[n]) { o[n] = s[n]; n++; } return n; }
static int pi_(char *o, char c) { o[0] = c; return 1; }
static int pu_(char *o, unsigned v) {
    char t[12]; int n = 0;
    if (!v) t[n++] = '0';
    while (v) { t[n++] = '0' + v % 10; v /= 10; }
    int i = 0; while (n) o[i++] = t[--n];
    return i;
}
static int ph(char *o, unsigned char v) {
    static const char h[] = "0123456789abcdef";
    o[0] = h[v >> 4]; o[1] = h[v & 15];
    return 2;
}
static int pdump(char *o, const unsigned char *src, int n) {
    int i = 0;
    for (int k = 0; k < n; k++) i += ph(o + i, src[k]);
    return i;
}

static long tms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static void emit_ts(char *buf, int len) {
    char hdr[24]; int hn = 0;
    long t = tms();
    if (logfd < 0) logfd = open("/tmp/usbsnoop.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (logfd < 0) return;
    hdr[hn++] = '[';
    if (t == 0) hdr[hn++] = '0';
    char tb[12]; int n = 0;
    while (t) { tb[n++] = '0' + t % 10; t /= 10; }
    while (n) hdr[hn++] = tb[--n];
    hdr[hn++] = ']'; hdr[hn++] = ' ';
    write(logfd, hdr, hn);
    write(logfd, buf, len);
}
static void emit(char *buf, int len) {
    if (logfd < 0) logfd = open("/tmp/usbsnoop.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (logfd >= 0) write(logfd, buf, len);
}



/* ---- serial phase capture: open/read/write on ttys ---- */
static int is_ttyfd[64];
static int fd_inited[64];

static int ttycheck(int fd) {
    if (fd < 0 || fd >= 64) return 0;
    if (!fd_inited[fd]) {
        long r = syscall(SYS_ioctl, (long)fd, 0x5401 /*TCGETS*/, (long)0);
        is_ttyfd[fd] = (r == 0);
        fd_inited[fd] = 1;
        if (is_ttyfd[fd]) {
            char b[96]; int p = 0;
            b[p++]='T'; b[p++]=' '; b[p++]='t'; b[p++]='t'; b[p++]='y'; b[p++]=','; b[p++]=' ';
            char tb[12]; long v = fd; int n=0; if(!v) tb[n++]='0';
            while(v){tb[n++]='0'+v%10;v/=10;} int i=0;while(n) b[p++]=tb[--n];
            b[p++]='\n';
            emit_ts(b, p);
        }
    }
    return is_ttyfd[fd];
}

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    int fd = syscall(SYS_open, (long)path, (long)flags, (long)mode);
    if (fd >= 0 && fd < 64) { fd_inited[fd]=0; is_ttyfd[fd]=0; }
    if (fd >= 0) {
        const char *s = path;
        const char *own = "/tmp/usbsnoop";
        int ownflag = 1;
        for (int i = 0; i < 11; i++) if (!s[i] || s[i] != own[i]) { ownflag = 0; break; }
        if (!ownflag) {
            char b[160]; int p=0;
            b[p++]='O'; b[p++]=' ';
            while (*s && p < 150) b[p++]=*s++;
            b[p++]='\n';
            if (logfd >= 0) { write(logfd, b, p); } else { emit_ts(b, p); }
        }
    }
    return fd;
}

static void log_io(char tag, int fd, const void *buf, long n, long ret) {
    if (!ttycheck(fd) || ret <= 0) return;
    char out[600]; int p = 0;
    out[p++]=tag; out[p++]=' ';
    // fd decimal
    { char tb[12]; long v=fd; int n=0; if(!v) tb[n++]='0';
      while(v){tb[n++]='0'+v%10;v/=10;} while(n) out[p++]=tb[--n]; }
    out[p++]=' ';
    { char tb[12]; long v=ret; int n=0; if(!v) tb[n++]='0';
      while(v){tb[n++]='0'+v%10;v/=10;} while(n) out[p++]=tb[--n]; }
    out[p++]=' ';
    int lim = ret > 200 ? 200 : (int)ret;
    const unsigned char *src = (const unsigned char *)buf;
    for (int i = 0; i < lim; i++) p += ph(out + p, src[i]);
    out[p++]='\n';
    emit_ts(out, p);
}

ssize_t read(int fd, void *buf, size_t n) {
    long r = syscall(SYS_read, (long)fd, (long)buf, (long)n);
    log_io('r', fd, buf, n, r);
    return r;
}
ssize_t write(int fd, const void *buf, size_t n) {
    long r = syscall(SYS_write, (long)fd, (long)buf, (long)n);
    log_io('w', fd, buf, n, r);
    return r;
}

long ioctl(int fd, unsigned long cmd, void *arg) {
    long r = syscall(SYS_ioctl, (long)fd, (long)cmd, (long)arg);
    if (IOC_TYP(cmd) != UB || !arg) return r;
    char out[1024];
    int p = 0;
    unsigned nr = IOC_NR(cmd);
    if (nr == 10) {                    // SUBMITURB
        struct urb *u = (struct urb *)arg;
        p += pl(out + p, "S ");
        p += pu_(out + p, u->endpoint);
        p += pi_(out + p, ' ');
        p += pu_(out + p, (unsigned)u->buffer_length);
        if (u->buffer && u->buffer_length > 0 && (u->endpoint & 0x80) == 0) {
            int n = u->buffer_length > 200 ? 200 : u->buffer_length;
            p += pi_(out + p, ' ');
            p += pdump(out + p, (const unsigned char *)u->buffer, n);
        }
        out[p++] = '\n';
        emit_ts(out, p);
    } else if (nr == 12 || nr == 13) { // REAPURB / REAPURBNDELAY: arg -> urb*
        struct urb *u = *(struct urb **)arg;
        if (r == 0 && u) {
            p += pl(out + p, "R ");
            p += pu_(out + p, u->endpoint);
            p += pi_(out + p, ' ');
            p += pu_(out + p, (unsigned)u->actual_length);
            if (u->buffer && u->actual_length > 0 && (u->endpoint & 0x80)) {
                int n = u->actual_length > 200 ? 200 : u->actual_length;
                p += pi_(out + p, ' ');
                p += pdump(out + p, (const unsigned char *)u->buffer, n);
            }
            p += pl(out + p, " st=");
            p += pu_(out + p, (unsigned)u->status);
            out[p++] = '\n';
            emit_ts(out, p);
        }
    } else if (nr == 0) {              // CONTROL
        struct ctrltransfer *c = (struct ctrltransfer *)arg;
        p += pl(out + p, "C ");
        p += pu_(out + p, c->bRequestType);
        p += pi_(out + p, ' ');
        p += pu_(out + p, c->bRequest);
        p += pi_(out + p, ' ');
        p += pu_(out + p, c->wValue);
        p += pi_(out + p, ' ');
        p += pu_(out + p, c->wIndex);
        p += pi_(out + p, ' ');
        p += pu_(out + p, c->wLength);
        p += pi_(out + p, ' ');
        p += pu_(out + p, (long)r >= 0 ? (unsigned)r : 0);
        if ((c->bRequestType & 0x80) && c->data && c->wLength && (long)r > 0) {
            int n = c->wLength; if (n > (long)r) n = (int)r;
            if (n > 200) n = 200;
            p += pi_(out + p, ' ');
            p += pdump(out + p, (const unsigned char *)c->data, n);
        }
        out[p++] = '\n';
        emit_ts(out, p);
    }
    return r;
}
