#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_pm.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mdns.h"
#include "nvs.h"
#include "remote.h"

#define AP_SSID      "HX711-Scale"
#define AP_PASS      "scale1234"   // WPA2 needs at least 8 characters
#define HOSTNAME     "scale"       // http://scale.local until the scale is given a name
#define STA_RETRIES  5
#define STA_RETRY_AFTER_S 30   // after giving up, try the home Wi-Fi again this often

extern const char index_html_start[] asm("_binary_index_html_start");
extern const char index_html_end[]   asm("_binary_index_html_end");

// Home Wi-Fi state, shown on the page
static char s_sta_ssid[33];
static char s_sta_ip[16];
static const char *volatile s_sta_state = "off";  // off, connecting, connected, failed
static int s_retries;
static esp_timer_handle_t s_retry_timer;
static bool s_fail_logged;   // log a lost home Wi-Fi once, not on every retry
static bool s_ap_on;         // the scale's own network keeps the radio awake, so it only runs when needed
static wifi_config_t s_ap_cfg = {
    .ap = {
        .ssid = AP_SSID,
        .ssid_len = sizeof AP_SSID - 1,
        .password = AP_PASS,
        .channel = 1,
        .max_connection = 4,
        .authmode = WIFI_AUTH_WPA2_PSK,
    },
};

// The scale's own network is the way in when there's no home Wi-Fi. While the home Wi-Fi works it is
// switched off, which lets the radio sleep between router beacons.
static void set_ap(bool on)
{
    if (on == s_ap_on) return;
    s_ap_on = on;
    esp_wifi_set_mode(on ? WIFI_MODE_APSTA : WIFI_MODE_STA);
    if (on) {
        esp_wifi_set_config(WIFI_IF_AP, &s_ap_cfg);
        remote_log("Wi-Fi \"" AP_SSID "\" is up (password " AP_PASS "): open http://192.168.4.1");
    } else {
        remote_log("Wi-Fi \"" AP_SSID "\" is off while the home Wi-Fi works (saves power)");
    }
}

// The scale's name, e.g. "Kitchen Scale", and its address made from it: http://kitchen-scale.local
static char s_name[33] = "Scale";
static char s_host[33] = HOSTNAME;
static esp_netif_t *s_sta_netif;

// Lowercase letters and digits, everything else becomes a single '-'.
static void make_host(const char *name, char *host, size_t len)
{
    size_t n = 0;
    for (const char *p = name; *p && n < len - 1; p++) {
        char c = *p;
        if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) host[n++] = c;
        else if (n && host[n - 1] != '-') host[n++] = '-';
    }
    while (n && host[n - 1] == '-') n--;
    host[n] = 0;
    if (!n) strlcpy(host, HOSTNAME, len);
}

static void load_name(void)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READONLY, &h) != ESP_OK) return;
    size_t len = sizeof s_name;
    if (nvs_get_str(h, "name", s_name, &len) == ESP_OK) make_host(s_name, s_host, sizeof s_host);
    nvs_close(h);
}

// ---------- Wi-Fi ----------
// The Wi-Fi driver keeps the home network's name and password in NVS itself.
static void sta_connect_saved(void)
{
    wifi_config_t cfg;
    esp_wifi_get_config(WIFI_IF_STA, &cfg);
    strlcpy(s_sta_ssid, (const char *)cfg.sta.ssid, sizeof s_sta_ssid);
    s_sta_ip[0] = 0;
    if (!s_sta_ssid[0]) {
        s_sta_state = "off";
        set_ap(true);
        return;
    }
    s_retries = 0;
    s_sta_state = "connecting";
    esp_wifi_connect();
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        sta_connect_saved();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_sta_ip[0] = 0;
        if (!strcmp(s_sta_state, "off") || !strcmp(s_sta_state, "failed")) return;
        // Give up after a few tries: retrying makes the radio scan, which upsets the scale's own network.
        if (++s_retries <= STA_RETRIES) {
            s_sta_state = "connecting";
            esp_wifi_connect();
        } else {
            s_sta_state = "failed";
            set_ap(true);
            // Try again later, so a router restart or a brief dropout doesn't need a reboot.
            esp_timer_start_once(s_retry_timer, STA_RETRY_AFTER_S * 1000000LL);
            if (s_fail_logged) return;
            s_fail_logged = true;
            int reason = ((wifi_event_sta_disconnected_t *)data)->reason;
            const char *why =
                reason == WIFI_REASON_NO_AP_FOUND ? "network not found (out of range, or 5 GHz only?)" :
                reason == WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY ? "router security not supported (WPA3 only?)" :
                reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT || reason == WIFI_REASON_HANDSHAKE_TIMEOUT ||
                reason == WIFI_REASON_AUTH_FAIL ? "wrong password?" : "see reason code";
            remote_log("!!! Could not join Wi-Fi \"%s\": %s (reason %d). Trying again every %d s.",
                       s_sta_ssid, why, reason, STA_RETRY_AFTER_S);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        snprintf(s_sta_ip, sizeof s_sta_ip, IPSTR, IP2STR(&e->ip_info.ip));
        s_retries = 0;
        s_fail_logged = false;
        s_sta_state = "connected";
        set_ap(false);
        remote_log(">>> Joined Wi-Fi \"%s\": open http://%s or http://%s.local", s_sta_ssid, s_sta_ip, s_host);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        remote_log("A device joined the " AP_SSID " network");
    }
}

static void retry_sta(void *arg)
{
    if (!strcmp(s_sta_state, "failed")) sta_connect_saved();
}

static void wifi_start(void)
{
    const esp_timer_create_args_t targs = { .callback = retry_sta, .name = "sta_retry" };
    esp_timer_create(&targs, &s_retry_timer);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    s_sta_netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(s_sta_netif, s_host);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    // A network built into the firmware, for a board that has never joined
    // one. Whatever the driver last saved wins over it, which is why this is
    // read before the saved config below rather than instead of it.
    if (CONFIG_SCALE_HOME_WIFI_PASS[0]) {
        wifi_config_t sta = {0};
        strlcpy((char *)sta.sta.ssid, CONFIG_SCALE_HOME_WIFI_SSID, sizeof sta.sta.ssid);
        strlcpy((char *)sta.sta.password, CONFIG_SCALE_HOME_WIFI_PASS, sizeof sta.sta.password);
        esp_wifi_set_config(WIFI_IF_STA, &sta);
    }
    wifi_config_t sta;
    esp_wifi_get_config(WIFI_IF_STA, &sta);
    if (!sta.sta.ssid[0]) set_ap(true);  // no home Wi-Fi saved: the scale's own network is the only way in
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_max_tx_power(52);  // 13 dBm instead of 20: smaller current spikes, still fine indoors
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);  // radio sleeps between router beacons (only while the scale's own network is off)
}

// ---------- web server ----------
static char s_json[8192];
static size_t s_n;

static void add(const char *fmt, ...)
{
    if (s_n >= sizeof s_json) return;
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(s_json + s_n, sizeof s_json - s_n, fmt, ap);
    va_end(ap);
    if (w > 0) s_n += w;
}

static void add_str(const char *s)
{
    add("\"");
    for (; *s; s++) {
        unsigned char c = *s;
        if (c == '"' || c == '\\') add("\\%c", c);
        else if (c < 0x20) add("\\u%04x", c);
        else add("%c", c);
    }
    add("\"");
}

static esp_err_t get_index(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache");  // so a firmware update shows the new page
    return httpd_resp_send(req, index_html_start, index_html_end - index_html_start);
}

// Firmware version (PROJECT_VER in CMakeLists.txt) and build ID (start of the firmware's SHA-256),
// which changes with every build even when the version doesn't.
static const char *fw_version(void) { return esp_app_get_description()->version; }
static const char *fw_build(void)
{
    static char id[9];
    if (!id[0]) esp_app_get_elf_sha256(id, sizeof id);
    return id;
}

// Just the weight, for other programs: {"grams":245.0,"unit":"g","stable":true,"status":"ok",...}
// status is "ok", "starting", "not_calibrated", "sensor_error" or "overload"; grams is null unless "ok".
static esp_err_t get_weight(httpd_req_t *req)
{
    scale_reading_t r;
    uint32_t seq = remote_get_reading(&r);
    const char *status = seq == 0 ? "starting" : !r.ok ? "sensor_error" : r.saturated ? "overload"
                       : r.factor == 0 ? "not_calibrated" : "ok";
    char g[16] = "null";
    if (strcmp(status, "ok") == 0) {
        float grams = r.grams;
        if (grams < 0 && grams > -2.5f * r.step) grams = 0;  // small dips below zero read 0, as on the page
        snprintf(g, sizeof g, "%.1f", grams);
    }
    unsigned long age = seq ? (esp_timer_get_time() - r.at_us) / 1000000 : 0;
    int pct = seq ? remote_batt_pct(r.batt) : -1;
    char batt[64] = "null";
    if (pct >= 0)
        snprintf(batt, sizeof batt, "{\"volts\":%.2f,\"percent\":%d,\"low\":%s}",
                 r.batt, pct, pct < BATT_LOW_PCT ? "true" : "false");
    char buf[320];
    int n = snprintf(buf, sizeof buf,
                     "{\"name\":\"%s\",\"grams\":%s,\"unit\":\"g\",\"stable\":%s,\"status\":\"%s\",\"step\":%g,\"seq\":%lu,"
                     "\"interval_s\":%lu,\"age_s\":%lu,\"battery\":%s,\"fw\":\"%s\",\"build\":\"%s\"}",
                     s_name, g, r.stable ? "true" : "false", status, r.step, (unsigned long)seq,
                     (unsigned long)r.interval_s, age, batt, fw_version(), fw_build());
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, buf, n);
}

// ?since=N returns the log lines after the first N.
static esp_err_t get_status(httpd_req_t *req)
{
    uint32_t since = 0;
    char q[32], v[12];
    if (httpd_req_get_url_query_str(req, q, sizeof q) == ESP_OK &&
        httpd_query_key_value(q, "since", v, sizeof v) == ESP_OK)
        since = strtoul(v, NULL, 10);

    scale_reading_t r;
    uint32_t seq = remote_get_reading(&r);
    uint32_t logs = remote_log_count();

    s_n = 0;
    add("{\"seq\":%lu,\"ok\":%s,\"sat\":%s,\"stable\":%s,\"step\":%g,\"raw\":%ld,\"delta\":%ld,",
        (unsigned long)seq, r.ok ? "true" : "false", r.saturated ? "true" : "false",
        r.stable ? "true" : "false", r.step, (long)r.raw, (long)r.delta);
    add("\"interval\":%lu,\"age\":%lu,", (unsigned long)r.interval_s,
        (unsigned long)(seq ? (esp_timer_get_time() - r.at_us) / 1000000 : 0));
    if (r.factor != 0) add("\"grams\":%.1f,", r.grams);
    else add("\"grams\":null,");
    add("\"factor\":%.3f,\"batt\":%.2f,\"batt_pct\":%d,\"up\":%lu,\"heap\":%lu,\"fw\":\"%s\",\"build\":\"%s\",",
        r.factor, r.batt, remote_batt_pct(r.batt), (unsigned long)(esp_timer_get_time() / 1000000),
        (unsigned long)esp_get_free_heap_size(), fw_version(), fw_build());
    add("\"name\":");
    add_str(s_name);
    add(",\"host\":\"%s\",", s_host);
    add("\"wifi\":{\"ap\":\"" AP_SSID "\",\"ap_on\":%s,\"state\":\"%s\",\"ssid\":", s_ap_on ? "true" : "false", s_sta_state);
    add_str(s_sta_ssid);
    add(",\"ip\":\"%s\"},\"logs\":[", s_sta_ip);

    uint32_t first = remote_log_oldest();
    if (since > first) first = since;
    for (uint32_t i = first; i < logs; i++) {
        char line[REMOTE_LOG_LEN];
        uint32_t t;
        if (!remote_get_log(i, line, sizeof line, &t)) continue;
        add("%s{\"t\":%lu,\"m\":", i > first ? "," : "", (unsigned long)t);
        add_str(line);
        add("}");
    }
    add("],\"logseq\":%lu}", (unsigned long)logs);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, s_json, s_n < sizeof s_json ? s_n : sizeof s_json - 1);
}

static esp_err_t read_body(httpd_req_t *req, char *buf, size_t len)
{
    if (req->content_len >= len) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Too long");
        return ESP_FAIL;
    }
    size_t got = 0;
    while (got < req->content_len) {
        int r = httpd_req_recv(req, buf + got, req->content_len - got);
        if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
        if (r <= 0) return ESP_FAIL;
        got += r;
    }
    buf[got] = 0;
    return ESP_OK;
}

// Body: the same text you'd type on serial ("t", "r" or grams).
static esp_err_t post_cmd(httpd_req_t *req)
{
    char cmd[REMOTE_CMD_LEN];
    if (read_body(req, cmd, sizeof cmd) != ESP_OK) return ESP_FAIL;
    if (!remote_send_command(cmd)) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Scale is busy, try again");
        return ESP_FAIL;
    }
    return httpd_resp_sendstr(req, "ok");
}

// Body: "name\npassword". An empty name forgets the home network.
static esp_err_t post_wifi(httpd_req_t *req)
{
    char body[128];
    if (read_body(req, body, sizeof body) != ESP_OK) return ESP_FAIL;
    char *pass = strchr(body, '\n');
    if (pass) *pass++ = 0;
    else pass = "";
    if (strlen(body) > 32 || strlen(pass) > 63) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Name or password too long");
        return ESP_FAIL;
    }

    wifi_config_t cfg = {0};
    memcpy(cfg.sta.ssid, body, strlen(body));
    strlcpy((char *)cfg.sta.password, pass, sizeof cfg.sta.password);
    s_sta_state = "off";  // so the disconnect below doesn't trigger a retry
    esp_wifi_disconnect();
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
    if (body[0]) remote_log("Joining Wi-Fi \"%s\"...", body);
    else remote_log("Home Wi-Fi forgotten");
    sta_connect_saved();
    return httpd_resp_sendstr(req, "ok");
}

// Body: the new name. Letters, digits, spaces and dashes, up to 32 characters.
static esp_err_t post_name(httpd_req_t *req)
{
    char name[64];
    if (read_body(req, name, sizeof name) != ESP_OK) return ESP_FAIL;
    size_t len = strlen(name);
    while (len && name[len - 1] == ' ') name[--len] = 0;
    char *start = name;
    while (*start == ' ') start++;
    bool ok = *start && strlen(start) <= 32;
    for (const char *p = start; ok && *p; p++)
        ok = (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == ' ' || *p == '-';
    if (!ok) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Use 1 to 32 letters, digits, spaces or dashes");
        return ESP_FAIL;
    }

    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, "name", start);
        nvs_commit(h);
        nvs_close(h);
    }
    strlcpy(s_name, start, sizeof s_name);
    make_host(s_name, s_host, sizeof s_host);
    mdns_hostname_set(s_host);                   // the new .local address works at once
    esp_netif_set_hostname(s_sta_netif, s_host); // the router sees it after the next reconnect
    remote_log(">>> Name set to \"%s\": open http://%s.local", s_name, s_host);
    return httpd_resp_sendstr(req, "ok");
}

// Body: the raw .bin from build/hx711_scale.bin. Written to the spare OTA slot, then the board restarts into it.
static esp_err_t post_ota(httpd_req_t *req)
{
    static char buf[4096];
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part || req->content_len == 0 || req->content_len > part->size) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Missing firmware file, or file too big");
        return ESP_FAIL;
    }
    esp_ota_handle_t ota;
    if (esp_ota_begin(part, OTA_WITH_SEQUENTIAL_WRITES, &ota) != ESP_OK) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Could not start the update");
        return ESP_FAIL;
    }
    remote_log("Firmware update started (%u KB)", (unsigned)(req->content_len / 1024));

    size_t left = req->content_len;
    while (left > 0) {
        int r = httpd_req_recv(req, buf, left < sizeof buf ? left : sizeof buf);
        if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
        if (r <= 0) {
            esp_ota_abort(ota);
            remote_log("!!! Firmware upload interrupted. Still running the old firmware.");
            return ESP_FAIL;
        }
        if (esp_ota_write(ota, buf, r) != ESP_OK) {
            esp_ota_abort(ota);
            remote_log("!!! That file isn't ESP32 firmware. Still running the old firmware.");
            httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Not an ESP32 firmware file");
            return ESP_FAIL;
        }
        left -= r;
    }
    if (esp_ota_end(ota) != ESP_OK || esp_ota_set_boot_partition(part) != ESP_OK) {
        remote_log("!!! Firmware file is damaged. Still running the old firmware.");
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Firmware file is damaged");
        return ESP_FAIL;
    }
    remote_log(">>> Firmware updated. Restarting...");
    httpd_resp_sendstr(req, "ok");
    vTaskDelay(pdMS_TO_TICKS(500));  // let the reply go out
    esp_restart();
    return ESP_OK;
}

void remote_start(void)
{
    load_name();
    wifi_start();

    // Let the processor slow down and sleep whenever nothing is running. Wi-Fi wakes it for router beacons.
    esp_pm_config_t pm = { .max_freq_mhz = 160, .min_freq_mhz = 40, .light_sleep_enable = true };
    if (esp_pm_configure(&pm) != ESP_OK) remote_log("!!! Power saving could not be turned on");

    if (mdns_init() == ESP_OK) {
        mdns_hostname_set(s_host);
        mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    }

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.lru_purge_enable = true;
    httpd_handle_t srv;
    if (httpd_start(&srv, &cfg) != ESP_OK) {
        remote_log("!!! Web server failed to start");
        return;
    }
    const httpd_uri_t uris[] = {
        { .uri = "/",            .method = HTTP_GET,  .handler = get_index },
        { .uri = "/api/status",  .method = HTTP_GET,  .handler = get_status },
        { .uri = "/api/weight",  .method = HTTP_GET,  .handler = get_weight },
        { .uri = "/api/cmd",     .method = HTTP_POST, .handler = post_cmd },
        { .uri = "/api/wifi",    .method = HTTP_POST, .handler = post_wifi },
        { .uri = "/api/name",    .method = HTTP_POST, .handler = post_name },
        { .uri = "/api/ota",     .method = HTTP_POST, .handler = post_ota },
    };
    for (size_t i = 0; i < sizeof uris / sizeof uris[0]; i++)
        httpd_register_uri_handler(srv, &uris[i]);

    // A new firmware counts as good once its web page is up, so it can be replaced over Wi-Fi.
    // If it crashes before getting here, the bootloader goes back to the previous one.
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_ota_mark_app_valid_cancel_rollback();
        remote_log(">>> New firmware is running and marked good");
    }
}
