idf_component_register(SRCS "main.c" "devices.c"
                       INCLUDE_DIRS "."
                       REQUIRES json esp_timer nvs_flash driver)
