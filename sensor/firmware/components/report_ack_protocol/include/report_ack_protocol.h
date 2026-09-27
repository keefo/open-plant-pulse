#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Report acknowledgement version 3: the hub has stored this report, complete.
 * See "Durable delivery and acknowledgement" in protocol/README.md. */
#define OPP_REPORT_ACK_PROTOCOL_VERSION 3
#define OPP_REPORT_ACK_PAYLOAD_SIZE 5

size_t opp_report_ack_encode(uint32_t report_id, uint8_t output[OPP_REPORT_ACK_PAYLOAD_SIZE]);

bool opp_report_ack_decode(const uint8_t *payload, size_t payload_size, uint32_t *report_id);
