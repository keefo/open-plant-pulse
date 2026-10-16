// Bluetooth LE link: mac/scale.html in Chrome connects with Web Bluetooth.
//   status  (read, notify)  latest reading as JSON, sent when it changes
//   log     (notify)        one message line per notification: "<secs> <text>"
//   command (write)         same text as on serial: "t", "r" or grams
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_bt.h"
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "remote.h"

#define DEVICE_NAME "HX711-Scale"

// 7a1c000N-3c5e-4b8e-9f1a-2d6c8e4b7a11, bytes reversed as NimBLE wants them
#define SCALE_UUID(n) BLE_UUID128_INIT(0x11, 0x7a, 0x4b, 0x8e, 0x6c, 0x2d, 0x1a, 0x9f, \
                                       0x8e, 0x4b, 0x5e, 0x3c, (n), 0x00, 0x1c, 0x7a)
static const ble_uuid128_t SVC_UUID    = SCALE_UUID(0x01);
static const ble_uuid128_t STATUS_UUID = SCALE_UUID(0x02);
static const ble_uuid128_t LOG_UUID    = SCALE_UUID(0x03);
static const ble_uuid128_t CMD_UUID    = SCALE_UUID(0x04);

static uint16_t s_status_handle, s_log_handle;
static volatile uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static volatile bool s_status_sub, s_log_sub;
static volatile uint32_t s_log_next;  // next log line to send
static uint8_t s_addr_type;

static int on_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR && attr == s_status_handle) {
        char buf[200];
        int n = remote_format_status(buf, sizeof buf);
        return os_mbuf_append(ctxt->om, buf, n) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        char cmd[REMOTE_CMD_LEN];
        uint16_t len;
        if (ble_hs_mbuf_to_flat(ctxt->om, cmd, sizeof cmd - 1, &len) != 0)
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        cmd[len] = 0;
        return remote_send_command(cmd) ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def s_services[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            { .uuid = &STATUS_UUID.u, .access_cb = on_access,
              .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_status_handle },
            { .uuid = &LOG_UUID.u, .access_cb = on_access,
              .flags = BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_log_handle },
            { .uuid = &CMD_UUID.u, .access_cb = on_access,
              .flags = BLE_GATT_CHR_F_WRITE },
            { 0 },
        },
    },
    { 0 },
};

static void advertise(void);

static int on_gap(struct ble_gap_event *ev, void *arg)
{
    switch (ev->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (ev->connect.status == 0) {
            s_conn = ev->connect.conn_handle;
            remote_log("Bluetooth: connected");
        } else {
            advertise();
        }
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        s_status_sub = s_log_sub = false;
        remote_log("Bluetooth: disconnected");
        advertise();
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (ev->subscribe.attr_handle == s_status_handle) {
            s_status_sub = ev->subscribe.cur_notify;
        } else if (ev->subscribe.attr_handle == s_log_handle) {
            s_log_next = remote_log_oldest();  // replay the kept history to the new page
            s_log_sub = ev->subscribe.cur_notify;
        }
        break;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        break;
    }
    return 0;
}

static void advertise(void)
{
    // The service UUID goes in the advert so Chrome can filter on it; the name goes in the scan response.
    struct ble_hs_adv_fields adv = {
        .flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP,
        .uuids128 = &SVC_UUID,
        .num_uuids128 = 1,
        .uuids128_is_complete = 1,
    };
    struct ble_hs_adv_fields rsp = {
        .name = (const uint8_t *)DEVICE_NAME,
        .name_len = sizeof DEVICE_NAME - 1,
        .name_is_complete = 1,
    };
    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
    };
    ble_gap_adv_set_fields(&adv);
    ble_gap_adv_rsp_set_fields(&rsp);
    ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &params, on_gap, NULL);
}

static void on_sync(void)
{
    ble_hs_id_infer_auto(0, &s_addr_type);
    advertise();
    remote_log("Bluetooth is on as \"" DEVICE_NAME "\": open mac/scale.html in Chrome and press Bluetooth");
}

static void host_task(void *arg)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static bool notify(uint16_t handle, const char *data, int len)
{
    uint16_t conn = s_conn;
    if (conn == BLE_HS_CONN_HANDLE_NONE) return false;
    int max = ble_att_mtu(conn) - 3;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len < max ? len : max);
    return om && ble_gatts_notify_custom(conn, handle, om) == 0;  // the mbuf is consumed either way
}

// Pushes new readings and log lines to the connected page.
static void notify_task(void *arg)
{
    uint32_t sent_seq = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        if (s_conn == BLE_HS_CONN_HANDLE_NONE) continue;

        scale_reading_t r;
        uint32_t seq = remote_get_reading(&r);
        if (s_status_sub && seq != sent_seq) {
            char buf[200];
            if (notify(s_status_handle, buf, remote_format_status(buf, sizeof buf))) sent_seq = seq;
        }

        // A few lines per tick; on failure (no buffers) retry the same line next tick.
        for (int k = 0; k < 5 && s_log_sub && s_log_next < remote_log_count(); k++) {
            char line[REMOTE_LOG_LEN], msg[REMOTE_LOG_LEN + 12];
            uint32_t secs;
            if (!remote_get_log(s_log_next, line, sizeof line, &secs)) {
                s_log_next = remote_log_oldest();
                continue;
            }
            int n = snprintf(msg, sizeof msg, "%lu %s", (unsigned long)secs, line);
            if (!notify(s_log_handle, msg, n < (int)sizeof msg ? n : (int)sizeof msg - 1)) break;
            s_log_next++;
        }
    }
}

void remote_start(void)
{
    if (nimble_port_init() != ESP_OK) {
        remote_log("!!! Bluetooth failed to start");
        return;
    }
    // 0 dBm instead of the default +3: smaller current peaks, still fine across a room.
    esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_N0);

    ble_hs_cfg.sync_cb = on_sync;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_gatts_count_cfg(s_services);
    ble_gatts_add_svcs(s_services);
    ble_svc_gap_device_name_set(DEVICE_NAME);

    nimble_port_freertos_init(host_task);
    xTaskCreate(notify_task, "ble_notify", 4096, NULL, 5, NULL);
}
