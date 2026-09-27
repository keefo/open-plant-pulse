#include "report_queue.h"

#include <string.h>

static uint32_t slot_of(const opp_report_queue_t *queue, uint32_t counter)
{
    return counter % queue->capacity;
}

bool opp_report_queue_open(opp_report_queue_t *queue, const opp_report_queue_storage_t *storage,
                           uint32_t capacity)
{
    if (queue == NULL || storage == NULL || capacity == 0) {
        return false;
    }
    *queue = (opp_report_queue_t){.storage = *storage, .capacity = capacity};
    uint32_t head;
    uint32_t tail;
    if (storage->load_counters(storage->context, &head, &tail) && tail >= head &&
        tail - head <= capacity) {
        queue->head = head;
        queue->tail = tail;
    }
    return true;
}

uint32_t opp_report_queue_depth(const opp_report_queue_t *queue)
{
    return queue == NULL ? 0 : queue->tail - queue->head;
}

bool opp_report_queue_full(const opp_report_queue_t *queue)
{
    return queue != NULL && opp_report_queue_depth(queue) >= queue->capacity;
}

bool opp_report_queue_push(opp_report_queue_t *queue, const opp_queued_report_t *entry)
{
    if (queue == NULL || entry == NULL || entry->report_id == 0 || opp_report_queue_full(queue)) {
        return false;
    }
    /* The entry first, then the counter that makes it part of the queue: a
     * reset between the two leaves a slot that is simply written again. */
    if (!queue->storage.save_entry(queue->storage.context, slot_of(queue, queue->tail), entry) ||
        !queue->storage.save_counters(queue->storage.context, queue->head, queue->tail + 1)) {
        return false;
    }
    queue->tail++;
    return true;
}

bool opp_report_queue_head(opp_report_queue_t *queue, opp_queued_report_t *entry)
{
    if (queue == NULL || entry == NULL || opp_report_queue_depth(queue) == 0) {
        return false;
    }
    if (!queue->head_cached) {
        if (!queue->storage.load_entry(queue->storage.context, slot_of(queue, queue->head),
                                       &queue->head_entry)) {
            return false;
        }
        queue->head_cached = true;
    }
    *entry = queue->head_entry;
    return true;
}

bool opp_report_queue_newest(opp_report_queue_t *queue, opp_queued_report_t *entry)
{
    if (queue == NULL || entry == NULL || opp_report_queue_depth(queue) == 0) {
        return false;
    }
    return queue->storage.load_entry(queue->storage.context, slot_of(queue, queue->tail - 1),
                                     entry);
}

static bool load_at(opp_report_queue_t *queue, uint32_t counter, opp_queued_report_t *entry)
{
    if (counter == queue->head) {
        return opp_report_queue_head(queue, entry);
    }
    return queue->storage.load_entry(queue->storage.context, slot_of(queue, counter), entry);
}

size_t opp_report_queue_page(opp_report_queue_t *queue,
                             uint8_t output[OPP_REPORT_QUEUE_PAGE_MAX_SIZE])
{
    if (queue == NULL || output == NULL) {
        return 0;
    }
    size_t offset = 2;
    uint8_t count = 0;
    for (uint32_t counter = queue->head;
         counter != queue->tail && count < OPP_REPORT_QUEUE_PAGE_REPORTS; ++counter) {
        opp_queued_report_t entry;
        if (!load_at(queue, counter, &entry) ||
            entry.main_size > OPP_BTHOME_SERVICE_DATA_MAX_SIZE ||
            entry.supplementary_size > OPP_BTHOME_SERVICE_DATA_MAX_SIZE) {
            /* Serve what could be read; the rest waits for the next page. */
            break;
        }
        output[offset++] = (uint8_t)entry.report_id;
        output[offset++] = (uint8_t)(entry.report_id >> 8U);
        output[offset++] = (uint8_t)(entry.report_id >> 16U);
        output[offset++] = (uint8_t)(entry.report_id >> 24U);
        output[offset++] = entry.main_size;
        memcpy(&output[offset], entry.main, entry.main_size);
        offset += entry.main_size;
        output[offset++] = entry.supplementary_size;
        memcpy(&output[offset], entry.supplementary, entry.supplementary_size);
        offset += entry.supplementary_size;
        if (entry.report_id > queue->served_through) {
            queue->served_through = entry.report_id;
        }
        ++count;
    }
    output[0] = OPP_REPORT_QUEUE_PAGE_MARKER;
    output[1] = count;
    return offset;
}

opp_report_ack_result_t opp_report_queue_ack_through(opp_report_queue_t *queue,
                                                     uint32_t report_id)
{
    if (queue == NULL || report_id == 0) {
        return OPP_REPORT_ACK_REFUSED;
    }
    uint32_t new_head = queue->head;
    while (new_head != queue->tail) {
        opp_queued_report_t entry;
        if (!load_at(queue, new_head, &entry)) {
            return OPP_REPORT_ACK_REFUSED;
        }
        if (entry.report_id > report_id) {
            break;
        }
        /* Only what the hub has read may go: it cannot have stored the rest. */
        if (entry.report_id > queue->served_through) {
            return OPP_REPORT_ACK_REFUSED;
        }
        ++new_head;
    }
    if (new_head == queue->head) {
        /* Nothing queued up to that ID: it went before, so a repeat is
         * harmless. */
        return OPP_REPORT_ACK_ALREADY;
    }
    if (!queue->storage.save_counters(queue->storage.context, new_head, queue->tail)) {
        return OPP_REPORT_ACK_REFUSED;
    }
    queue->head = new_head;
    queue->head_cached = false;
    return OPP_REPORT_ACK_REMOVED;
}

void opp_report_queue_end_session(opp_report_queue_t *queue)
{
    if (queue != NULL) {
        queue->served_through = 0;
    }
}

bool opp_report_queue_clear(opp_report_queue_t *queue)
{
    if (queue == NULL ||
        !queue->storage.save_counters(queue->storage.context, queue->tail, queue->tail)) {
        return false;
    }
    queue->head = queue->tail;
    queue->head_cached = false;
    return true;
}
