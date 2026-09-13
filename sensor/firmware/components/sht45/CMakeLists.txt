idf_component_register(
    SRCS "sht45.c" "sht45_decode.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2c
)