#include "report_ack_protocol.h"

size_t opp_report_ack_encode(
    const opp_report_ack_t *ack,
    uint8_t output[OPP_REPORT_ACK_PAYLOAD_SIZE])
{
    if (ack == NULL || output == NULL || ack->request_id == 0) {
        return 0;
    }
    output[0] = OPP_REPORT_ACK_PROTOCOL_VERSION;
    output[1] = (uint8_t)ack->request_id;
    output[2] = (uint8_t)(ack->request_id >> 8U);
    output[3] = (uint8_t)(ack->request_id >> 16U);
    output[4] = (uint8_t)(ack->request_id >> 24U);
    output[5] = ack->packet_id;
    return OPP_REPORT_ACK_PAYLOAD_SIZE;
}

bool opp_report_ack_decode(const uint8_t *payload, size_t payload_size,
                           opp_report_ack_t *ack)
{
    if (payload == NULL || ack == NULL || payload_size != OPP_REPORT_ACK_PAYLOAD_SIZE ||
        payload[0] != OPP_REPORT_ACK_PROTOCOL_VERSION) {
        return false;
    }
    const uint32_t request_id = (uint32_t)payload[1] |
                                ((uint32_t)payload[2] << 8U) |
                                ((uint32_t)payload[3] << 16U) |
                                ((uint32_t)payload[4] << 24U);
    if (request_id == 0) {
        return false;
    }
    ack->request_id = request_id;
    ack->packet_id = payload[5];
    return true;
}

bool opp_report_ack_equal(const opp_report_ack_t *left,
                          const opp_report_ack_t *right)
{
    return left != NULL && right != NULL && left->request_id == right->request_id &&
           left->packet_id == right->packet_id;
}