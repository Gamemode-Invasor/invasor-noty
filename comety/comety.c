#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <signal.h>
#include <unistd.h>
#include <sys/file.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sqlite3.h>

#define DEFAULT_MS 1000UL
#define HTTP_TIMEOUT_MS 5000
#define MAX_BACKOFF_MS 30000UL

#define SQL_TABLE_BODY \
    "audit_id INTEGER PRIMARY KEY, " \
    "processed INTEGER NOT NULL CHECK (processed IN (0, 1)), " \
    "id INTEGER NOT NULL, " \
    "\"key\" TEXT NOT NULL, " \
    "name TEXT NOT NULL, " \
    "description TEXT NOT NULL, " \
    "visible_while_locked INTEGER NOT NULL CHECK (visible_while_locked IN (0, 1)), " \
    "unlock_time TEXT, " \
    "image_url_locked TEXT NOT NULL, " \
    "image_url_unlocked TEXT NOT NULL, " \
    "changed INTEGER NOT NULL CHECK (changed IN (0, 1)), " \
    "rarity REAL NOT NULL DEFAULT 0.0, " \
    "rarity_level_description TEXT NOT NULL DEFAULT \"\", " \
    "rarity_level_slug TEXT NOT NULL DEFAULT \"\""

#define SQL_CREATE_TABLE \
    "CREATE TABLE IF NOT EXISTS achievement_audit (" SQL_TABLE_BODY ")"

#define SQL_ADD_PROCESSED \
    "ALTER TABLE achievement_audit ADD COLUMN processed INTEGER NOT NULL " \
    "DEFAULT 0 CHECK (processed IN (0, 1))"

#define SQL_DROP_TRIGGER \
    "DROP TRIGGER IF EXISTS trg_achievement_after_update"

/* Solo un desbloqueo real: unlock_time pasa a tener valor o cambia. */
#define SQL_CREATE_TRIGGER \
    "CREATE TRIGGER trg_achievement_after_update " \
    "AFTER UPDATE ON achievement " \
    "WHEN NEW.unlock_time IS NOT NULL " \
    "AND NEW.unlock_time IS NOT OLD.unlock_time BEGIN " \
    "INSERT INTO achievement_audit (id, \"key\", name, description, " \
    "visible_while_locked, unlock_time, image_url_locked, image_url_unlocked, " \
    "changed, rarity, rarity_level_description, rarity_level_slug, processed) " \
    "VALUES (NEW.id, NEW.\"key\", NEW.name, NEW.description, " \
    "NEW.visible_while_locked, NEW.unlock_time, NEW.image_url_locked, " \
    "NEW.image_url_unlocked, NEW.changed, NEW.rarity, " \
    "NEW.rarity_level_description, NEW.rarity_level_slug, 0); END"

#define SQL_POLL \
    "SELECT * FROM achievement_audit WHERE processed = 0 ORDER BY audit_id"

#define SQL_MARK "UPDATE achievement_audit SET processed = 1 WHERE audit_id = ?"

static const char *g_prog = "comety";
static volatile sig_atomic_t g_stop = 0;
static int g_exit_code = 0;
static int g_lock_fd = -1;

struct options {
    const char *db;
    const char *url;
    unsigned long ms;
    int quiet;
};

struct url_parts {
    char host[256];
    char port[8];
    char path[1024];
};

static void usage(FILE *out)
{
    fprintf(out,
            "Uso: %s --url <web> [--quiet] <bd> [ms]\n"
            "       %s --help\n"
            "\n"
            "  <bd>            base de datos SQLite (lectura/escritura)\n"
            "  [ms]            intervalo de sondeo en milisegundos (por defecto 1000)\n"
            "  --url <web>     endpoint http:// al que enviar cada fila nueva (obligatorio)\n"
            "  --quiet         no imprime por pantalla las filas procesadas\n"
            "  --help          muestra esta ayuda y termina\n"
            "\n"
            "Por cada fila nueva de achievement_audit se hace un POST con su JSON\n"
            "(sin las columnas audit_id ni processed). Si la respuesta es 200 la\n"
            "fila se marca processed=1; un 400/422 (la fila no es válida) la descarta\n"
            "marcándola también; cualquier otro fallo (429, 5xx, sin conexión...)\n"
            "detiene la tanda y se reintenta en el siguiente intervalo. Solo se\n"
            "audita un desbloqueo real (unlock_time nuevo o cambiado). Termina si\n"
            "muere el proceso que lo lanzó o si otra instancia ya vigila la bd.\n",
            g_prog, g_prog);
}

static void free_cols(char **cols, int n)
{
    int i;

    for (i = 0; i < n; i++)
        free(cols[i]);
    free(cols);
}

static int col_index(char **cols, int n, const char *name)
{
    int i;

    for (i = 0; i < n; i++) {
        if (!strcmp(cols[i], name))
            return i;
    }
    return -1;
}

static int table_columns(sqlite3 *db, const char *table, char ***out, int *out_n)
{
    sqlite3_stmt *st = NULL;
    char sql[256];
    char **cols = NULL;
    int n = 0, cap = 0;

    *out = NULL;
    *out_n = 0;
    snprintf(sql, sizeof(sql), "SELECT name FROM pragma_table_info('%s')",
             table);
    if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) != SQLITE_OK)
        return -1;
    while (sqlite3_step(st) == SQLITE_ROW) {
        const char *nm = (const char *)sqlite3_column_text(st, 0);

        if (n == cap) {
            int nc = cap ? cap * 2 : 16;
            char **np = realloc(cols, (size_t)nc * sizeof(*cols));

            if (!np) {
                sqlite3_finalize(st);
                free_cols(cols, n);
                return -1;
            }
            cols = np;
            cap = nc;
        }
        cols[n] = strdup(nm ? nm : "");
        if (!cols[n]) {
            sqlite3_finalize(st);
            free_cols(cols, n);
            return -1;
        }
        n++;
    }
    sqlite3_finalize(st);
    *out = cols;
    *out_n = n;
    return 0;
}

static int ensure_schema(sqlite3 *db, const char *db_path, int *pollable)
{
    static int warned_incompat = 0;
    char **cols = NULL;
    char *err = NULL;
    int ncols = 0;
    int rc;

    *pollable = 0;

    rc = sqlite3_exec(db, SQL_CREATE_TABLE, NULL, NULL, &err);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "%s: %s: %s\n", g_prog, db_path,
                err ? err : sqlite3_errmsg(db));
        sqlite3_free(err);
        return rc;
    }

    if (table_columns(db, "achievement_audit", &cols, &ncols) < 0) {
        fprintf(stderr, "%s: %s: no se pudo leer el esquema de "
                "achievement_audit\n", g_prog, db_path);
        return SQLITE_ERROR;
    }

    if (col_index(cols, ncols, "audit_id") < 0) {
        if (!warned_incompat) {
            fprintf(stderr,
                    "%s: %s: esquema incompatible, falta 'audit_id' en "
                    "achievement_audit (se conserva la tabla existente)\n",
                    g_prog, db_path);
            warned_incompat = 1;
        }
        free_cols(cols, ncols);
        return SQLITE_OK;
    }

    if (col_index(cols, ncols, "processed") < 0) {
        rc = sqlite3_exec(db, SQL_ADD_PROCESSED, NULL, NULL, &err);
        if (rc != SQLITE_OK) {
            fprintf(stderr, "%s: %s: %s\n", g_prog, db_path,
                    err ? err : sqlite3_errmsg(db));
            sqlite3_free(err);
            free_cols(cols, ncols);
            return rc;
        }
        free_cols(cols, ncols);
        cols = NULL;
        ncols = 0;
        if (table_columns(db, "achievement_audit", &cols, &ncols) < 0) {
            fprintf(stderr, "%s: %s: no se pudo leer el esquema de "
                    "achievement_audit\n", g_prog, db_path);
            return SQLITE_ERROR;
        }
    }

    free_cols(cols, ncols);

    rc = sqlite3_exec(db, SQL_DROP_TRIGGER, NULL, NULL, &err);
    if (rc == SQLITE_OK) {
        sqlite3_free(err);
        err = NULL;
        rc = sqlite3_exec(db, SQL_CREATE_TRIGGER, NULL, NULL, &err);
    }
    if (rc != SQLITE_OK) {
        fprintf(stderr, "%s: %s: %s\n", g_prog, db_path,
                err ? err : sqlite3_errmsg(db));
        sqlite3_free(err);
        return rc;
    }
    warned_incompat = 0;
    *pollable = 1;
    return SQLITE_OK;
}

static void print_row(sqlite3_stmt *st, int ncols)
{
    int i;

    putchar('(');
    for (i = 0; i < ncols; i++) {
        const unsigned char *txt;
        int type = sqlite3_column_type(st, i);

        if (i > 0)
            fputs(", ", stdout);
        if (type == SQLITE_NULL) {
            fputs("None", stdout);
            continue;
        }
        if (type == SQLITE_TEXT)
            putchar('\'');
        txt = sqlite3_column_text(st, i);
        if (txt)
            fwrite(txt, 1, (size_t)sqlite3_column_bytes(st, i), stdout);
        if (type == SQLITE_TEXT)
            putchar('\'');
    }
    putchar(')');
    putchar('\n');
}

struct sbuf {
    char *p;
    size_t len;
    size_t cap;
};

static int sb_add(struct sbuf *b, const char *s, size_t n)
{
    if (b->len + n + 1 > b->cap) {
        size_t ncap = b->cap ? b->cap : 512;
        char *np;

        while (ncap < b->len + n + 1)
            ncap *= 2;
        np = realloc(b->p, ncap);
        if (!np)
            return -1;
        b->p = np;
        b->cap = ncap;
    }
    memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = '\0';
    return 0;
}

static int json_escape(struct sbuf *b, const unsigned char *s, size_t n)
{
    size_t i;

    for (i = 0; i < n; i++) {
        unsigned char c = s[i];
        char tmp[8];

        switch (c) {
        case '"':  if (sb_add(b, "\\\"", 2)) return -1; break;
        case '\\': if (sb_add(b, "\\\\", 2)) return -1; break;
        case '\b': if (sb_add(b, "\\b", 2)) return -1; break;
        case '\f': if (sb_add(b, "\\f", 2)) return -1; break;
        case '\n': if (sb_add(b, "\\n", 2)) return -1; break;
        case '\r': if (sb_add(b, "\\r", 2)) return -1; break;
        case '\t': if (sb_add(b, "\\t", 2)) return -1; break;
        default:
            if (c < 0x20) {
                snprintf(tmp, sizeof(tmp), "\\u%04x", c);
                if (sb_add(b, tmp, strlen(tmp))) return -1;
            } else {
                if (sb_add(b, (const char *)&c, 1)) return -1;
            }
            break;
        }
    }
    return 0;
}

static int json_isfinite(const char *s)
{
    for (; *s; s++) {
        if (*s == 'i' || *s == 'I' || *s == 'n' || *s == 'N')
            return 0;
    }
    return 1;
}

static char *build_row_json(sqlite3_stmt *st, int ncols)
{
    struct sbuf b = { NULL, 0, 0 };
    int i, first = 1;

    if (sb_add(&b, "{", 1))
        goto fail;

    for (i = 0; i < ncols; i++) {
        const char *name = sqlite3_column_name(st, i);
        int type = sqlite3_column_type(st, i);
        const unsigned char *txt;
        char tmp[32];

        if (!name || !strcmp(name, "audit_id") || !strcmp(name, "processed"))
            continue;
        if (!first && sb_add(&b, ",", 1))
            goto fail;
        first = 0;

        if (sb_add(&b, "\"", 1) ||
            json_escape(&b, (const unsigned char *)name, strlen(name)) ||
            sb_add(&b, "\":", 2))
            goto fail;

        if (type == SQLITE_NULL) {
            if (sb_add(&b, "null", 4))
                goto fail;
            continue;
        }
        if (type == SQLITE_INTEGER) {
            snprintf(tmp, sizeof(tmp), "%lld",
                     (long long)sqlite3_column_int64(st, i));
            if (sb_add(&b, tmp, strlen(tmp)))
                goto fail;
            continue;
        }

        txt = sqlite3_column_text(st, i);
        if (!txt) {
            if (sb_add(&b, "null", 4))
                goto fail;
            continue;
        }
        if (type == SQLITE_FLOAT) {
            if (!json_isfinite((const char *)txt)) {
                if (sb_add(&b, "null", 4))
                    goto fail;
            } else if (sb_add(&b, (const char *)txt,
                              (size_t)sqlite3_column_bytes(st, i))) {
                goto fail;
            }
            continue;
        }
        if (sb_add(&b, "\"", 1) ||
            json_escape(&b, txt, (size_t)sqlite3_column_bytes(st, i)) ||
            sb_add(&b, "\"", 1))
            goto fail;
    }

    if (sb_add(&b, "}", 1))
        goto fail;
    return b.p;

fail:
    free(b.p);
    return NULL;
}

static int parse_url(const char *url, struct url_parts *u)
{
    const char *p = url + 7;
    const char *slash, *hostend, *colon;
    size_t n;

    slash = strchr(p, '/');
    hostend = slash ? slash : p + strlen(p);
    if (hostend == p)
        return -1;

    colon = memchr(p, ':', (size_t)(hostend - p));
    if (colon && memchr(colon + 1, ':', (size_t)(hostend - colon - 1)))
        colon = NULL;

    n = (size_t)((colon ? colon : hostend) - p);
    if (n == 0 || n >= sizeof(u->host) || memchr(p, '@', n))
        return -1;
    memcpy(u->host, p, n);
    u->host[n] = '\0';

    if (colon) {
        n = (size_t)(hostend - colon - 1);
        if (n == 0 || n >= sizeof(u->port))
            return -1;
        memcpy(u->port, colon + 1, n);
        u->port[n] = '\0';
    } else {
        strcpy(u->port, "80");
    }

    if (slash) {
        n = strlen(slash);
        if (n >= sizeof(u->path))
            return -1;
        memcpy(u->path, slash, n + 1);
    } else {
        strcpy(u->path, "/");
    }
    return 0;
}

static int connect_timeout(int fd, const struct sockaddr *sa, socklen_t len)
{
    int fl = fcntl(fd, F_GETFL, 0);
    int rc, soerr = 0;
    socklen_t sl = sizeof(soerr);
    fd_set w;
    struct timeval tv;

    if (fl < 0 || fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0)
        return -1;
    rc = connect(fd, sa, len);
    if (rc == 0)
        goto done;
    if (errno != EINPROGRESS)
        return -1;

    FD_ZERO(&w);
    FD_SET(fd, &w);
    tv.tv_sec = HTTP_TIMEOUT_MS / 1000;
    tv.tv_usec = (HTTP_TIMEOUT_MS % 1000) * 1000;
    rc = select(fd + 1, NULL, &w, NULL, &tv);
    if (rc <= 0) {
        errno = (rc == 0) ? ETIMEDOUT : errno;
        return -1;
    }
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl) < 0 || soerr) {
        if (soerr)
            errno = soerr;
        return -1;
    }

done:
    fcntl(fd, F_SETFL, fl);
    return 0;
}

static int send_all(int fd, const char *buf, size_t len)
{
    while (len > 0) {
        ssize_t n = send(fd, buf, len, MSG_NOSIGNAL);

        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        buf += n;
        len -= (size_t)n;
    }
    return 0;
}

static int http_post(const struct url_parts *u, const char *body,
                     size_t body_len, int *status, char *err, size_t errlen)
{
    struct addrinfo hints, *res = NULL, *ai;
    struct timeval tv;
    char header[2048];
    char buf[4096];
    int fd = -1, hlen, rc = -1;
    ssize_t n;

    *status = 0;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    rc = getaddrinfo(u->host, u->port, &hints, &res);
    if (rc != 0) {
        snprintf(err, errlen, "%s", gai_strerror(rc));
        return -1;
    }

    for (ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0)
            continue;
        if (connect_timeout(fd, ai->ai_addr, ai->ai_addrlen) == 0)
            break;
        close(fd);
        fd = -1;
    }
    if (fd < 0) {
        snprintf(err, errlen, "%s", strerror(errno));
        freeaddrinfo(res);
        return -1;
    }
    freeaddrinfo(res);
    rc = -1;

    tv.tv_sec = HTTP_TIMEOUT_MS / 1000;
    tv.tv_usec = (HTTP_TIMEOUT_MS % 1000) * 1000;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    hlen = snprintf(header, sizeof(header),
                    "POST %s HTTP/1.1\r\n"
                    "Host: %s%s%s\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: %zu\r\n"
                    "Connection: close\r\n"
                    "\r\n",
                    u->path, u->host,
                    strcmp(u->port, "80") ? ":" : "", u->port, body_len);
    if (hlen < 0 || (size_t)hlen >= sizeof(header)) {
        snprintf(err, errlen, "cabecera demasiado grande");
        goto out;
    }
    if (send_all(fd, header, (size_t)hlen) < 0 ||
        (body_len > 0 && send_all(fd, body, body_len) < 0)) {
        snprintf(err, errlen, "%s", strerror(errno));
        goto out;
    }

    {
        size_t got = 0;

        for (;;) {
            char *eol;

            n = recv(fd, buf + got, sizeof(buf) - 1 - got, 0);
            if (n < 0) {
                if (errno == EINTR)
                    continue;
                snprintf(err, errlen, "%s", strerror(errno));
                goto out;
            }
            if (n == 0) {
                snprintf(err, errlen, "sin respuesta del servidor");
                goto out;
            }
            got += (size_t)n;
            buf[got] = '\0';

            if (strncmp(buf, "HTTP/", 5) != 0) {
                snprintf(err, errlen, "respuesta no válida");
                goto out;
            }
            eol = strstr(buf, "\r\n");
            if (!eol)
                eol = strchr(buf, '\n');
            if (eol) {
                char *sp = strchr(buf, ' ');

                if (sp && sp < eol) {
                    *status = atoi(sp + 1);
                    if (*status > 0) {
                        rc = 0;
                        goto out;
                    }
                }
                snprintf(err, errlen, "respuesta no válida");
                goto out;
            }
            if (got >= sizeof(buf) - 1) {
                snprintf(err, errlen, "respuesta no válida");
                goto out;
            }
        }
    }

out:
    close(fd);
    return rc;
}

static int parse_args(int argc, char **argv, struct options *o)
{
    int i, ms_set = 0;

    o->db = NULL;
    o->url = NULL;
    o->ms = DEFAULT_MS;
    o->quiet = 0;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        char *end;

        if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(stdout);
            return 1;
        }
        if (!strcmp(a, "--quiet")) {
            o->quiet = 1;
            continue;
        }
        if (!strcmp(a, "--url")) {
            if (++i >= argc) {
                fprintf(stderr, "%s: --url necesita un valor\n", g_prog);
                usage(stderr);
                return -1;
            }
            o->url = argv[i];
            continue;
        }
        if (!strncmp(a, "--", 2)) {
            fprintf(stderr, "%s: parámetro desconocido: %s\n", g_prog, a);
            usage(stderr);
            return -1;
        }
        if (!o->db) {
            o->db = a;
            continue;
        }
        if (ms_set) {
            fprintf(stderr, "%s: argumento sobrante: '%s'\n", g_prog, a);
            usage(stderr);
            return -1;
        }
        o->ms = strtoul(a, &end, 10);
        if (*a == '\0' || *end != '\0' || o->ms == 0) {
            fprintf(stderr, "%s: el intervalo debe ser un entero > 0: '%s'\n",
                    g_prog, a);
            return -1;
        }
        ms_set = 1;
    }

    if (!o->url) {
        fprintf(stderr, "%s: falta el parámetro obligatorio --url <web>\n",
                g_prog);
        usage(stderr);
        return -1;
    }
    if (!o->db) {
        fprintf(stderr, "%s: falta el parámetro <bd>\n", g_prog);
        usage(stderr);
        return -1;
    }
    if (!strncmp(o->url, "https://", 8)) {
        fprintf(stderr, "%s: https:// no soportado, usa http://\n", g_prog);
        return -1;
    }
    if (strncmp(o->url, "http://", 7)) {
        fprintf(stderr, "%s: la URL debe empezar por http://\n", g_prog);
        return -1;
    }
    return 0;
}

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* 0: bloqueada por esta instancia; 1: otra instancia ya vigila la bd;
 * -1: no se puede abrir (*err = errno). Es un flock sobre la propia bd: no
 * interfiere con los bloqueos de SQLite (fcntl) y desaparece al morir. */
static int lock_db(const char *path, int *err)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);

    if (fd < 0) {
        *err = errno;
        return -1;
    }
    if (flock(fd, LOCK_EX | LOCK_NB) < 0) {
        int busy = (errno == EWOULDBLOCK);

        *err = errno;
        close(fd);
        return busy ? 1 : -1;
    }
    g_lock_fd = fd;
    return 0;
}

enum { SEND_OK, SEND_DROP, SEND_RETRY };

/* 200: hecho. 400/422: la fila no vale y reenviarla no lo arreglará: se
 * descarta. Todo lo demás (429, 5xx, 404 de una URL mal puesta, sin
 * conexión...) es temporal: la fila se conserva. */
static int classify(int rc, int status)
{
    if (rc < 0)
        return SEND_RETRY;
    if (status == 200)
        return SEND_OK;
    if (status == 400 || status == 422)
        return SEND_DROP;
    return SEND_RETRY;
}

/* Un ciclo de sondeo. Devuelve 1 si falló algo de la bd (se espera más antes
 * del siguiente), 0 si fue bien o solo falló el envío. */
static int poll_once(const struct options *o, const struct url_parts *url,
                     sqlite3 **dbp, int *schema_ok)
{
    sqlite3 *db = *dbp;
    sqlite3_stmt *st = NULL;
    sqlite3_int64 *ids = NULL;
    size_t nids = 0, caps = 0;
    int rc, aborted = 0;

    if (!db) {
        int err = 0;
        int lk = g_lock_fd >= 0 ? 0 : lock_db(o->db, &err);

        if (lk > 0) {
            fprintf(stderr, "%s: %s: ya lo vigila otra instancia\n", g_prog,
                    o->db);
            g_exit_code = 1;
            g_stop = 1;
            return 1;
        }
        if (lk < 0) {
            fprintf(stderr, "%s: %s: %s\n", g_prog, o->db, strerror(err));
            return 1;
        }
        rc = sqlite3_open_v2(o->db, &db, SQLITE_OPEN_READWRITE, NULL);
        if (rc != SQLITE_OK) {
            fprintf(stderr, "%s: %s: %s\n", g_prog, o->db,
                    db ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
            if (db)
                sqlite3_close(db);
            return 1;
        }
        sqlite3_busy_timeout(db, 5000);
        *dbp = db;
        *schema_ok = 0;
    }

    if (!*schema_ok) {
        int pollable = 0;

        if (ensure_schema(db, o->db, &pollable) != SQLITE_OK || !pollable) {
            sqlite3_close(db);
            *dbp = NULL;
            return 1;
        }
        *schema_ok = 1;
    }

    rc = sqlite3_prepare_v2(db, SQL_POLL, -1, &st, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "%s: %s: %s\n", g_prog, o->db, sqlite3_errmsg(db));
        sqlite3_close(db);
        *dbp = NULL;
        return 1;
    }

    {
        int ncols = sqlite3_column_count(st);
        int idx_audit = -1;
        int i;

        for (i = 0; i < ncols; i++) {
            if (strcmp(sqlite3_column_name(st, i), "audit_id") == 0) {
                idx_audit = i;
                break;
            }
        }

        while ((rc = sqlite3_step(st)) == SQLITE_ROW) {
            sqlite3_int64 id = idx_audit >= 0 ?
                               sqlite3_column_int64(st, idx_audit) : -1;
            char *json;
            char herr[256];
            int status = 0, verdict;

            if (g_stop) {
                aborted = 1;
                break;
            }
            json = build_row_json(st, ncols);
            if (!json) {
                fprintf(stderr, "%s: sin memoria (audit_id=%lld)\n",
                        g_prog, (long long)id);
                continue;
            }
            herr[0] = '\0';
            rc = http_post(url, json, strlen(json), &status, herr,
                           sizeof(herr));
            free(json);
            verdict = classify(rc, status);

            if (verdict == SEND_RETRY) {
                /* Se conserva esta fila y las siguientes: se reintenta en el
                 * próximo intervalo, en orden. */
                if (rc < 0)
                    fprintf(stderr, "%s: %s (audit_id=%lld): %s\n", g_prog,
                            o->url, (long long)id, herr);
                else
                    fprintf(stderr, "%s: %s (audit_id=%lld): HTTP %d\n",
                            g_prog, o->url, (long long)id, status);
                aborted = 1;
                break;
            }
            if (verdict == SEND_DROP)
                fprintf(stderr, "%s: %s (audit_id=%lld): HTTP %d, fila "
                        "descartada\n", g_prog, o->url, (long long)id, status);
            else if (!o->quiet)
                print_row(st, ncols);

            if (nids == caps) {
                size_t nc = caps ? caps * 2 : 64;
                sqlite3_int64 *np = realloc(ids, nc * sizeof(*ids));

                if (!np) {
                    fprintf(stderr, "%s: sin memoria (audit_id=%lld)\n",
                            g_prog, (long long)id);
                    aborted = 1;
                    break;
                }
                ids = np;
                caps = nc;
            }
            ids[nids++] = id;
        }
        if (!aborted && rc != SQLITE_DONE) {
            fprintf(stderr, "%s: %s: %s\n", g_prog, o->db, sqlite3_errmsg(db));
            *schema_ok = 0;
        }
    }
    sqlite3_finalize(st);
    fflush(stdout);

    /* Las filas ya enviadas se marcan siempre, aunque la tanda se haya
     * interrumpido (error de envío o SIGTERM): así no se reenvían. */
    if (nids > 0) {
        sqlite3_stmt *mk = NULL;

        rc = sqlite3_prepare_v2(db, SQL_MARK, -1, &mk, NULL);
        if (rc == SQLITE_OK) {
            size_t k;

            for (k = 0; k < nids; k++) {
                sqlite3_bind_int64(mk, 1, ids[k]);
                if (sqlite3_step(mk) != SQLITE_DONE)
                    fprintf(stderr, "%s: %s (audit_id=%lld): %s\n", g_prog,
                            o->db, (long long)ids[k], sqlite3_errmsg(db));
                sqlite3_reset(mk);
            }
            sqlite3_finalize(mk);
        } else {
            fprintf(stderr, "%s: %s: %s\n", g_prog, o->db,
                    sqlite3_errmsg(db));
        }
    }
    free(ids);
    return 0;
}

static void nap(unsigned long ms)
{
    struct timespec ts;

    ts.tv_sec = (time_t)(ms / 1000);
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL); /* una señal lo interrumpe: g_stop se mira después */
}

int main(int argc, char **argv)
{
    struct options o;
    struct url_parts url;
    struct sigaction sa;
    sqlite3 *db = NULL;
    unsigned long delay, cap;
    pid_t parent = getppid();
    int schema_ok = 0, rc_args;

    signal(SIGPIPE, SIG_IGN);

    rc_args = parse_args(argc, argv, &o);
    if (rc_args != 0)
        return rc_args > 0 ? 0 : 1;

    if (parse_url(o.url, &url) < 0) {
        fprintf(stderr, "%s: URL no válida: %s\n", g_prog, o.url);
        return 1;
    }

    /* SIGTERM/SIGINT: acabar lo que se esté haciendo (sin SA_RESTART, para
     * que la espera se interrumpa) y salir. */
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    cap = o.ms > MAX_BACKOFF_MS ? o.ms : MAX_BACKOFF_MS;
    delay = o.ms;

    while (!g_stop) {
        /* El proceso que nos lanzó murió (nos adoptó otro): no quedarse huérfano. */
        if (getppid() != parent)
            break;
        if (poll_once(&o, &url, &db, &schema_ok))
            delay = delay * 2 > cap ? cap : delay * 2; /* bd ausente o rota */
        else
            delay = o.ms;
        if (!g_stop)
            nap(delay);
    }

    if (db)
        sqlite3_close(db);
    if (g_lock_fd >= 0)
        close(g_lock_fd);
    return g_exit_code;
}
