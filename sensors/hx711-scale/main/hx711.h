#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "driver/gpio.h"

typedef struct {
    gpio_num_t dout;
    gpio_num_t sck;
} hx711_t;

void hx711_init(hx711_t *dev);
bool hx711_is_ready(const hx711_t *dev);
// Blocks up to timeout_ms waiting for data. Returns false on timeout.
bool hx711_read_raw(const hx711_t *dev, int32_t *out, int timeout_ms);
// Averages n good readings. Returns false if any read timed out.
bool hx711_read_avg(const hx711_t *dev, int n, int32_t *out);
void hx711_power_down(const hx711_t *dev);
void hx711_power_up(const hx711_t *dev);
