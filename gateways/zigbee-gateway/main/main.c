/* A Zigbee gateway, and nothing else yet.
 *
 * This is the radio the hub does not have. The iMac speaks Wi-Fi and
 * Bluetooth and has no 802.15.4, so something has to form the Zigbee
 * network and, later, hand what it hears over USB. That is all this board
 * is for.
 *
 * At this stage it does one job: form a network and say so over the USB
 * serial log. Nothing has joined it and there is no bridge to the hub,
 * because neither is worth writing until the network provably exists.
 *
 * Written against esp-zigbee-lib 2.x, whose API differs from the 1.x one
 * most examples still show: signals arrive at handlers registered with
 * ezb_app_signal_add_handler rather than at one global function.
 */

#include <inttypes.h>
#include <string.h>
#include <stdbool.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_zigbee.h"
#include "cJSON.h"
#include "devices.h"
#include "ezbee/zha.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

static const char *TAG = "gateway";

/* How long the network stays open after it forms. Short, and shut after
 * that: a scan from this network found eight others nearby, so an open door
 * is an open door to all of them. The hub opens it again when pairing. */
#define JOIN_WINDOW_SECONDS 180

/* When the current join window closes, so the hub can show a countdown that
 * matches the gateway rather than one the page invented. */
static int64_t s_permit_until_us;

static void report_network(void)
{
    ezb_extaddr_t self;
    ezb_nwk_get_extended_address(&self);
    const uint8_t *address = (const uint8_t *)&self;
    ESP_LOGI(TAG, "network up: pan 0x%04x, channel %u, short 0x%04x",
             (unsigned)ezb_nwk_get_panid(), (unsigned)ezb_nwk_get_current_channel(),
             (unsigned)ezb_nwk_get_short_address());
    ESP_LOGI(TAG, "gateway %02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
             address[7], address[6], address[5], address[4],
             address[3], address[2], address[1], address[0]);
}

/* What the scale sends. Zigbee carries a packed report rather than the JSON
 * the Wi-Fi link uses, because a frame holds about 80 bytes and the JSON runs
 * nearer 400. The hub wants JSON, so it is rebuilt here on the way out. */
#define SCALE_CLUSTER  0xFC00
#define SCALE_ENDPOINT 10
#define SCALE_PROFILE  0x0104

#define FLAG_OK        (1u << 0)
#define FLAG_STABLE    (1u << 1)
#define FLAG_SATURATED (1u << 2)
#define FLAG_WIFI      (1u << 3)

typedef struct __attribute__((packed)) {
    uint32_t seq;
    float grams;
    float batt;
    int8_t batt_pct;   /* -1 when no divider is wired to measure it */
    uint8_t flags;
    uint8_t fw_major;  /* the running firmware, so an update can be */
    uint8_t fw_minor;  /* confirmed over Zigbee with Wi-Fi switched */
    uint8_t fw_patch;  /* off again, which is how the scale usually sits */
    uint8_t reset_reason;  /* esp_reset_reason(), so a reboot is visible */
    uint32_t uptime_s;     /* going backwards is the giveaway */
    uint32_t interval_s;
} scale_report_t;

static void register_endpoint(void)
{
    // A gateway endpoint rather than a bare one: commissioning leans on the
    // standard clusters, and an endpoint carrying none never gets through it.
    ezb_zha_custom_gateway_config_t cfg = {
        .basic_cfg = {.zcl_version = 3, .power_source = 1 /* mains */},
        .identify_cfg = {0},
    };
    ezb_af_ep_desc_t ep_desc = ezb_zha_create_custom_gateway(SCALE_ENDPOINT, &cfg);
    ezb_af_device_desc_t dev = ezb_af_create_device_desc();
    ezb_err_t a = ezb_af_device_add_endpoint_desc(dev, ep_desc);
    ezb_err_t r = ezb_af_device_desc_register(dev);
    ESP_LOGI(TAG, "endpoint %d add=%d register=%d", SCALE_ENDPOINT, (int)a, (int)r);
}

static bool on_data(const ezb_apsde_data_ind_t *ind)
{
    if (ind->cluster_id != SCALE_CLUSTER) return false;

    uint16_t from = ind->src_address.u.short_addr;
    char eui[EUI_TEXT_LEN];
    if (!devices_eui_for(from, eui)) {
        // A frame from a device the table has not placed yet: resolve it now
        // rather than reporting something nobody can address a reply to.
        devices_seen_short(from);
        if (!devices_eui_for(from, eui)) {
            // Said out loud, not only to a console nobody is reading.
            // Dropping these quietly is how a healthy scale reported
            // into nothing for twelve hours while both ends looked
            // fine: it got its acknowledgement, so it never retried.
            ESP_LOGW(TAG, "frame from 0x%04x, whose EUI-64 is unknown", (unsigned)from);
            printf("{\"t\":\"unplaced\",\"short\":\"0x%04x\",\"bytes\":%u}\n",
                   (unsigned)from, (unsigned)ind->asdu_length);
            return false;
        }
    }
    devices_seen_short(from);

    if (ind->asdu_length != sizeof(scale_report_t)) {
        // Not a reading. Devices also send short notes about themselves --
        // the name they answer to on Wi-Fi, for one. Passing them through
        // unread keeps this gateway out of the business of knowing what
        // each kind of device has to say.
        char note[64];
        size_t n = ind->asdu_length < sizeof note - 1 ? ind->asdu_length : sizeof note - 1;
        memcpy(note, ind->asdu, n);
        note[n] = '\0';
        for (size_t i = 0; i < n; i++)
            if (note[i] == '"' || note[i] == '\\' || (unsigned char)note[i] < 0x20) note[i] = '.';
        printf("{\"t\":\"info\",\"eui\":\"%s\",\"text\":\"%s\"}\n", eui, note);
        return true;
    }

    scale_report_t r;
    memcpy(&r, ind->asdu, sizeof r);
    /* One line of JSON per reading, which is what the hub reads off USB.
     * lqi and rssi are the stack's, not the scale's: they say how well the
     * two radios hear each other. */
    printf("{\"t\":\"reading\",\"eui\":\"%s\",\"short\":\"0x%04x\",\"kind\":\"scale\","
           "\"seq\":%" PRIu32 ",\"grams\":%.1f,\"batt\":%.2f,\"batt_pct\":%d,"
           "\"fw\":\"%u.%u.%u\",\"reset\":%u,\"uptime\":%" PRIu32 ","
           "\"ok\":%s,\"stable\":%s,\"saturated\":%s,\"wifi\":%s,"
           "\"interval\":%" PRIu32 ",\"lqi\":%u,\"rssi\":%d}\n",
           eui, (unsigned)from, r.seq, r.grams, r.batt, (int)r.batt_pct,
           (unsigned)r.fw_major, (unsigned)r.fw_minor, (unsigned)r.fw_patch,
           (unsigned)r.reset_reason, r.uptime_s,
           (r.flags & FLAG_OK) ? "true" : "false",
           (r.flags & FLAG_STABLE) ? "true" : "false",
           (r.flags & FLAG_SATURATED) ? "true" : "false",
           (r.flags & FLAG_WIFI) ? "true" : "false",
           r.interval_s, (unsigned)ind->lqi, (int)ind->rssi);
    return true;
}

static void open_for_joining(const char *when)
{
    ezb_err_t err = ezb_bdb_open_network(JOIN_WINDOW_SECONDS);
    if (err == 0) {
        s_permit_until_us = esp_timer_get_time() + (int64_t)JOIN_WINDOW_SECONDS * 1000000;
        ESP_LOGI(TAG, "open to joining (%s)", when);
    } else {
        ESP_LOGE(TAG, "could not open for joining (%s): ezb error %d", when, (int)err);
    }
}

/* Returning false leaves the signal to the stack's own handling, which is
 * what everything not named here wants. */
static bool on_signal(const ezb_app_signal_t *signal)
{
    switch (ezb_app_signal_get_type(signal)) {
    case EZB_BDB_SIGNAL_FORMATION:
        report_network();
        open_for_joining("after forming");
        return false;

    case EZB_BDB_SIGNAL_DEVICE_REBOOT:
        // This arrives after initialisation whether or not anything was
        // saved. If the node is still factory new then no network has really
        // been formed yet, whatever address it is reporting, so form one
        // properly rather than opening a network that cannot accept joins.
        if (ezb_bdb_is_factory_new()) {
            ESP_LOGI(TAG, "still factory new; forming a network properly");
            ezb_err_t e = ezb_bdb_start_top_level_commissioning(EZB_BDB_MODE_NETWORK_FORMATION);
            if (e != 0) ESP_LOGE(TAG, "formation refused: ezb error %d", (int)e);
            return false;
        }
        report_network();
        open_for_joining("after reboot");
        return false;

    case EZB_NWK_SIGNAL_PERMIT_JOIN_STATUS:
        ESP_LOGI(TAG, "permit-join status changed");
        return false;

    case EZB_ZDO_SIGNAL_DEVICE_ANNCE: {
        const ezb_zdo_signal_device_annce_params_t *a = ezb_app_signal_get_params(signal);
        if (a) {
            devices_seen(a->short_addr, &a->device_addr);
            char eui[EUI_TEXT_LEN];
            if (devices_eui_for(a->short_addr, eui))
                printf("{\"t\":\"joined\",\"eui\":\"%s\",\"short\":\"0x%04x\"}\n",
                       eui, (unsigned)a->short_addr);
        }
        ESP_LOGI(TAG, "a device joined");
    }

        return false;

    case EZB_ZDO_SIGNAL_SKIP_STARTUP:
        // The stack is up but has deliberately not commissioned itself,
        // which is what starting without autostart buys: the decision to
        // form a network is taken here rather than behind our back.
        ESP_LOGI(TAG, "stack ready; commissioning");
        ezb_bdb_start_top_level_commissioning(EZB_BDB_MODE_INITIALIZATION);
        return false;

    case EZB_BDB_SIGNAL_DEVICE_FIRST_START:
        ESP_LOGI(TAG, "no network saved; forming one");
        ezb_bdb_start_top_level_commissioning(EZB_BDB_MODE_NETWORK_FORMATION);
        return false;

    default:
        ESP_LOGI(TAG, "signal: %s",
                 ezb_app_signal_to_string(ezb_app_signal_get_type(signal)));
        return false;
    }
}

/* The hub speaks JSON lines, one per message, addressed by EUI-64:
 *
 *   {"t":"cmd","id":7,"eui":"acebe6fffe0d7f18","cmd":"w 0"}
 *   {"t":"list"}
 *   {"t":"permit","seconds":180}
 *
 * Every command carries an id, and its outcome comes back as an ack carrying
 * the same id. That pairing is the point: a frame queued for a sleepy device
 * that never collects it fails with APS status 0xa7, and without an explicit
 * acknowledgement that is indistinguishable from success.
 */
static int s_pending_id = -1;       // id of the command awaiting confirmation

static void emit_ack(int id, unsigned status, const char *note)
{
    printf("{\"t\":\"ack\",\"id\":%d,\"status\":%u,\"note\":\"%s\"}\n",
           id, status, note ? note : "");
}

/* The stack's own neighbour table, which is a different thing from the
 * address table this gateway keeps.
 *
 * devices.c is a cache: EUI-64 to short address, rebuilt from traffic, and
 * an implementation detail nobody should be editing. This is the radio's
 * view -- who is actually reachable, how well, and as what. It is the thing
 * that answers "why is this device flaky", so it is reported and never
 * written to.
 */
static void emit_neighbours(void)
{
    ezb_nwk_info_iterator_t iterator = EZB_NWK_INFO_ITERATOR_INIT;
    ezb_nwk_neighbor_info_t nbr;
    bool first = true;
    printf("{\"t\":\"neighbours\",\"list\":[");
    while (ezb_nwk_get_next_neighbor(&iterator, &nbr) == 0) {
        const uint8_t *b = (const uint8_t *)&nbr.ieee_addr;
        printf("%s{\"eui\":\"%02x%02x%02x%02x%02x%02x%02x%02x\",\"short\":\"0x%04x\","
               "\"type\":%u,\"depth\":%u,\"rx_on_idle\":%u,\"relationship\":%u,"
               "\"lqi\":%u,\"rssi\":%d,\"age\":%u}",
               first ? "" : ",",
               b[7], b[6], b[5], b[4], b[3], b[2], b[1], b[0],
               (unsigned)nbr.short_addr, (unsigned)nbr.device_type, (unsigned)nbr.depth,
               (unsigned)nbr.rx_on_when_idle, (unsigned)nbr.relationship,
               (unsigned)nbr.lqi, (int)nbr.rssi, (unsigned)nbr.age);
        first = false;
    }
    printf("]}\n");
}

static void emit_devices(void)
{
    char eui[EUI_TEXT_LEN];
    uint16_t short_addr;
    int64_t seen_us;
    int64_t now = esp_timer_get_time();
    int64_t permit_left = s_permit_until_us - now;
    if (permit_left < 0) permit_left = 0;
    /* Uptime and why it last started, in the reply the hub already
     * polls for. A coordinator that restarts loses the children it was
     * relaying for, so its restarts are worth noticing -- and until it
     * said how long it had been up, nothing could tell a gateway that
     * had been running for a week from one that rebooted a minute ago.
     */
    printf("{\"t\":\"devices\",\"pan\":\"0x%04x\",\"channel\":%u,"
           "\"permit_s\":%ld,\"uptime_s\":%lld,\"reset\":%u,\"list\":[",
           (unsigned)ezb_nwk_get_panid(), (unsigned)ezb_nwk_get_current_channel(),
           (long)(permit_left / 1000000),
           (long long)(esp_timer_get_time() / 1000000),
           (unsigned)esp_reset_reason());
    for (size_t i = 0; devices_at(i, eui, &short_addr, &seen_us); i++)
        printf("%s{\"eui\":\"%s\",\"short\":\"0x%04x\",\"last_seen_s\":%ld}",
               i ? "," : "", eui, (unsigned)short_addr,
               (long)((now - seen_us) / 1000000));
    printf("]}\n");
}

/* Sends to one named device. An unknown EUI-64 is refused rather than
 * delivered to whoever happens to be on the network. */
static void send_to(const char *eui, const char *cmd, int id)
{
    uint16_t dst = devices_short_for(eui);
    if (dst == 0xFFFE) {
        emit_ack(id, 0xFF, "unknown device");
        ESP_LOGW(TAG, "no device %s; \"%s\" not sent", eui, cmd);
        return;
    }
    ezb_apsde_data_req_t req = {
        .dst_address = EZB_ADDRESS_SHORT(dst),
        .src_endpoint = SCALE_ENDPOINT,
        .dst_endpoint = SCALE_ENDPOINT,
        .cluster_id = SCALE_CLUSTER,
        .profile_id = SCALE_PROFILE,
        .radius = 0,
        .tx_options = EZB_APSDE_TX_OPT_ACK_TX,
        .asdu_length = (uint16_t)strlen(cmd),
        .asdu = (uint8_t *)cmd,
    };
    s_pending_id = id;
    ezb_err_t e = ezb_apsde_data_request(&req);
    if (e != 0) {
        s_pending_id = -1;
        emit_ack(id, 0xFE, "refused by the stack");
        return;
    }
    ESP_LOGI(TAG, "sent \"%s\" to %s (0x%04x)", cmd, eui, (unsigned)dst);
}

/* Whether a command reached the device, rather than merely leaving here. */
static void on_confirm(const ezb_apsde_data_confirm_t *cnf)
{
    if (cnf->cluster_id != SCALE_CLUSTER) return;
    if (s_pending_id < 0) return;
    emit_ack(s_pending_id, cnf->status,
             cnf->status == 0 ? "delivered" : "not acknowledged");
    ESP_LOGI(TAG, "delivery to 0x%04x: %s (status %u)",
             (unsigned)cnf->dst_address.u.short_addr,
             cnf->status == 0 ? "acknowledged" : "FAILED", (unsigned)cnf->status);
    s_pending_id = -1;
}

static void handle_message(char *text)
{
    cJSON *msg = cJSON_Parse(text);
    if (!msg) {
        // Not JSON. Left deliberately unhandled rather than guessed at: the
        // bare text commands this replaced addressed whichever device had
        // reported last, which is wrong as soon as there are two.
        ESP_LOGW(TAG, "ignoring a line that is not JSON: %.40s", text);
        return;
    }
    const cJSON *type = cJSON_GetObjectItem(msg, "t");
    const char *kind = cJSON_IsString(type) ? type->valuestring : "";
    const cJSON *id_item = cJSON_GetObjectItem(msg, "id");
    int id = cJSON_IsNumber(id_item) ? id_item->valueint : -1;

    if (!strcmp(kind, "list")) {
        emit_devices();
    } else if (!strcmp(kind, "neighbours")) {
        emit_neighbours();
    } else if (!strcmp(kind, "permit")) {
        const cJSON *secs = cJSON_GetObjectItem(msg, "seconds");
        uint8_t duration = cJSON_IsNumber(secs) ? (uint8_t)secs->valueint : 0;
        ezb_bdb_open_network(duration);
        s_permit_until_us = duration
            ? esp_timer_get_time() + (int64_t)duration * 1000000
            : 0;
        printf("{\"t\":\"permit\",\"seconds\":%u}\n", (unsigned)duration);
        ESP_LOGI(TAG, "joining open for %u s", (unsigned)duration);
    } else if (!strcmp(kind, "cmd")) {
        const cJSON *eui = cJSON_GetObjectItem(msg, "eui");
        const cJSON *cmd = cJSON_GetObjectItem(msg, "cmd");
        if (!cJSON_IsString(eui) || !cJSON_IsString(cmd))
            emit_ack(id, 0xFD, "cmd needs eui and cmd");
        else if (esp_zigbee_lock_acquire(pdMS_TO_TICKS(1000))) {
            send_to(eui->valuestring, cmd->valuestring, id);
            esp_zigbee_lock_release();
        } else {
            emit_ack(id, 0xFC, "stack busy");
        }
    } else if (!strcmp(kind, "forget") || !strcmp(kind, "remove")) {
        const cJSON *eui = cJSON_GetObjectItem(msg, "eui");
        if (!cJSON_IsString(eui)) {
            emit_ack(id, 0xFD, "needs eui");
        } else {
            // Two different things. "remove" asks the device to leave, so it
            // does not simply rejoin and reappear; "forget" only drops it
            // here, for a device that is dead or out of range and cannot be
            // told anything at all.
            if (!strcmp(kind, "remove")) {
                uint16_t dst = devices_short_for(eui->valuestring);
                ezb_extaddr_t addr;
                if (dst != 0xFFFE && devices_address_for(eui->valuestring, &addr) &&
                    esp_zigbee_lock_acquire(pdMS_TO_TICKS(1000))) {
                    ezb_zdo_nwk_mgmt_leave_req_t leave = {
                        .dst_nwk_addr = dst,
                        .field = {
                            .device_addr = addr,
                            .remove_children = false,
                            // Told to go and stay gone. Asking it to rejoin
                            // would undo the removal at the next beacon.
                            .rejoin = false,
                        },
                    };
                    ezb_zdo_nwk_mgmt_leave_req(&leave);
                    esp_zigbee_lock_release();
                }
            }
            bool had = devices_forget(eui->valuestring);
            printf("{\"t\":\"%s\",\"eui\":\"%s\",\"known\":%s}\n",
                   kind, eui->valuestring, had ? "true" : "false");
            emit_ack(id, 0, had ? "dropped" : "was not listed");
        }
    } else {
        ESP_LOGW(TAG, "unknown message type \"%s\"", kind);
    }
    cJSON_Delete(msg);
}

static void console_task(void *argument)
{
    (void)argument;
    static char line[256];
    size_t n = 0;
    for (;;) {
        int ch = getchar();
        if (ch == EOF) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            if (!n) continue;
            line[n] = '\0';
            n = 0;
            handle_message(line);
        } else if (n < sizeof line - 1) {
            line[n++] = (char)ch;
        }
    }
}

static void zigbee_task(void *argument)
{
    (void)argument;
    const esp_zigbee_config_t config = {
        .device_config = {
            .device_type = EZB_NWK_DEVICE_TYPE_COORDINATOR,
            .install_code_policy = false,
            .zczr_config = {.max_children = 16},
        },
        .platform_config = {
            // Left unset: the stack then keeps its datasets in the
            // ordinary nvs partition, which is where version 2 puts them.
            // The radio is on this chip. The other mode drives a separate
            // radio over UART, which is what a dongle does for a PC.
            .radio_config = {.radio_mode = ESP_ZIGBEE_RADIO_MODE_NATIVE},
        },
    };
    ESP_ERROR_CHECK(esp_zigbee_init(&config));
    // A centralised trust centre, which is what an ordinary end device looks
    // for. Both ends have to agree or no join is possible.
    ezb_aps_secur_enable_distributed_security(false);
    // ezb_err_t is a plain int whose success value is zero.
    ESP_ERROR_CHECK(ezb_app_signal_add_handler(on_signal) == 0 ? ESP_OK : ESP_FAIL);
    ezb_apsde_data_indication_handler_register(on_data);
    ezb_apsde_data_confirm_handler_register(on_confirm);
    register_endpoint();
    ESP_ERROR_CHECK(esp_zigbee_start(false));
    esp_err_t ended = esp_zigbee_launch_mainloop();
    ESP_LOGE(TAG, "mainloop returned: %s", esp_err_to_name(ended));
    vTaskDelete(NULL);
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    // Before the radio starts, so a device reporting immediately can
    // still be placed.
    devices_load();
    xTaskCreate(zigbee_task, "zigbee", 8192, NULL, 5, NULL);
    xTaskCreate(console_task, "console", 4096, NULL, 4, NULL);
}
