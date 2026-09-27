idf_component_register(
    SRCS "app_main.c" "battery_monitor.c" "bthome_broadcaster.c" "clock_sync.c" "device_config_store.c" "device_identity.c" "firmware_update.c" "force_report.c" "report_id.c" "sht45_monitor.c" "soil_probe.c" "web_ui.c" "web_ui_config.c" "wifi_credentials_store.c"
    INCLUDE_DIRS "."
    REQUIRES battery_gauge bthome_payload ina219 clock_policy device_config_protocol firmware_update_protocol power_source report_ack_protocol sensor_protocol sht45 wifi_credentials_protocol app_update bt esp_driver_uart esp_app_format esp_driver_tsens esp_driver_usb_serial_jtag esp_event esp_http_client esp_http_server esp_hw_support esp_netif esp_timer esp_wifi json mbedtls nvs_flash
    EMBED_FILES "web/index.html" "web/app.css" "web/app.js"
)