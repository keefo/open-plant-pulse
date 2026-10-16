#include "hx711.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void hx711_init(hx711_t *dev)
{
    gpio_config_t out = {
        .pin_bit_mask = 1ULL << dev->sck,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&out);
    gpio_config_t in = {
        .pin_bit_mask = 1ULL << dev->dout,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&in);
    hx711_power_up(dev);
}

bool hx711_is_ready(const hx711_t *dev)
{
    return gpio_get_level(dev->dout) == 0;  // DOUT goes low when data is ready
}

bool hx711_read_raw(const hx711_t *dev, int32_t *out, int timeout_ms)
{
    // Time the wait with esp_timer: at the default 100 Hz tick, pdMS_TO_TICKS(1) is 0.
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (!hx711_is_ready(dev)) {
        if (esp_timer_get_time() >= deadline) return false;
        vTaskDelay(1);  // at least one tick, so lower-priority tasks get to run
    }

    uint32_t v = 0;
    // SCK high must stay < 60us or the HX711 powers down, so block interrupts.
    portENTER_CRITICAL(&s_mux);
    for (int i = 0; i < 24; i++) {
        gpio_set_level(dev->sck, 1);
        esp_rom_delay_us(1);
        v = (v << 1) | gpio_get_level(dev->dout);
        gpio_set_level(dev->sck, 0);
        esp_rom_delay_us(1);
    }
    // 25th pulse: next conversion = channel A, gain 128
    gpio_set_level(dev->sck, 1);
    esp_rom_delay_us(1);
    gpio_set_level(dev->sck, 0);
    esp_rom_delay_us(1);
    portEXIT_CRITICAL(&s_mux);

    if (v & 0x800000) v |= 0xFF000000;  // sign-extend 24-bit two's complement
    *out = (int32_t)v;
    return true;
}

bool hx711_read_avg(const hx711_t *dev, int n, int32_t *out)
{
    int64_t sum = 0;
    for (int i = 0; i < n; i++) {
        int32_t r;
        if (!hx711_read_raw(dev, &r, 500)) return false;
        sum += r;
    }
    *out = (int32_t)(sum / n);
    return true;
}

void hx711_power_down(const hx711_t *dev)
{
    gpio_set_level(dev->sck, 0);
    gpio_set_level(dev->sck, 1);
    esp_rom_delay_us(70);  // >60us high = power down
}

void hx711_power_up(const hx711_t *dev)
{
    gpio_set_level(dev->sck, 0);
}
