#include "report_ack_protocol.h"

bool opp_drain_is_request(const uint8_t *payload, size_t payload_size)
{
    return payload != NULL && payload_size == 1 && payload[0] == OPP_DRAIN_REQUEST;
}

bool opp_drain_is_end(const uint8_t *payload, size_t payload_size)
{
    return payload != NULL && payload_size == 1 && payload[0] == OPP_DRAIN_END;
}

size_t opp_drain_acknowledge_encode(uint32_t report_id,
                                    uint8_t output[OPP_DRAIN_ACKNOWLEDGE_SIZE])
{
    if (output == NULL || report_id == 0) {
        return 0;
    }
    output[0] = OPP_DRAIN_ACKNOWLEDGE;
    output[1] = (uint8_t)report_id;
    output[2] = (uint8_t)(report_id >> 8U);
    output[3] = (uint8_t)(report_id >> 16U);
    output[4] = (uint8_t)(report_id >> 24U);
    return OPP_DRAIN_ACKNOWLEDGE_SIZE;
}

bool opp_drain_acknowledge_decode(const uint8_t *payload, size_t payload_size,
                                  uint32_t *report_id)
{
    if (payload == NULL || report_id == NULL || payload_size != OPP_DRAIN_ACKNOWLEDGE_SIZE ||
        payload[0] != OPP_DRAIN_ACKNOWLEDGE) {
        return false;
    }
    const uint32_t value = (uint32_t)payload[1] | ((uint32_t)payload[2] << 8U) |
                           ((uint32_t)payload[3] << 16U) | ((uint32_t)payload[4] << 24U);
    if (value == 0) {
        return false;
    }
    *report_id = value;
    return true;
}
