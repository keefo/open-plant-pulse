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
} opp_report_queue_t;

typedef enum {
    /* The oldest report: removed. */
    OPP_REPORT_ACK_REMOVED,
    /* Not queued any more (acknowledged before): accepted, nothing changes. */
    OPP_REPORT_ACK_ALREADY,
    /* Queued behind the oldest, unknown, or not saved: nothing removed. */
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
opp_report_ack_result_t opp_report_queue_ack(opp_report_queue_t *queue, uint32_t report_id);
bool opp_report_queue_clear(opp_report_queue_t *queue);
