#include "power_source.h"

const char *opp_power_source_status_value(bool usb_connected)
{
    return usb_connected ? "usb" : "battery_inferred";
}