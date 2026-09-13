idf_component_register(
    SRCS "app_main.c" "clock_sync.c" "sht45_monitor.c" "web_ui.c" "web_ui_config.c"
    INCLUDE_DIRS "."
    REQUIRES bthome_payload clock_policy power_source sensor_protocol sht45 esp_app_format esp_driver_tsens esp_driver_usb_serial_jtag esp_event esp_http_server esp_netif esp_timer esp_wifi json nvs_flash
    EMBED_FILES "web/index.html" "web/app.css" "web/app.js"
)