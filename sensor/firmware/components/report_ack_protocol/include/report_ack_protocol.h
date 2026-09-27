#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Forced-report acknowledgement version 2: the report ID replaces the 8-bit
 * packet ID that contract v3 no longer sends. */
#define OPP_REPORT_ACK_PROTOCOL_VERSION 2
#define OPP_REPORT_ACK_PAYLOAD_SIZE 9

typedef struct {
    uint32_t request_id;
    uint32_t report_id;
} opp_report_ack_t;

size_t opp_report_ack_encode(
    const opp_report_ack_t *ack,
    uint8_t output[OPP_REPORT_ACK_PAYLOAD_SIZE]);

bool opp_report_ack_decode(const uint8_t *payload, size_t payload_size,
                           opp_report_ack_t *ack);

bool opp_report_ack_equal(const opp_report_ack_t *left,
                          const opp_report_ack_t *right);