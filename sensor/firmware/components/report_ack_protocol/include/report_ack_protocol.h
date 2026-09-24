#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OPP_REPORT_ACK_PROTOCOL_VERSION 1
#define OPP_REPORT_ACK_PAYLOAD_SIZE 6

typedef struct {
    uint32_t request_id;
    uint8_t packet_id;
} opp_report_ack_t;

size_t opp_report_ack_encode(
    const opp_report_ack_t *ack,
    uint8_t output[OPP_REPORT_ACK_PAYLOAD_SIZE]);

bool opp_report_ack_decode(const uint8_t *payload, size_t payload_size,
                           opp_report_ack_t *ack);

bool opp_report_ack_equal(const opp_report_ack_t *left,
                          const opp_report_ack_t *right);