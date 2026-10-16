#include "sht45.h"

#include <stdint.h>

#include "driver/i2c_master.h"
#include "i2c_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "sht45_decode.h"

#define SHT45_I2C_ADDRESS 0x44
#define SHT45_I2C_FREQUENCY_HZ 100000
#define SHT45_TRANSACTION_TIMEOUT_MS 100
#define SHT45_HIGH_PRECISION_COMMAND 0xfd
#define SHT45_HIGH_PRECISION_DELAY_MS 10

static i2c_master_bus_handle_t i2c_bus;
static i2c_master_dev_handle_t sht45_device;

esp_err_t opp_sht45_init(void)
{
    if (i2c_bus != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    return opp_i2c_bus_acquire(&i2c_bus);
}

esp_err_t opp_sht45_deinit(void)
{
    if (i2c_bus == NULL) {
        return ESP_OK;
    }

    if (sht45_device != NULL) {
        esp_err_t error = i2c_master_bus_rm_device(sht45_device);
        if (error != ESP_OK) {
            return error;
        }
        sht45_device = NULL;
    }

    esp_err_t error = opp_i2c_bus_release();
    if (error == ESP_OK) {
        i2c_bus = NULL;
    }
    return error;
}

esp_err_t opp_sht45_probe(void)
{
    if (i2c_bus == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (sht45_device != NULL) {
        return ESP_OK;
    }

    esp_err_t error = i2c_master_probe(i2c_bus, SHT45_I2C_ADDRESS,
                                       SHT45_TRANSACTION_TIMEOUT_MS);
    if (error != ESP_OK) {
        return error;
    }

    const i2c_device_config_t device_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = SHT45_I2C_ADDRESS,
        .scl_speed_hz = SHT45_I2C_FREQUENCY_HZ,
    };
    return i2c_master_bus_add_device(i2c_bus, &device_config, &sht45_device);
}

esp_err_t opp_sht45_read(opp_sht45_sample_t *sample)
{
    if (sample == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (sht45_device == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const uint8_t command = SHT45_HIGH_PRECISION_COMMAND;
    esp_err_t error = i2c_master_transmit(sht45_device, &command, sizeof(command),
                                          SHT45_TRANSACTION_TIMEOUT_MS);
    if (error != ESP_OK) {
        return error;
    }

    vTaskDelay(pdMS_TO_TICKS(SHT45_HIGH_PRECISION_DELAY_MS));

    uint8_t response[OPP_SHT45_RESPONSE_SIZE];
    error = i2c_master_receive(sht45_device, response, sizeof(response),
                               SHT45_TRANSACTION_TIMEOUT_MS);
    if (error != ESP_OK) {
        return error;
    }
    if (!opp_sht45_decode_response(response, sample)) {
        return ESP_ERR_INVALID_CRC;
    }
    return ESP_OK;
}