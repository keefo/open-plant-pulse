#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bthome_payload.h"

/* The durable queue of reports awaiting the hub's acknowledgement.
 *
 * A ring of fixed slots addressed by two ever-increasing counters, `head` (the
 * oldest unacknowledged report) and `tail` (where the next one goes). Storage
 * is behind callbacks so the rules can be tested without flash. */

typedef struct {
    uint32_t report_id;
    uint8_t main[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    uint8_t main_size;
    uint8_t supplementary[OPP_BTHOME_SERVICE_DATA_MAX_SIZE];
    uint8_t supplementary_size;
} opp_queued_report_t;

typedef struct {
    void *context;
    /* Both counters are saved together, so a change is all or nothing. */
    bool (*load_counters)(void *context, uint32_t *head, uint32_t *tail);
    bool (*save_counters)(void *context, uint32_t head, uint32_t tail);
    bool (*load_entry)(void *context, uint32_t slot, opp_queued_report_t *entry);
    bool (*save_entry)(void *context, uint32_t slot, const opp_queued_report_t *entry);
} opp_report_queue_storage_t;

typedef struct {
    opp_report_queue_storage_t storage;
    uint32_t capacity;
    uint32_t head;
    uint32_t tail;
    bool head_cached;
    opp_queued_report_t head_entry;
    /* The last report put on a page during this connection: the hub can
     * acknowledge only what it has read. */
    uint32_t served_through;
} opp_report_queue_t;

/* A queue page: marker, count, then per report its ID, and each packet as a
 * length byte and the bytes. Eight full reports fit the 512-byte ATT limit. */
#define OPP_REPORT_QUEUE_PAGE_MARKER 0x20U
#define OPP_REPORT_QUEUE_PAGE_REPORTS 8U
#define OPP_REPORT_QUEUE_PAGE_MAX_SIZE \
    (2U + OPP_REPORT_QUEUE_PAGE_REPORTS * (4U + 1U + OPP_BTHOME_SERVICE_DATA_MAX_SIZE + \
                                          1U + OPP_BTHOME_SERVICE_DATA_MAX_SIZE))

typedef enum {
    /* One or more reports removed. */
    OPP_REPORT_ACK_REMOVED,
    /* Nothing queued up to that ID (acknowledged before): accepted, no change. */
    OPP_REPORT_ACK_ALREADY,
    /* Never served on a page this connection, ID 0, or not saved: nothing
     * removed. */
    OPP_REPORT_ACK_REFUSED,
} opp_report_ack_result_t;

/* Start from what storage holds; an empty or inconsistent store starts empty. */
bool opp_report_queue_open(opp_report_queue_t *queue, const opp_report_queue_storage_t *storage,
                           uint32_t capacity);
uint32_t opp_report_queue_depth(const opp_report_queue_t *queue);
bool opp_report_queue_full(const opp_report_queue_t *queue);
/* Saved before it returns true; false when full or storage fails. */
bool opp_report_queue_push(opp_report_queue_t *queue, const opp_queued_report_t *entry);
bool opp_report_queue_head(opp_report_queue_t *queue, opp_queued_report_t *entry);
/* The most recent report: what the sensor advertises, so the hub sees fresh
 * data whatever is still queued behind it. */
bool opp_report_queue_newest(opp_report_queue_t *queue, opp_queued_report_t *entry);
/* Encode the oldest reports as a queue page; returns its size (at least 2).
 * Remembers the last report on it as the most the hub may acknowledge. */
size_t opp_report_queue_page(opp_report_queue_t *queue,
                             uint8_t output[OPP_REPORT_QUEUE_PAGE_MAX_SIZE]);
/* Remove every queued report up to and including `report_id`, with one saved
 * change of the head. */
opp_report_ack_result_t opp_report_queue_ack_through(opp_report_queue_t *queue,
                                                     uint32_t report_id);
/* The connection ended: nothing has been served to anyone now. */
void opp_report_queue_end_session(opp_report_queue_t *queue);
bool opp_report_queue_clear(opp_report_queue_t *queue);
