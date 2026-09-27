#include "i2c_bus.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

static StaticSemaphore_t lock_storage;
static SemaphoreHandle_t lock;
static portMUX_TYPE create_lock = portMUX_INITIALIZER_UNLOCKED;
static i2c_master_bus_handle_t shared_bus;
static unsigned users;

static SemaphoreHandle_t bus_lock(void)
{
    portENTER_CRITICAL(&create_lock);
    if (lock == NULL) {
        lock = xSemaphoreCreateMutexStatic(&lock_storage);
    }
    portEXIT_CRITICAL(&create_lock);
    return lock;
}

esp_err_t opp_i2c_bus_acquire(i2c_master_bus_handle_t *bus)
{
    if (bus == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(bus_lock(), portMAX_DELAY);
    esp_err_t error = ESP_OK;
    if (shared_bus == NULL) {
        const i2c_master_bus_config_t config = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = CONFIG_OPP_SHT45_SDA_GPIO,
            .scl_io_num = CONFIG_OPP_SHT45_SCL_GPIO,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags.enable_internal_pullup = true,
        };
        error = i2c_new_master_bus(&config, &shared_bus);
    }
    if (error == ESP_OK) {
        ++users;
        *bus = shared_bus;
    }
    xSemaphoreGive(bus_lock());
    return error;
}

esp_err_t opp_i2c_bus_release(void)
{
    xSemaphoreTake(bus_lock(), portMAX_DELAY);
    esp_err_t error = ESP_OK;
    if (users == 0) {
        error = ESP_ERR_INVALID_STATE;
    } else if (users == 1) {
        error = i2c_del_master_bus(shared_bus);
        if (error == ESP_OK) {
            shared_bus = NULL;
            users = 0;
        }
    } else {
        --users;
    }
    xSemaphoreGive(bus_lock());
    return error;
}
