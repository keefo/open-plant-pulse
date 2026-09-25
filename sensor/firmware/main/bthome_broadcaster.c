#include "bthome_broadcaster.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "device_config_protocol.h"
#include "device_config_store.h"
#include "device_identity.h"
#include "web_ui.h"
#include "wifi_credentials_store.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "force_report.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "store/config/ble_store_config.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#define BTHOME_UUID 0xfcd2
#define MAX_SERVICE_DATA_SIZE 22
#define MAX_LOCAL_NAME_SIZE 20
#define SHUTDOWN_GRACE_MS 2000
/* A person has to read a code off a label and type it. The bounded advertising
 * window exists to protect battery life, but enforcing it against a connection
 * that is in the middle of pairing just cuts the pairing off, and the phone or
 * laptop asks again, forever. Once someone is connected, give them a minute. */
#define PAIRING_WINDOW_MS 60000

static const ble_uuid128_t device_config_service_uuid = BLE_UUID128_INIT(
    0x00, 0x50, 0x50, 0x4f, 0x0f, 0x8d, 0x4a, 0x9a,
    0x28, 0x4c, 0x15, 0x1b, 0x01, 0x00, 0x51, 0x7f);
static const ble_uuid128_t device_config_characteristic_uuid = BLE_UUID128_INIT(
    0x00, 0x50, 0x50, 0x4f, 0x0f, 0x8d, 0x4a, 0x9a,
    0x28, 0x4c, 0x15, 0x1b, 0x02, 0x00, 0x51, 0x7f);

static const char *TAG = "bthome_adv";
static SemaphoreHandle_t advertisement_done;
static uint8_t own_address_type;
static uint8_t service_data_buffer[MAX_SERVICE_DATA_SIZE];
static size_t service_data_buffer_size;
static char local_name_buffer[MAX_LOCAL_NAME_SIZE];
static uint32_t advertisement_window_ms;
static uint32_t advertisement_interval_ms;
static int start_result;
static uint16_t connection_handle = BLE_HS_CONN_HANDLE_NONE;
/* The bond cannot be dropped while it is carrying the write that asked for it,
 * so the request is noted and honoured once the link is closed. */
static bool release_requested;

/* Marker, joined flag, and four address bytes. */
#define OPP_STATION_STATUS_SIZE 6
#define OPP_STATION_STATUS_MARKER 0xA1

static size_t append_station_status(uint8_t *output)
{
    const bool joined = web_ui_station_joined();
    output[0] = OPP_STATION_STATUS_MARKER;
    output[1] = joined ? 1U : 0U;
    output[2] = 0;
    output[3] = 0;
    output[4] = 0;
    output[5] = 0;
    if (joined) {
        unsigned int octets[4] = {0};
        if (sscanf(web_ui_station_address(), "%u.%u.%u.%u",
                   &octets[0], &octets[1], &octets[2], &octets[3]) == 4) {
            for (size_t index = 0; index < 4; ++index) {
                output[2 + index] = (uint8_t)octets[index];
            }
        }
    }
    return OPP_STATION_STATUS_SIZE;
}

static int device_config_access(uint16_t conn_handle, uint16_t attr_handle,
                                struct ble_gatt_access_ctxt *context, void *argument)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)argument;
    if (context->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        opp_report_ack_t ack;
        uint8_t ack_payload[OPP_REPORT_ACK_PAYLOAD_SIZE];
        if (opp_force_report_get_ack(&ack) &&
            opp_report_ack_encode(&ack, ack_payload) == sizeof(ack_payload)) {
            return os_mbuf_append(context->om, ack_payload, sizeof(ack_payload)) == 0
                       ? 0
                       : BLE_ATT_ERR_INSUFFICIENT_RES;
        }
        opp_device_config_t config;
        uint8_t payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE + OPP_STATION_STATUS_SIZE];
        opp_device_config_store_get(&config);
        size_t payload_size = opp_device_config_encode(&config, payload);
        if (payload_size == 0) {
            return BLE_ATT_ERR_ATTR_NOT_FOUND;
        }
        /* Append what only the sensor knows: whether it is on the household
         * network and at which address. The hub cannot see either over
         * Bluetooth, and a console it switched on stays "waiting" until told.
         * A suffix rather than a new payload version, so a hub that does not
         * know about it still decodes the configuration it asked for. */
        payload_size += append_station_status(&payload[payload_size]);
        return os_mbuf_append(context->om, payload, payload_size) == 0
                   ? 0
                   : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (context->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        const size_t payload_size = OS_MBUF_PKTLEN(context->om);
        if (payload_size > OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        uint8_t payload[OPP_DEVICE_CONFIG_PAYLOAD_MAX_SIZE];
        if (ble_hs_mbuf_to_flat(context->om, payload, payload_size, NULL) != 0) {
            return BLE_ATT_ERR_UNLIKELY;
        }
        if (opp_device_release_matches(payload, payload_size)) {
            /* Only the hub that owns this sensor is connected at all, and only
             * over an encrypted link, so reaching here is sufficient proof. */
            ESP_LOGW(TAG, "Released by its hub; forgetting configuration and network");
            web_ui_stop();
            opp_wifi_credentials_store_clear();
            opp_device_config_store_clear();
            opp_device_identity_set_onboarded(false);
            release_requested = true;
            return 0;
        }
        opp_report_ack_t ack;
        if (opp_report_ack_decode(payload, payload_size, &ack)) {
            return opp_force_report_acknowledge(&ack) == ESP_OK
                       ? 0
                       : BLE_ATT_ERR_UNLIKELY;
        }
        opp_device_config_t config;
        if (!opp_device_config_decode(payload, payload_size, &config)) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        return opp_device_config_store_apply(&config) == ESP_OK
                   ? 0
                   : BLE_ATT_ERR_UNLIKELY;
    }
    return BLE_ATT_ERR_REQ_NOT_SUPPORTED;
}

static const struct ble_gatt_svc_def device_config_services[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &device_config_service_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &device_config_characteristic_uuid.u,
                .access_cb = device_config_access,
                .flags = BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_READ |
                         BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_WRITE,
            },
            {0},
        },
    },
    {0},
};

/* One sensor belongs to one hub. A bond already present means this sensor has an
 * owner, so a second central is refused rather than quietly replacing it. */
static bool has_bond(void)
{
    int count = 0;
    if (ble_store_util_count(BLE_STORE_OBJ_TYPE_OUR_SEC, &count) != 0) {
        return false;
    }
    return count > 0;
}

static bool peer_is_bonded(uint16_t conn_handle)
{
    struct ble_gap_conn_desc description;
    if (ble_gap_conn_find(conn_handle, &description) != 0) {
        return false;
    }
    return description.sec_state.bonded;
}

/* Is this the hub that already owns the sensor?
 *
 * Checked against stored keys rather than the live connection, because the
 * answer is needed the moment a central connects, before any encryption exists
 * to make sec_state.bonded true. Without a code to prove who is connecting,
 * this is what keeps a claimed sensor claimed. */
static bool peer_is_owner(uint16_t conn_handle)
{
    struct ble_gap_conn_desc description;
    if (ble_gap_conn_find(conn_handle, &description) != 0) {
        return false;
    }
    if (description.sec_state.bonded) {
        return true;
    }
    struct ble_store_key_sec key = {0};
    struct ble_store_value_sec value;
    key.peer_addr = description.peer_id_addr;
    return ble_store_read_peer_sec(&key, &value) == 0;
}

static int gap_event(struct ble_gap_event *event, void *context)
{
    (void)context;
    if (event->type == BLE_GAP_EVENT_ADV_COMPLETE) {
        xSemaphoreGive(advertisement_done);
    } else if (event->type == BLE_GAP_EVENT_CONNECT) {
        if (event->connect.status == 0) {
            if (has_bond() && !peer_is_owner(event->connect.conn_handle)) {
                ESP_LOGW(TAG, "Refused a hub that does not own this sensor");
                ble_gap_terminate(event->connect.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                return 0;
            }
            connection_handle = event->connect.conn_handle;
            ESP_LOGI(TAG, "Hub connected for report acknowledgement or device configuration");
        } else {
            xSemaphoreGive(advertisement_done);
        }
    } else if (event->type == BLE_GAP_EVENT_DISCONNECT) {
        connection_handle = BLE_HS_CONN_HANDLE_NONE;
        if (release_requested) {
            release_requested = false;
            ble_store_clear();
            ESP_LOGW(TAG, "Bond cleared; this sensor belongs to nobody again");
        }
        xSemaphoreGive(advertisement_done);
    } else if (event->type == BLE_GAP_EVENT_REPEAT_PAIRING) {
        /* Re-pairing from the hub that already owns this sensor is allowed, so a
         * hub that lost its own keys can recover. A different central is not the
         * owner, and gets nothing. */
        struct ble_gap_conn_desc description;
        if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &description) != 0) {
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        if (!description.sec_state.bonded) {
            ESP_LOGW(TAG, "Refused re-pairing: this sensor already belongs to a hub");
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        ble_store_util_delete_peer(&description.peer_id_addr);
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    } else if (event->type == BLE_GAP_EVENT_ENC_CHANGE) {
        const bool encrypted = peer_is_bonded(event->enc_change.conn_handle);
        ESP_LOGI(TAG, "Link encryption %s", encrypted ? "established" : "not established");
        if (encrypted && !opp_device_identity_is_onboarded()) {
            opp_device_identity_set_onboarded(true);
            /* Report at once rather than at the next interval. Somebody is
             * watching the last step of setup, and half an hour of nothing is
             * indistinguishable from a failure. */
            uint32_t request_id;
            opp_force_report_request(&request_id);
        }
    }
    return 0;
}

static void on_reset(int reason)
{
    ESP_LOGE(TAG, "NimBLE host reset; reason=%d", reason);
    start_result = reason == 0 ? BLE_HS_EUNKNOWN : reason;
    xSemaphoreGive(advertisement_done);
}

static void on_sync(void)
{
    start_result = ble_hs_util_ensure_addr(0);
    if (start_result == 0) {
        start_result = ble_hs_id_infer_auto(0, &own_address_type);
    }

    struct ble_hs_adv_fields fields = {0};
    if (start_result == 0) {
        fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
        fields.svc_data_uuid16 = service_data_buffer;
        fields.svc_data_uuid16_len = service_data_buffer_size;
        start_result = ble_gap_adv_set_fields(&fields);
    }

    struct ble_hs_adv_fields response = {0};
    if (start_result == 0) {
        response.name = (const uint8_t *)local_name_buffer;
        response.name_len = strlen(local_name_buffer);
        response.name_is_complete = 1;
        start_result = ble_gap_adv_rsp_set_fields(&response);
    }

    if (start_result == 0) {
        struct ble_gap_adv_params parameters = {0};
        parameters.conn_mode = BLE_GAP_CONN_MODE_UND;
        parameters.disc_mode = BLE_GAP_DISC_MODE_GEN;
        parameters.itvl_min = BLE_GAP_ADV_ITVL_MS(advertisement_interval_ms);
        parameters.itvl_max = BLE_GAP_ADV_ITVL_MS(advertisement_interval_ms);
        start_result = ble_gap_adv_start(
            own_address_type,
            NULL,
            (int32_t)advertisement_window_ms,
            &parameters,
            gap_event,
            NULL);
    }
    if (start_result != 0) {
        xSemaphoreGive(advertisement_done);
    }
}

static void host_task(void *context)
{
    (void)context;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t opp_bthome_broadcast(const char *local_name,
                               const uint8_t *service_data,
                               size_t service_data_size,
                               uint32_t window_ms,
                               uint32_t interval_ms)
{
    if (local_name == NULL || service_data == NULL || service_data_size == 0 ||
        service_data_size + 2 > MAX_SERVICE_DATA_SIZE ||
        strlen(local_name) >= MAX_LOCAL_NAME_SIZE || window_ms == 0 || interval_ms == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    local_name_buffer[0] = '\0';
    strlcpy(local_name_buffer, local_name, sizeof(local_name_buffer));
    service_data_buffer[0] = (uint8_t)BTHOME_UUID;
    service_data_buffer[1] = (uint8_t)(BTHOME_UUID >> 8U);
    memcpy(service_data_buffer + 2, service_data, service_data_size);
    service_data_buffer_size = service_data_size + 2;
    advertisement_window_ms = window_ms;
    advertisement_interval_ms = interval_ms;
    start_result = 0;
    connection_handle = BLE_HS_CONN_HANDLE_NONE;

    advertisement_done = xSemaphoreCreateBinary();
    if (advertisement_done == NULL) {
        if (advertisement_done != NULL) {
            vSemaphoreDelete(advertisement_done);
        }
        return ESP_ERR_NO_MEM;
    }

    esp_err_t result = nimble_port_init();
    if (result == ESP_OK) {
        int gatt_result = ble_gatts_count_cfg(device_config_services);
        if (gatt_result == 0) {
            gatt_result = ble_gatts_add_svcs(device_config_services);
        }
        if (gatt_result != 0) {
            ESP_LOGE(TAG, "Could not register device configuration service; rc=%d", gatt_result);
            result = ESP_FAIL;
        }
    }
    if (result == ESP_OK) {
        ble_hs_cfg.reset_cb = on_reset;
        ble_hs_cfg.sync_cb = on_sync;
        /* LE Secure Connections without a passkey.
         *
         * The link is still encrypted, and the long term key is still derived by
         * ECDH rather than transmitted. What is given up is protection against
         * someone sitting in the middle of the one pairing exchange, which is
         * the price of an onboarding with nothing to type.
         *
         * Ownership is defended instead of adoption: anyone may claim a sensor
         * that belongs to nobody, and nobody may take one that is already
         * claimed. That lock is the refusal below, the hub's release, and the
         * physical reset, not the handshake. */
        ble_hs_cfg.sm_sc = 1;
        ble_hs_cfg.sm_bonding = 1;
        ble_hs_cfg.sm_mitm = 0;
        ble_hs_cfg.sm_io_cap = BLE_HS_IO_NO_INPUT_OUTPUT;
        ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
        ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
        ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
        nimble_port_freertos_init(host_task);
        bool window_expired = xSemaphoreTake(
                                  advertisement_done,
                                  pdMS_TO_TICKS(window_ms + SHUTDOWN_GRACE_MS)) != pdTRUE;
        if (window_expired && connection_handle != BLE_HS_CONN_HANDLE_NONE) {
            /* Someone is connected, so the window has done its job: it found a
             * hub. Wait for them to finish rather than hanging up on them. */
            ESP_LOGI(TAG, "Connected at the end of the window; allowing %d ms to pair",
                     PAIRING_WINDOW_MS);
            ble_gap_adv_stop();
            window_expired =
                xSemaphoreTake(advertisement_done, pdMS_TO_TICKS(PAIRING_WINDOW_MS)) != pdTRUE;
        }
        if (window_expired) {
            ESP_LOGE(TAG, "Advertising exceeded its %lu ms bounded window",
                     (unsigned long)window_ms);
            ble_gap_adv_stop();
            if (connection_handle != BLE_HS_CONN_HANDLE_NONE) {
                ble_gap_terminate(connection_handle, BLE_ERR_REM_USER_CONN_TERM);
            }
            result = ESP_ERR_TIMEOUT;
        } else if (start_result != 0) {
            ESP_LOGE(TAG, "Could not start BTHome advertising; rc=%d", start_result);
            result = ESP_FAIL;
        }

        int stop_result = nimble_port_stop();
        if (stop_result != 0 && result == ESP_OK) {
            result = ESP_FAIL;
        }
        esp_err_t deinit_result = nimble_port_deinit();
        if (deinit_result != ESP_OK && result == ESP_OK) {
            result = deinit_result;
        }
    }

    vSemaphoreDelete(advertisement_done);
    advertisement_done = NULL;
    return result;
}