#include "report_queue.h"

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

opp_report_ack_result_t opp_report_queue_ack(opp_report_queue_t *queue, uint32_t report_id)
{
    if (queue == NULL || report_id == 0) {
        return OPP_REPORT_ACK_REFUSED;
    }
    opp_queued_report_t head;
    if (!opp_report_queue_head(queue, &head)) {
        /* Nothing queued: whatever it names was delivered before. */
        return opp_report_queue_depth(queue) == 0 ? OPP_REPORT_ACK_ALREADY
                                                  : OPP_REPORT_ACK_REFUSED;
    }
    if (report_id < head.report_id) {
        /* Report IDs only increase, so an older one was removed before. */
        return OPP_REPORT_ACK_ALREADY;
    }
    if (report_id != head.report_id) {
        return OPP_REPORT_ACK_REFUSED;
    }
    if (!queue->storage.save_counters(queue->storage.context, queue->head + 1, queue->tail)) {
        return OPP_REPORT_ACK_REFUSED;
    }
    queue->head++;
    queue->head_cached = false;
    return OPP_REPORT_ACK_REMOVED;
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
