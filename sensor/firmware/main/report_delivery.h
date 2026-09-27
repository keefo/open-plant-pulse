#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "report_queue.h"

/* The durable queue of reports awaiting the hub, kept in NVS. Safe to call from
 * the report task and from the Bluetooth host task at once. NVS must be
 * initialised first. */
esp_err_t opp_delivery_init(void);
bool opp_delivery_push(const opp_queued_report_t *report);
bool opp_delivery_head(opp_queued_report_t *report);
opp_report_ack_result_t opp_delivery_acknowledge(uint32_t report_id);
uint32_t opp_delivery_depth(void);
uint32_t opp_delivery_capacity(void);
bool opp_delivery_clear(void);
