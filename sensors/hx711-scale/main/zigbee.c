// Zigbee link: the scale joins the coordinator's network and sends each reading to it.
// This runs *alongside* Wi-Fi rather than instead of it, so the scale stays reachable for
// over-the-air updates while the Zigbee side is being proven. The two share one 2.4 GHz
// radio and take turns; see CONFIG_ESP_COEX_SW_COEXIST_ENABLE.
//
// A reading is sent as a packed binary report, not as the JSON the other links use: a
// Zigbee frame carries about 80 bytes and remote_format_status writes nearer 400. The
// gateway expands it back into JSON on its way to the hub.
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_coexist.h"
#include <stdio.h>
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_app_desc.h"
#include "esp_zigbee.h"
#include "ezbee/zha.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "remote.h"

#define SCALE_ENDPOINT   10
#define SCALE_CLUSTER    0xFC00  // manufacturer-specific: our own reading, not a ZCL one
#define SCALE_PROFILE    0x0104  // Home Automation
#define COORDINATOR_ADDR 0x0000

#define FLAG_OK        (1u << 0)
#define FLAG_STABLE    (1u << 1)
#define FLAG_SATURATED (1u << 2)
#define FLAG_WIFI      (1u << 3)  /* Wi-Fi and the web server are running */

typedef struct __attribute__((packed)) {
    uint32_t seq;
    float grams;
    float batt;
    // Charge left, or -1 when the battery divider is not wired. Without
    // this the hub cannot tell a flat cell from an unwired one: both are a
    // small voltage, and the unwired case is the common one because the
    // divider is optional. The Wi-Fi status has always carried it.
    int8_t batt_pct;
    uint8_t flags;
    // The running firmware version, three bytes rather than a string.
    //
    // The hub judges an over-the-air install by the version the device
    // reports back, and with Wi-Fi normally off Zigbee is the only way
    // it can hear one. Three bytes against roughly forty spare in a
    // frame is nothing, and it means an update can be confirmed with
    // the second radio already switched off again.
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    // Why it last started, and how long it has been up.
    //
    // Four bytes that mean the hub notices a reboot without anybody
    // plugging in a cable. A scale that quietly panics and recovers
    // looks identical to one that never faltered, until the uptime is
    // seen to have gone backwards.
    uint8_t reset_reason;
    uint32_t uptime_s;
    // The rate the scale is actually sampling at. Sent rather than assumed,
    // so a page cannot show one rate while the device runs another.
    uint32_t interval_s;
} scale_report_t;

static bool s_joined;
// Read once from the app descriptor rather than parsed per report.
static uint8_t s_fw[3];
static volatile bool s_report_now;  // a command asked for an immediate report

// How often readings go out. Zigbee follows the sampling interval, so there
// is one rate to set; this floor exists only because relaying continuous
// sampling (ten a second) takes the shared radio away from Wi-Fi. It is kept
// here rather than compiled in, so it can be raised, lowered, or turned off
// entirely by whoever decides that trade-off.
static uint32_t s_floor_s = CONFIG_SCALE_ZIGBEE_MIN_GAP_SECONDS;

uint32_t zigbee_report_every(void)
{
    uint32_t every = remote_interval_seconds();
    if (every == 0) every = s_floor_s;   // continuous sampling: use the floor
    if (s_floor_s && every < s_floor_s) every = s_floor_s;
    return every;
}

uint32_t zigbee_report_floor(void)
{
    return s_floor_s;
}

void zigbee_set_report_floor(uint32_t seconds)
{
    s_floor_s = seconds;
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) != ESP_OK) return;
    esp_err_t err = nvs_set_u32(h, "zb_floor", seconds);
    if (err == ESP_OK) err = nvs_commit(h);
    if (err != ESP_OK)
        remote_log("!!! Zigbee report floor not saved: %s", esp_err_to_name(err));
    nvs_close(h);
    if (seconds == 0)
        remote_log(">>> Zigbee reports follow the sampling rate exactly. Relaying continuous "
                   "sampling can cost Wi-Fi its share of the radio.");
    else
        remote_log(">>> Zigbee reports no more often than every %lu s", (unsigned long)seconds);
}

static void load_floor(void)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READONLY, &h) != ESP_OK) return;
    nvs_get_u32(h, "zb_floor", &s_floor_s);
    nvs_close(h);
}

// Kept for the status page: what has happened on this link since boot.
static uint32_t s_joins;        // times it has joined or rejoined
static uint32_t s_searches;     // steering attempts
static uint32_t s_reports;      // readings handed to the stack
static uint32_t s_report_fails; // ...and those the stack would not take
static uint32_t s_commands;     // commands arriving from the coordinator
// Readings sent since one was last acknowledged. The link is judged on
// arrivals, not on the stack still believing it is joined.
static uint32_t s_unacked;
// The name this scale answers to on Wi-Fi, so a hub reached over Zigbee can
// offer a way back to the scale's own page. Too long to fit in a reading and
// too rarely useful to repeat in every one, so it is sent after joining and
// then occasionally: whoever is listening may have restarted since, and a
// note sent once is a note the hub loses the first time it is restarted.
static bool s_announce_host;
static int64_t s_host_sent_us;
#define HOST_REPEAT_SECONDS 600
#define UNACKED_BEFORE_REJOIN 6
// How long a report may go unanswered before it counts as lost.
#define CONFIRM_WAIT_SECONDS 30
// How long a device that has a network saved gets to rejoin it before that
// saved network is written off. Rejoining is the cheap, unattended path and
// starting over is the expensive one that needs somebody at the hub, so the
// cheap one is given plenty of room.
#define REJOIN_GRACE_SECONDS 120
// How long the stack gets to put the link back together on its own before
// the credentials are thrown away. Generous on purpose: a secured rejoin
// costs nothing and may be waiting on a coordinator that is being flashed,
// while a factory reset needs a person.
#define GIVE_UP_AFTER_SECONDS (30 * 60)
static int64_t  s_last_report_us;
static int64_t  s_joined_at_us;
static int64_t  s_trouble_since_us;
// A report is out and nothing has come back for it yet.
static volatile bool s_awaiting_confirm;
static uint8_t  s_confirms_logged;
static int64_t  s_sent_at_us;
static int64_t  s_rejoin_deadline_us;
static char     s_last_trouble[64];

static void steering_task(void *arg);
static void start_steering(const char *why);

/* A report that was never confirmed is a report that failed.
 *
 * This counted only confirms that arrived *saying* they had failed, so
 * a coordinator that simply stops listening -- the actual failure, three
 * times over -- produced no confirms at all and incremented nothing. The
 * scale reported "0 failures" while transmitting into an empty room for
 * twelve hours, and its own recovery never fired because by its own
 * count there was nothing wrong.
 *
 * So silence is now counted. A report is outstanding from the moment it
 * goes out, and if nothing comes back before the next one is due it is
 * written off exactly as a failed confirm would be.
 */
static void note_unacked(void);

/* Whether a reading actually arrived anywhere. */
static void on_confirm(const ezb_apsde_data_confirm_t *cnf)
{
    /* Every confirm counts, whatever cluster it names.
     *
     * This filtered on the scale cluster, as the indication handler
     * does -- and a confirm does not necessarily carry the cluster the
     * way an indication does. If it does not, every confirm was being
     * discarded here, which is why this device has never once logged
     * "heard again" and why its unacked count sat at zero through
     * three days of talking to nobody. The first few are logged so the
     * next person does not have to infer it. */
    if (s_confirms_logged < 3) {
        s_confirms_logged++;
        remote_log("Zigbee: confirm cluster 0x%04x status %u",
                   (unsigned)cnf->cluster_id, (unsigned)cnf->status);
    }
    s_awaiting_confirm = false;
    if (cnf->status == 0) {
        if (s_unacked)
            remote_log("Zigbee: heard again after %lu unacknowledged",
                       (unsigned long)s_unacked);
        s_unacked = 0;
        return;
    }
    note_unacked();
}

static void note_unacked(void)
{
    if (++s_unacked == UNACKED_BEFORE_REJOIN) {
        // Noticed, and nothing more. Being joined is not the same as being
        // heard -- a coordinator that restarts forgets its children while
        // this end still believes all is well -- but the cure used to be a
        // factory reset, and that is the one action that makes the problem
        // permanent: it throws away the network key, so the device can no
        // longer rejoin and has to be let in by hand. On a scale sitting on
        // a shelf that means it stays off the network until somebody
        // notices and opens the join window.
        //
        // Losing a parent is the stack's job to fix, and it has the means:
        // a secured rejoin uses the key this device already holds and
        // needs no permission from anyone. So say so and let it work.
        remote_log("!!! Zigbee: %lu readings unacknowledged; waiting for the "
                   "stack to find its way back", (unsigned long)s_unacked);
        snprintf(s_last_trouble, sizeof s_last_trouble,
                 "%lu readings unacknowledged", (unsigned long)s_unacked);
        s_trouble_since_us = esp_timer_get_time();
        return;
    }
    if (s_unacked < UNACKED_BEFORE_REJOIN) return;

    // Last resort, and only after a long time with nothing getting through.
    // By this point rejoining has had many minutes to work and has not, so
    // the stored credentials are worth less than a fresh start -- and a
    // fresh start at least recovers by itself once somebody scans.
    if (esp_timer_get_time() - s_trouble_since_us
        < (int64_t)GIVE_UP_AFTER_SECONDS * 1000000) return;
    remote_log("!!! Zigbee: nothing acknowledged for %d minutes; starting over",
               GIVE_UP_AFTER_SECONDS / 60);
    s_unacked = 0;
    s_joined = false;
    s_trouble_since_us = esp_timer_get_time();
    ezb_bdb_reset_via_local_action();
}
static const char *bdb_status_name(uint8_t s);
static uint32_t channel_mask(void);
static uint32_t primary_mask(void);
static void save_channel(uint8_t channel);

// Which radio has priority, and whether the Zigbee receiver stays on.
//
// Called at startup and whenever Wi-Fi is switched. The capability is
// announced when the device joins, so a change made later tells the
// local stack but not the coordinator -- which is the right way round:
// turning Wi-Fi on takes the radio away, and the web page is then the
// way in instead of Zigbee commands.
void zigbee_share_radio_with_wifi(bool wifi_on)
{
    ezb_nwk_set_rx_on_when_idle(!wifi_on);
    esp_coex_preference_set(wifi_on ? ESP_COEX_PREFER_WIFI : ESP_COEX_PREFER_BALANCE);
    remote_log("Zigbee: receiver %s (Wi-Fi %s)",
               wifi_on ? "off between exchanges" : "on", wifi_on ? "on" : "off");
}

static void register_endpoint(void)
{
    // A standards-shaped Home Automation device, not a bare endpoint. BDB and
    // ZDO lean on the Basic and Identify clusters during commissioning, and an
    // endpoint carrying none of them never gets through it.
    ezb_zha_temperature_sensor_config_t cfg = {
        .basic_cfg = {.zcl_version = 3, .power_source = 1 /* mains */},
        .identify_cfg = {0},
        .temp_meas_cfg = {0},
    };
    ezb_af_ep_desc_t ep_desc = ezb_zha_create_temperature_sensor(SCALE_ENDPOINT, &cfg);
    ezb_af_device_desc_t dev = ezb_af_create_device_desc();
    ezb_err_t a = ezb_af_device_add_endpoint_desc(dev, ep_desc);
    ezb_err_t r = ezb_af_device_desc_register(dev);
    remote_log("Zigbee: endpoint %d add=%d register=%d", SCALE_ENDPOINT, (int)a, (int)r);
}


static void send_reading(const scale_reading_t *r, uint32_t seq)
{
    scale_report_t report = {
        .seq = seq,
        .grams = r->grams,
        .batt = r->batt,
        .batt_pct = (int8_t)remote_batt_pct(r->batt),
        .fw_major = s_fw[0], .fw_minor = s_fw[1], .fw_patch = s_fw[2],
        .reset_reason = (uint8_t)esp_reset_reason(),
        .uptime_s = (uint32_t)(esp_timer_get_time() / 1000000),
        // The Wi-Fi bit lets the coordinator toggle it without having to
        // guess which way round it currently is.
        .flags = (uint8_t)((r->ok ? FLAG_OK : 0) | (r->stable ? FLAG_STABLE : 0) |
                           (r->saturated ? FLAG_SATURATED : 0) |
                           (remote_wifi_enabled() ? FLAG_WIFI : 0)),
        .interval_s = remote_interval_seconds(),
    };
    ezb_apsde_data_req_t req = {
        .dst_address = EZB_ADDRESS_SHORT(COORDINATOR_ADDR),
        .src_endpoint = SCALE_ENDPOINT,
        .dst_endpoint = SCALE_ENDPOINT,
        .cluster_id = SCALE_CLUSTER,
        .profile_id = SCALE_PROFILE,
        .radius = 0,
        // Ask to be acknowledged. Without this the scale cannot tell a
        // delivered reading from one shouted into an empty room, and after a
        // coordinator restart it will happily transmit for days to a parent
        // that no longer has it.
        .tx_options = EZB_APSDE_TX_OPT_ACK_TX,
        .asdu_length = sizeof report,
        .asdu = (uint8_t *)&report,
    };
    ezb_err_t e = ezb_apsde_data_request(&req);
    if (e == 0) {
        s_reports++;
    } else {
        s_report_fails++;
        snprintf(s_last_trouble, sizeof s_last_trouble, "report refused: ezb error %d", (int)e);
    }
}

// Commands arrive the same way they do over Wi-Fi: as a short line handed to the scale
// loop. That keeps one command vocabulary across every link.
static bool on_data(const ezb_apsde_data_ind_t *ind)
{
    if (ind->cluster_id != SCALE_CLUSTER || ind->asdu_length == 0) return false;
    char cmd[REMOTE_CMD_LEN];
    size_t n = ind->asdu_length < sizeof cmd - 1 ? ind->asdu_length : sizeof cmd - 1;
    memcpy(cmd, ind->asdu, n);
    cmd[n] = '\0';
    s_commands++;
    remote_log("Zigbee command: %s", cmd);
    remote_send_command(cmd);
    // Answer straight away rather than at the next scheduled reading.
    //
    // Every report carries the rate the scale is running at, and that is
    // the only way the hub ever learns it. So a command that changes the
    // rate could not be confirmed until the next report -- which is one
    // whole new interval away, by definition. Set it to an hour and the
    // page says "changing to 1 hour" for an hour. One extra frame per
    // command is a cheap price for the page telling the truth promptly.
    s_report_now = true;
    return true;
}

static bool on_signal(const ezb_app_signal_t *signal)
{
    switch (ezb_app_signal_get_type(signal)) {
    case EZB_ZDO_SIGNAL_SKIP_STARTUP:
        ezb_bdb_start_top_level_commissioning(EZB_BDB_MODE_INITIALIZATION);
        return false;
    case EZB_BDB_SIGNAL_DEVICE_FIRST_START:
        // Initialisation has finished and there is no network saved, so this
        // is the moment BDB is ready to be told to go looking. Starting any
        // earlier is refused as busy.
        start_steering("first start");
        return false;
    case EZB_BDB_SIGNAL_STEERING: {
        // Retrying is left to the steering task. Starting another search from
        // here, as soon as this one fails, keeps the shared radio scanning
        // without pause and pushes Wi-Fi off the air.
        s_joined = ezb_bdb_dev_joined();
        if (s_joined) {
            s_joins++;
            s_joined_at_us = esp_timer_get_time();
            s_rejoin_deadline_us = 0;
            s_announce_host = true;
            save_channel((uint8_t)ezb_nwk_get_current_channel());
            remote_log("Zigbee: joined pan 0x%04x on channel %u",
                       (unsigned)ezb_nwk_get_panid(), (unsigned)ezb_nwk_get_current_channel());
            return false;
        }
        const ezb_bdb_signal_simple_params_t *pr = ezb_app_signal_get_params(signal);
        snprintf(s_last_trouble, sizeof s_last_trouble, "joining failed: %s",
                 pr ? bdb_status_name(pr->status) : "no reason given");
        remote_log("Zigbee: joining failed, %s",
                   pr ? bdb_status_name(pr->status) : "no reason given");
        return false;
    }

    case EZB_BDB_SIGNAL_DEVICE_REBOOT:
        s_joined = ezb_bdb_dev_joined();
        if (s_joined) {
            s_joins++;
            s_joined_at_us = esp_timer_get_time();
            s_rejoin_deadline_us = 0;
            s_announce_host = true;
            save_channel((uint8_t)ezb_nwk_get_current_channel());
            remote_log("Zigbee: rejoined pan 0x%04x on channel %u",
                       (unsigned)ezb_nwk_get_panid(), (unsigned)ezb_nwk_get_current_channel());
        } else {
            // Steering is for a device that has never joined. One that kept
            // state from an earlier run rejoins instead, and asking it to
            // steer is answered with "no network found".
            //
            // This used to wipe that state and restart, which read as
            // tidying up and was in fact the bug underneath every "it will
            // not come back on its own" in this project. Stored credentials
            // are what make a rejoin possible: a secured rejoin uses the key
            // the device already holds and needs nobody's permission, so a
            // power cut should be invisible. Throwing the key away on every
            // boot turned each one into a fresh join, which needs the
            // coordinator's join window open -- which means a person.
            //
            // Not being joined at this instant is not failure, either. The
            // rejoin is asynchronous and this signal arrives before it has
            // had a chance; the right response is to wait for it.
            bool fresh = ezb_bdb_is_factory_new();
            remote_log("Zigbee: ready, factory new = %s", fresh ? "yes" : "no");
            if (fresh) {
                start_steering("after initialisation");
            } else {
                remote_log("Zigbee: has a network saved; waiting for it to rejoin");
                s_rejoin_deadline_us = esp_timer_get_time()
                    + (int64_t)REJOIN_GRACE_SECONDS * 1000000;
            }
        }
        return false;
    default:
        remote_log("Zigbee signal: %s",
                   ezb_app_signal_to_string(ezb_app_signal_get_type(signal)));
        return false;
    }
}


// Looks for a coordinator now and then, rather than continuously: the radio
// belongs to Wi-Fi too, and a scan that never stops means Wi-Fi never gets it.
//
// A flat minute was the wrong shape for both ends of the problem. A scale
// switched on next to an open join window waited up to a minute for its
// second try, which feels broken; a scale in a house with no coordinator at
// all scanned every minute forever, for nothing. So: try again quickly at
// first, then slower, settling at the old minute.
#define STEERING_FIRST_RETRY_SECONDS 2
#define STEERING_RETRY_SECONDS 60



#define ALL_CHANNELS 0x07FFF800u   /* 11-26 */

/* The channel this scale last found a coordinator on, kept in our own NVS
 * namespace rather than the stack's: a factory reset clears the stack's
 * store, and this is exactly the knowledge worth surviving one. */
static uint8_t saved_channel(void)
{
    nvs_handle_t h;
    uint8_t channel = 0;
    if (nvs_open("scale", NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, "zbchan", &channel);
        nvs_close(h);
    }
    return (channel >= 11 && channel <= 26) ? channel : 0;
}

static void save_channel(uint8_t channel)
{
    if (channel < 11 || channel > 26 || channel == saved_channel()) return;
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, "zbchan", channel);
    nvs_commit(h);
    nvs_close(h);
}

static uint32_t channel_mask(void)
{
    // Channel 0 means "look everywhere": slower, but it does not depend on
    // the coordinator having stayed where we last saw it.
    return CONFIG_SCALE_ZIGBEE_CHANNEL ? (1u << CONFIG_SCALE_ZIGBEE_CHANNEL)
                                       : ALL_CHANNELS;
}

/* Where to look first. BDB scans the primary set before the secondary one,
 * and setting both to every channel -- which is what this did -- threw that
 * away and made every search a sixteen channel sweep. One channel is the
 * common case and is far quicker; everywhere else is still the fallback. */
static uint32_t primary_mask(void)
{
    uint8_t channel = saved_channel();
    if (channel) return 1u << channel;
    return channel_mask();
}

static const char *bdb_status_name(uint8_t s)
{
    switch (s) {
    case EZB_BDB_STATUS_SUCCESS:           return "success";
    case EZB_BDB_STATUS_IN_PROGRESS:       return "still in progress";
    case EZB_BDB_STATUS_NO_NETWORK:        return "no network found";
    case EZB_BDB_STATUS_FORMATION_FAILURE: return "formation failed";
    case EZB_BDB_STATUS_NOT_PERMITTED:     return "not permitted";
    case EZB_BDB_STATUS_TCLK_EX_FAILURE:   return "trust-centre key exchange failed";
    case EZB_BDB_STATUS_NOT_ON_A_NETWORK:  return "not on a network";
    case EZB_BDB_STATUS_ON_A_NETWORK:      return "already on a network";
    case EZB_BDB_STATUS_CANCELLED:         return "cancelled";
    case EZB_BDB_STATUS_DEV_ANNCE_SEND_FAILURE: return "device announce not acknowledged";
    default:                               return "unknown reason";
    }
}

static void start_steering(const char *why)
{
    // The channel set is reapplied here because a BDB reset puts it back to
    // its default, and steering would then search somewhere else entirely.
    ezb_bdb_set_primary_channel_set(primary_mask());
    ezb_bdb_set_secondary_channel_set(channel_mask());
    s_searches++;
    ezb_err_t e = ezb_bdb_start_top_level_commissioning(EZB_BDB_MODE_NETWORK_STEERING);
    if (e == 0) {
        remote_log("Zigbee: looking for a coordinator (%s)", why);
    } else if (e == 9 /* EZB_ERR_BUSY */) {
        remote_log("Zigbee: still busy commissioning (%s); will try again", why);
    } else {
        remote_log("!!! Zigbee: steering refused (%s), error %d", why, (int)e);
    }
}

/* Sleep without starving the watchdog.
 *
 * A watched task that sleeps longer than the watchdog's timeout is a
 * panic waiting to happen, and the steering backoff grows to sixty
 * seconds against a thirty second watchdog. That fired only while the
 * device was off the network -- which is to say, only while it was
 * trying to recover -- so the watchdog added to catch a hang was
 * instead rebooting the scale mid-retry and guaranteeing it never got
 * back. Sliced, so a long wait still checks in.
 */
#define WDT_SLICE_SECONDS 5

static void watched_sleep(uint32_t seconds)
{
    while (seconds) {
        uint32_t slice = seconds < WDT_SLICE_SECONDS ? seconds : WDT_SLICE_SECONDS;
        vTaskDelay(pdMS_TO_TICKS(slice * 1000));
        esp_task_wdt_reset();
        seconds -= slice;
    }
}

static void steering_task(void *arg)
{
    (void)arg;
    // Watched, so that a task stuck on a stack lock becomes a panic and
    // a backtrace rather than a scale that is powered and mute.
    esp_task_wdt_add(NULL);
    // Started by zigbee_task once the stack is up. It cannot poll for that:
    // every call into the stack, esp_zigbee_is_started() included, asserts on
    // uninitialised globals if it arrives first.

    uint32_t wait_s = STEERING_FIRST_RETRY_SECONDS;
    for (;;) {
        // A backstop only. The first attempt is made from the signal handler,
        // when BDB says it is ready; this picks it up again if that failed.
        esp_task_wdt_reset();
        watched_sleep(wait_s);
        if (s_joined) {
            wait_s = STEERING_FIRST_RETRY_SECONDS;   // ready to be quick again
            continue;
        }
        if (wait_s < STEERING_RETRY_SECONDS) {
            wait_s *= 2;
            if (wait_s > STEERING_RETRY_SECONDS) wait_s = STEERING_RETRY_SECONDS;
        }

        // A saved network that has not come back. Steering cannot help --
        // the stack answers "no network found" while it still holds old
        // credentials -- so this is the one place a reset is the right
        // move, and only once waiting has genuinely failed.
        if (!ezb_bdb_is_factory_new()) {
            if (s_rejoin_deadline_us == 0
                || esp_timer_get_time() < s_rejoin_deadline_us) continue;
            remote_log("!!! Zigbee: did not rejoin within %d s; starting over",
                       REJOIN_GRACE_SECONDS);
            esp_zigbee_factory_reset();  // does not return
        }
        if (esp_zigbee_lock_acquire(pdMS_TO_TICKS(1000))) {
            start_steering("retry");
            esp_zigbee_lock_release();
        } else {
            remote_log("!!! Zigbee: could not take the stack lock");
        }
    }
}

/* Tells the coordinator what this scale is called. A short text frame on the
 * same cluster; the coordinator passes anything that is not a reading
 * straight through. */
static void announce_host(void)
{
    char text[64];
    const char *ip = remote_sta_ip();
    int n = ip && ip[0]
        ? snprintf(text, sizeof text, "host=%s ip=%s", remote_hostname(), ip)
        : snprintf(text, sizeof text, "host=%s", remote_hostname());
    if (n <= 0) return;
    ezb_apsde_data_req_t req = {
        .dst_address = EZB_ADDRESS_SHORT(COORDINATOR_ADDR),
        .src_endpoint = SCALE_ENDPOINT,
        .dst_endpoint = SCALE_ENDPOINT,
        .cluster_id = SCALE_CLUSTER,
        .profile_id = SCALE_PROFILE,
        .radius = 0,
        .asdu_length = (uint16_t)n,
        .asdu = (uint8_t *)text,
    };
    ezb_apsde_data_request(&req);
}

static void report_task(void *arg)
{
    // Watched too: this is the task whose silence is the symptom.
    esp_task_wdt_add(NULL);
    uint32_t sent = 0;
    int64_t last_us = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_task_wdt_reset();
        /* Write off a report nothing answered.
         *
         * Generous, because a sleepy device's acknowledgement travels
         * on its own schedule and a slow one is not a lost one. What is
         * being caught here is not lateness but a coordinator that has
         * stopped listening entirely, which shows up as every single
         * report going unanswered rather than an occasional one. */
        if (s_awaiting_confirm
            && esp_timer_get_time() - s_sent_at_us
               > (int64_t)CONFIRM_WAIT_SECONDS * 1000000) {
            s_awaiting_confirm = false;
            note_unacked();
        }
        if (!s_joined) continue;
        // One rate for the whole scale: Zigbee reports whatever the sampling
        // interval is set to, so the web page and the Zigbee feed agree and
        // there is a single thing to configure.
        //
        // The exception is continuous mode, where the scale samples ten times
        // a second for a live display. Relaying that floods the shared radio
        // and pushes Wi-Fi off the air, so it is held to the floor below.
        uint32_t every = zigbee_report_every();
        int64_t now = esp_timer_get_time();
        bool forced = s_report_now;
        if (!forced && last_us && now - last_us < (int64_t)every * 1000000) continue;
        scale_reading_t r;
        uint32_t seq = remote_get_reading(&r);
        // A forced report goes out even if the scale has not sampled since
        // the last one: what has changed is the settings it carries, not
        // the weight.
        if (!forced && seq == sent) continue;
        s_report_now = false;
        last_us = now;
        if (esp_zigbee_lock_acquire(pdMS_TO_TICKS(500))) {
            if (s_announce_host ||
                now - s_host_sent_us > (int64_t)HOST_REPEAT_SECONDS * 1000000) {
                s_announce_host = false;
                s_host_sent_us = now;
                announce_host();
            }
            s_last_report_us = now;
            s_awaiting_confirm = true;
            s_sent_at_us = now;
            send_reading(&r, seq);
            esp_zigbee_lock_release();
            sent = seq;
        }
    }
}

static void zigbee_task(void *arg)
{
    const esp_zigbee_config_t config = {
        .device_config = {
            .device_type = EZB_NWK_DEVICE_TYPE_END_DEVICE,
            .install_code_policy = false,
            // Staying awake: a sleepy end device cannot also hold a Wi-Fi
            // association, and keeping Wi-Fi is the point of this build.
            .zed_config = {.ed_timeout = 3, .keep_alive = 3000},
        },
        .platform_config = {
            .radio_config = {.radio_mode = ESP_ZIGBEE_RADIO_MODE_NATIVE},
        },
    };
    // Nothing here is allowed to abort the program. Wi-Fi is the way back in
    // to a scale that is out of reach, and a Zigbee fault must never cost us
    // that: it gives up and leaves the rest of the firmware running.
    esp_err_t err = esp_zigbee_init(&config);
    if (err != ESP_OK) {
        remote_log("!!! Zigbee could not start: %s. Wi-Fi is unaffected.", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    ezb_app_signal_add_handler(on_signal);
    ezb_apsde_data_indication_handler_register(on_data);
    ezb_apsde_data_confirm_handler_register(on_confirm);

    // These belong here, not in the steering path: a device that rejoins a
    // network it already knows never steers, and would otherwise run with
    // none of them applied.
    //
    // The coordinator is a centralised trust centre; looking for a
    // distributed-security network instead makes every beacon unusable.
    ezb_aps_secur_enable_distributed_security(false);
    // BDB discards beacons below a minimum link quality before the
    // application sees them, which is indistinguishable from hearing none.
    ezb_nwk_set_min_join_lqi(0);
    // The 802.15.4 receiver follows Wi-Fi, because they share one radio.
    //
    // Holding the receiver on costs Wi-Fi nearly all of it: the scale still
    // associates and answers ARP, but ICMP and HTTP never get through. So it
    // was turned off outright, and that stopped every command reaching the
    // scale -- a frame from the coordinator is buffered for a device that
    // says it is not listening, and dropped when it never collects one. The
    // rate menu sprang back and the Wi-Fi button did nothing, with the radio
    // reporting each command as delivered.
    //
    // Wi-Fi is off nearly all the time on a scale under a plant, and with
    // nothing to protect there is no reason to be deaf. On when Wi-Fi is
    // off, off when it is on, decided here at boot and again whenever Wi-Fi
    // is switched, so whichever radio is in use has what it needs.
    zigbee_share_radio_with_wifi(remote_wifi_enabled());
    // Look on one channel, not all sixteen. A full sweep keeps the shared radio
    // busy for seconds at a time, which is enough to stop Wi-Fi associating.
    // Channel 0 means "look everywhere": slower, but it does not depend on
    // the coordinator having stayed where we last saw it.
    // The coordinator is a centralised trust centre. Without this the device
    // looks for a distributed-security network instead and discards every
    // beacon it hears, which is reported as simply finding no network.
    ezb_aps_secur_enable_distributed_security(false);
    ezb_err_t ch = ezb_bdb_set_primary_channel_set(primary_mask());
    ezb_bdb_set_secondary_channel_set(channel_mask());
    remote_log("Zigbee: channel mask 0x%08x -> %d", (unsigned)channel_mask(), (int)ch);
    register_endpoint();
    if ((err = esp_zigbee_start(false)) != ESP_OK) {
        remote_log("!!! Zigbee could not start: %s. Wi-Fi is unaffected.", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    // Only now is it safe for anything else to call into the stack.
    xTaskCreate(steering_task, "zb_steer", 3072, NULL, 5, NULL);
    esp_zigbee_launch_mainloop();
    vTaskDelete(NULL);
}

void zigbee_rejoin(void)
{
    // Throw away the network and go and find it again.
    //
    // A device can be joined, sending happily, and still unreachable: the
    // reports go up while every command times out with no acknowledgement,
    // because the parent-child relationship is only half there. Nothing
    // short of starting over fixes that, and until now there was no way to
    // ask for it except by waiting half an hour for the unacked-report
    // path to give up -- which needs the coordinator's join window open at
    // exactly the right moment.
    remote_log(">>> Zigbee: leaving the network and looking again");
    if (esp_zigbee_lock_acquire(pdMS_TO_TICKS(1000))) {
        s_joined = false;
        ezb_bdb_reset_via_local_action();
        esp_zigbee_lock_release();
    } else {
        remote_log("!!! Zigbee: could not take the stack lock");
    }
}

void zigbee_start(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    if (app) {
        unsigned major = 0, minor = 0, patch = 0;
        sscanf(app->version, "%u.%u.%u", &major, &minor, &patch);
        s_fw[0] = (uint8_t)major;
        s_fw[1] = (uint8_t)minor;
        s_fw[2] = (uint8_t)patch;
    }
    // Idempotent, because two paths call it: boot calls it when Wi-Fi
    // is off, and the Wi-Fi handler calls it once an address arrives.
    // With Wi-Fi off at boot and then switched on, both fired -- which
    // started a second pair of tasks and left the stack reporting
    // ESP_ERR_INVALID_STATE. The guard belongs here, where the thing
    // being started is, not in one of the two callers.
    static bool started;
    if (started) return;
    started = true;
    load_floor();
    xTaskCreate(zigbee_task, "zigbee", 8192, NULL, 5, NULL);
    xTaskCreate(report_task, "zb_report", 3072, NULL, 5, NULL);
}

// Zigbee shares the scale's nvs partition. If it fills, writes start failing
// and, worse, the boot code erases the whole partition to recover -- taking
// the calibration with it. Worth watching rather than discovering.
static int nvs_free_entries(void)
{
    nvs_stats_t st;
    return nvs_get_stats(NULL, &st) == ESP_OK ? (int)st.free_entries : -1;
}

int zigbee_format_status(char *buf, size_t len)
{
    int64_t now = esp_timer_get_time();
    ezb_extpanid_t ext = {0};
    if (s_joined) ezb_nwk_get_extended_panid(&ext);
    return snprintf(buf, len,
        "{\"joined\":%s,\"pan\":\"0x%04x\",\"channel\":%u,\"short\":\"0x%04x\","
        "\"joins\":%lu,\"searches\":%lu,\"reports\":%lu,\"report_fails\":%lu,"
        // The one that matters: reports nobody answered. report_fails
        // counts only sends the stack refused, which stayed at zero
        // through every episode of talking to nobody.
        "\"unacked\":%lu,"
        "\"commands\":%lu,\"last_report_s\":%ld,\"joined_for_s\":%ld,"
        "\"every_s\":%lu,\"floor_s\":%lu,\"nvs_free\":%d,\"trouble\":\"%s\"}",
        s_joined ? "true" : "false",
        s_joined ? (unsigned)ezb_nwk_get_panid() : 0u,
        s_joined ? (unsigned)ezb_nwk_get_current_channel() : 0u,
        s_joined ? (unsigned)ezb_nwk_get_short_address() : 0u,
        (unsigned long)s_joins, (unsigned long)s_searches,
        (unsigned long)s_reports, (unsigned long)s_report_fails,
        (unsigned long)s_unacked,
        (unsigned long)s_commands,
        s_last_report_us ? (long)((now - s_last_report_us) / 1000000) : -1,
        s_joined_at_us ? (long)((now - s_joined_at_us) / 1000000) : -1,
        (unsigned long)zigbee_report_every(), (unsigned long)s_floor_s,
        nvs_free_entries(),
        s_last_trouble);
}
