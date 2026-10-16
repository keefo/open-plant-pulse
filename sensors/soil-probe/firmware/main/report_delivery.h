#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "report_queue.h"

/* The durable queue of reports awaiting the hub, kept in NVS. Safe to call from
 * the report task and from the Bluetooth host task at once. NVS must be
 * initialised first. */
esp_err_t opp_delivery_init(void);
bool opp_delivery_push(const opp_queued_report_t *report);
bool opp_delivery_newest(opp_queued_report_t *report);
/* The bulk drain, on the hub's connection. */
size_t opp_delivery_page(uint8_t output[OPP_REPORT_QUEUE_PAGE_MAX_SIZE]);
opp_report_ack_result_t opp_delivery_acknowledge_through(uint32_t report_id);
void opp_delivery_connection_ended(void);
uint32_t opp_delivery_depth(void);
uint32_t opp_delivery_capacity(void);
bool opp_delivery_clear(void);
