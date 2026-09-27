#include "ina219.h"

#include "driver/i2c_master.h"
#include "i2c_bus.h"
#include "ina219_decode.h"

#define INA219_I2C_FREQUENCY_HZ 100000
#define INA219_TRANSACTION_TIMEOUT_MS 100

static i2c_master_bus_handle_t i2c_bus;
static i2c_master_dev_handle_t device;

static esp_err_t write_register(uint8_t reg, uint16_t value)
{
    const uint8_t bytes[] = {reg, (uint8_t)(value >> 8U), (uint8_t)value};
    return i2c_master_transmit(device, bytes, sizeof(bytes), INA219_TRANSACTION_TIMEOUT_MS);
}

static esp_err_t read_register(uint8_t reg, uint16_t *value)
{
    uint8_t bytes[2];
    esp_err_t error = i2c_master_transmit_receive(device, &reg, 1, bytes, sizeof(bytes),
                                                  INA219_TRANSACTION_TIMEOUT_MS);
    if (error == ESP_OK) {
        *value = (uint16_t)((uint16_t)bytes[0] << 8U) | bytes[1];
    }
    return error;
}

esp_err_t opp_ina219_init(uint8_t address)
{
    if (device != NULL) {
        return ESP_OK;
    }
    esp_err_t error = ESP_OK;
    if (i2c_bus == NULL) {
        error = opp_i2c_bus_acquire(&i2c_bus);
        if (error != ESP_OK) {
            i2c_bus = NULL;
            return error;
        }
    }
    error = i2c_master_probe(i2c_bus, address, INA219_TRANSACTION_TIMEOUT_MS);
    if (error == ESP_OK) {
        const i2c_device_config_t config = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = address,
            .scl_speed_hz = INA219_I2C_FREQUENCY_HZ,
        };
        error = i2c_master_bus_add_device(i2c_bus, &config, &device);
    }
    if (error == ESP_OK) {
        error = write_register(OPP_INA219_REG_CONFIG, OPP_INA219_CONFIG_CONTINUOUS);
    }
    if (error != ESP_OK) {
        opp_ina219_deinit();
    }
    return error;
}

esp_err_t opp_ina219_read(opp_ina219_sample_t *sample)
{
    if (sample == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (device == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    uint16_t shunt;
    uint16_t bus;
    esp_err_t error = read_register(OPP_INA219_REG_SHUNT_VOLTAGE, &shunt);
    if (error == ESP_OK) {
        error = read_register(OPP_INA219_REG_BUS_VOLTAGE, &bus);
    }
    if (error != ESP_OK) {
        return error;
    }
    sample->shunt_microvolts = opp_ina219_shunt_microvolts(shunt);
    sample->bus_millivolts = opp_ina219_bus_millivolts(bus);
    sample->overflow = opp_ina219_bus_overflow(bus);
    return ESP_OK;
}

esp_err_t opp_ina219_deinit(void)
{
    esp_err_t error = ESP_OK;
    if (device != NULL) {
        write_register(OPP_INA219_REG_CONFIG, OPP_INA219_CONFIG_POWER_DOWN);
        error = i2c_master_bus_rm_device(device);
        if (error != ESP_OK) {
            return error;
        }
        device = NULL;
    }
    if (i2c_bus != NULL) {
        error = opp_i2c_bus_release();
        if (error == ESP_OK) {
            i2c_bus = NULL;
        }
    }
    return error;
}
