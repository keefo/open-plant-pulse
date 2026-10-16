idf_component_register(
    SRCS "ina219.c" "ina219_decode.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2c i2c_bus
)
